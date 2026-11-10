import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import SETTINGS
from exchange import get_trading_symbols, get_klines, get_htf_klines, analyze_funding_squeeze, get_request_stats
from screener import screen_symbols
from strategy import SweepEngine, atr, compute_htf_bias, compute_rsi_divergence_array
from risk import build_trade_plan
from state_manager import StateManager
from telegram_notify import send_telegram, fmt_entry, fmt_event, fmt_events
from volume_spike import check_volume_spike, fmt_volume_spike
from whale_alert import check_whale_alert, fmt_whale_alert, load_mark_price_map
from funding_radar import scan_funding_radar, fmt_funding_radar
from market_wide_funding_pulse import run_funding_pulse_check, send_funding_pulse, _load_pulse_state
from futures_volume_dominance import run_volume_dominance_check, send_volume_dominance, _load_dominance_state
from exchange_risk_watch import run_risk_watch_check, send_risk_watch, _load_risk_state
from oi_market_breadth import run_oi_breadth_check, send_oi_breadth, _load_oi_breadth_state
from market_sentiment_radar import run_sentiment_radar_check, send_sentiment_radar, _load_sentiment_state
from crypto_fear_greed import run_fear_greed_check, send_fear_greed, _load_fng_state
from coinmarketcap_context import (
    run_cmc_context_check, send_cmc_context,
    run_cmc_discovery_check, send_cmc_discovery,
)
from logger import get_logger

log = get_logger("bot")
state = StateManager()

# --- Graceful shutdown ---
_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    _shutdown_requested = True
    log.info("🛑 Kapatma sinyali alındı, mevcut tur tamamlanıyor...")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# --- Tur istatistikleri ---
class ScanStats:
    def __init__(self):
        self.scanned = 0
        self.signals = []      # (symbol, direction, entry, sl, tp1, score)
        self.events = []       # TP/SL/BE olayları
        self.errors = 0
        self.skipped = 0
        self.whale_alerts = [] # whale_info dict'leri (global sıralama için)

    @property
    def long_count(self):
        return sum(1 for s in self.signals if s[1] == 1)

    @property
    def short_count(self):
        return sum(1 for s in self.signals if s[1] == -1)


def process_symbol(symbol, stats: ScanStats, mark_price_map=None):
    df = get_klines(symbol)
    if df is None or len(df) < max(SETTINGS.SWING_LENGTH * 2, 50) + 5:
        stats.skipped += 1
        return

    stats.scanned += 1

    # Sadece kapanmış son mum üzerinden çalış (son satır henüz kapanmamış olabilir)
    closed_df = df.iloc[:-1].reset_index(drop=True)
    bar_time = closed_df["open_time"].iloc[-1]
    last_high, last_low = closed_df["high"].iloc[-1], closed_df["low"].iloc[-1]

    # --- Volume Spike Tespiti (sinyal akışından bağımsız, her sembol için çalışır) ---
    if SETTINGS.USE_VOLUME_SPIKE:
        try:
            spike_info = check_volume_spike(symbol, closed_df, mark_price_map=mark_price_map)
            if spike_info:
                log.info(f"🚨 {symbol}: Hacim patlaması! {spike_info['multiplier']:.1f}x, %{spike_info['change_pct']:+.2f}")
                send_telegram(fmt_volume_spike(spike_info))
        except Exception as e:
            log.debug(f"Volume spike kontrol hatası ({symbol}): {e}")

    # --- Whale Alert / Taker Akışı Radarı (sinyal akışından bağımsız) ---
    # Bildirimler tur sonunda GLOBAL sapma sırasına göre sınırlanır.
    if SETTINGS.USE_WHALE_ALERT:
        try:
            whale_info = check_whale_alert(symbol, mark_price_map=mark_price_map)
            if whale_info:
                log.info(f"🐋 {symbol}: Balina akışı! Taker buy payı %{whale_info['buy_ratio']*100:.1f}, sapma {whale_info['deviation']*100:+.1f} puan")
                stats.whale_alerts.append(whale_info)
                # Whale sinyalini signal_log.json'a kaydet (geriye dönük analiz için).
                # log_whale_signal: lock altında append + _save → persist garantili.
                try:
                    state.log_whale_signal(whale_info)
                except Exception as _e:
                    log.debug(f"Whale signal_log kayıt hatası ({symbol}): {_e}")
        except Exception as e:
            log.debug(f"Whale alert kontrol hatası ({symbol}): {e}")

    # Açık pozisyon varsa SL/TP/BE kontrolü yap
    if state.has_open(symbol):
        events = state.update_on_bar(symbol, last_high, last_low,
                                     close=closed_df["close"].iloc[-1])
        if events:
            for ev in events:
                _log_event(ev)
                stats.events.append(ev)
            send_telegram(fmt_events(events))
        return  # Pine'daki gibi: pozisyon açıkken sembol için yeni giriş aranmaz

    # HTF trend filtresi (aktifse)
    htf_bias = 0
    if SETTINGS.USE_HTF_FILTER:
        try:
            htf_df = get_htf_klines(symbol)
            htf_bias = compute_htf_bias(htf_df)
        except Exception as e:
            log.debug(f"HTF veri alınamadı: {symbol} — {e}")

    # RSI divergence tespiti (aktifse)
    rsi_div = 0
    if SETTINGS.USE_RSI_DIVERGENCE:
        try:
            rsi_div_array = compute_rsi_divergence_array(closed_df)
            rsi_div = int(rsi_div_array[-1]) if len(rsi_div_array) > 0 else 0
        except Exception as e:
            log.debug(f"RSI divergence hesaplanamadı: {symbol} — {e}")

    # Funding rate squeeze tespiti (aktifse)
    funding_info = None
    if SETTINGS.USE_FUNDING_FILTER:
        try:
            funding_info = analyze_funding_squeeze(symbol)
        except Exception as e:
            log.debug(f"Funding bilgisi alınamadı: {symbol} — {e}")

    engine = SweepEngine(htf_bias=htf_bias, funding_info=funding_info, rsi_div=rsi_div)
    setups = list(engine.iter_signals(closed_df))

    # Sweep sinyali: son bar'da mı?
    sweep_setup = None
    if setups and setups[-1].bar_index == len(closed_df) - 1:
        sweep_setup = setups[-1]

    # --- ImpulseEngine (paralel motor) ---
    impulse_setup = None
    if SETTINGS.USE_IMPULSE_ENGINE:
        try:
            from impulse_engine import ImpulseEngine
            imp_engine = ImpulseEngine(htf_bias=htf_bias, rsi_div=rsi_div, funding_info=funding_info)
            imp_setups = list(imp_engine.iter_signals(closed_df))
            if imp_setups and imp_setups[-1].bar_index == len(closed_df) - 1:
                impulse_setup = imp_setups[-1]
        except Exception as e:
            log.debug(f"ImpulseEngine hatası: {symbol} — {e}")

    if sweep_setup:
        sweep_setup.engine = "sweep"
    if impulse_setup:
        impulse_setup.engine = "impulse"

    if sweep_setup and sweep_setup.setup_type in SETTINGS.DISABLED_SETUP_TYPES:
        log.debug(f"{symbol} SweepEngine setup_type='{sweep_setup.setup_type}' DISABLED_SETUP_TYPES ile engellendi")
        sweep_setup = None
    if impulse_setup and impulse_setup.setup_type in getattr(SETTINGS, "DISABLED_IMPULSE_SETUP_TYPES", ()):
        log.debug(f"{symbol} ImpulseEngine setup_type='{impulse_setup.setup_type}' "
                  "DISABLED_IMPULSE_SETUP_TYPES ile engellendi")
        impulse_setup = None

    setup = None
    engine_used = "sweep"
    if sweep_setup and impulse_setup:
        if impulse_setup.score >= sweep_setup.score + 10.0:
            setup = impulse_setup
            engine_used = "impulse"
        else:
            setup = sweep_setup
            engine_used = "sweep"
        log.debug(
            f"{symbol} Çift motor: Sweep(sweep)={sweep_setup.score:.0f} vs "
            f"Impulse(impulse)={impulse_setup.score:.0f} → {engine_used} seçildi"
        )
    elif sweep_setup:
        setup = sweep_setup
        engine_used = "sweep"
    elif impulse_setup:
        setup = impulse_setup
        engine_used = "impulse"
    else:
        return

    setup.engine = engine_used

    if state.already_alerted(symbol, bar_time):
        return

    entry = closed_df["close"].iloc[-1]

    # (Analiz) Güvenlik ağı: _mk() içindeki kesintiler (örn. WTD cezası) skoru
    # MIN_SWEEP_SCORE'un altına düşürebilir. Bu kontrol OB bloğunun içindeydi;
    # orderbook kapalıyken / None dönerken / hata fırlatırken atlanıyordu.
    min_score = (SETTINGS.MIN_IMPULSE_SCORE if setup.engine == "impulse"
                 else SETTINGS.MIN_SWEEP_SCORE)
    if setup.score < min_score:
        log.debug(f"{symbol} {setup.engine} skoru eşik altında ({setup.score:.0f} < "
                  f"{min_score:.0f}) → aday düşürüldü")
        return

    # --- #6 Orderbook Imbalance Kademeli Analizi (Anti-Spoofing) ---
    if getattr(SETTINGS, "USE_ORDERBOOK_IMBALANCE", False):
        try:
            from exchange import analyze_orderbook_persistent
            ob_info = analyze_orderbook_persistent(symbol, entry, setup.direction)
            if ob_info:
                raw_ratio = ob_info["ratio"]
                # Ratio cap
                capped_ratio = min(raw_ratio, SETTINGS.OB_IMBALANCE_RATIO_CAP)
                setup.ob_imbalance_ratio = capped_ratio
                setup.ob_favorable = ob_info["favorable"]

                # Kademeli bonus/ceza hesapla
                tiered_score = 2.0  # varsayılan (en düşük kademe)
                for tier_max, tier_val in SETTINGS.OB_IMBALANCE_TIERS:
                    if capped_ratio <= tier_max:
                        tiered_score = tier_val
                        break

                # Anti-spoofing: kalıcılık çarpanı uygula
                effective_mult = ob_info.get("effective_multiplier", 1.0)
                tiered_score *= effective_mult

                # Spread kalitesi bonusu
                if ob_info.get("spread_quality") == "tight":
                    tiered_score += SETTINGS.OB_SPREAD_TIGHT_BONUS

                # Sığ derinlik cezası
                if ob_info.get("depth_quality") == "shallow":
                    tiered_score -= SETTINGS.OB_DEPTH_SHALLOW_PENALTY

                if ob_info["favorable"]:
                    setup.score = min(setup.score + tiered_score, 100.0)
                else:
                    setup.score -= abs(tiered_score)

                # L2 sonrası grade'i güncelle (kritik: skor değişti, grade eşleşmeli)
                for _g_min, _g_lbl, _g_prob in SETTINGS.GRADE_TABLE:
                    if setup.score >= _g_min:
                        setup.grade = _g_lbl
                        setup.probability = _g_prob
                        break

                # Kalıcılık ve kalite logları
                persist_lbl = "✅kalıcı" if ob_info.get("persistent") else "⚠️geçici"
                log.debug(
                    f"{symbol} L2 OB: oran={capped_ratio:.2f} {persist_lbl} "
                    f"(CV={ob_info.get('cv', 0):.3f}, snap={ob_info.get('snapshots_taken', 1)}) "
                    f"spread={ob_info.get('spread_quality', '?')} "
                    f"derinlik={ob_info.get('depth_quality', '?')} "
                    f"({ob_info.get('avg_depth_usd', 0)/1000:.0f}K USD) "
                    f"skor_etki={tiered_score:+.1f}"
                )
                
                if setup.score < min_score:
                    log.debug(f"{symbol} L2 Orderbook sonrası skor eşik altında ({setup.score:.0f} < {min_score:.0f}). İşlem iptal edildi.")
                    return
        except Exception as e:
            log.debug(f"Orderbook analiz hatası: {symbol} — {e}")

    atr_v = atr(closed_df, SETTINGS.ATR_LEN).values[-1]
    # --- #11 Dinamik TP/SL (rejime göre) ---
    plan = build_trade_plan(setup.direction, entry, setup.anchor_extreme, atr_v,
                            regime=setup.market_regime)

    # (Analiz) Pozisyon burada AÇILMAZ: sinyal aday olarak döndürülür.
    # Tur sonunda korelasyon kapısından geçen adaylar run_once içinde açılır
    # (eşzamanlı aynı-yön sinyal kümeleri tek anda -5R yiyordu: 28 Ağu 14:00).
    return {"symbol": symbol, "plan": plan, "score": setup.score,
            "bar_time": bar_time, "setup": setup, "htf_bias": htf_bias}


def _gate_candidates(candidates):
    """(Analiz) Portföy korelasyon kapısı (canlı tur).

    Aynı tur (aynı bar) içinde aynı yönde CORRELATION_CLUSTER_MIN veya daha
    fazla aday varsa bu piyasa geneli bir olaydır (28 Ağu 14:00: 5 paritede
    eşzamanlı A/A+ SHORT, 5/5 SL). Yalnız en yüksek skorlu
    MAX_SAME_DIR_PER_CYCLE tanesi açılır, gerisi düşürülür."""
    if not SETTINGS.CORRELATION_GATE_ENABLED or not candidates:
        return candidates
    kept = []
    for direction in (1, -1):
        group = [c for c in candidates if c["plan"].direction == direction]
        if len(group) >= SETTINGS.CORRELATION_CLUSTER_MIN:
            group.sort(key=lambda c: -c["score"])
            kept_group = group[:SETTINGS.MAX_SAME_DIR_PER_CYCLE]
            dropped = group[SETTINGS.MAX_SAME_DIR_PER_CYCLE:]
            side = "LONG" if direction == 1 else "SHORT"
            log.warning(f"🔗 Korelasyon kapısı: {len(group)} adet {side} aday aynı barda "
                        f"(piyasa geneli olay şüphesi) → en iyi {len(kept_group)} tanesi açılıyor")
            for c in dropped:
                log.info(f"   ↳ düşürüldü: {c['symbol']} (skor {c['score']:.0f})")
            kept.extend(kept_group)
        else:
            kept.extend(group)
    return kept


def _log_signal(symbol, plan, score, htf_bias, setup=None):
    """Terminale renkli LONG/SHORT sinyal bilgisi yazdırır."""
    if plan.direction == 1:
        side = "\033[1;32m🟢 LONG\033[0m"
    else:
        side = "\033[1;31m🔴 SHORT\033[0m"

    htf_str = ""
    if SETTINGS.USE_HTF_FILTER:
        htf_label = {1: "📈Yükseliş", -1: "📉Düşüş", 0: "➡️Nötr"}
        htf_str = f" | HTF: {htf_label.get(htf_bias, '?')}"

    ind_str = ""
    if setup:
        st_lbl = {1: "📈LONG", -1: "🔴SHORT", 0: "➡️Nötr"}.get(setup.supertrend_trend, "➡️Nötr")
        ind_str = f"\n  ADX: {setup.adx_value:.1f} (DI+: {setup.di_plus:.1f}, DI-: {setup.di_minus:.1f}) | ST: {st_lbl}"

    log.info(
        f"{'═' * 55}\n"
        f"  ⚡ SİNYAL: {side} │ {symbol}\n"
        f"  Skor: {score:.0f}/100{htf_str}{ind_str}\n"
        f"  Giriş:  {plan.entry:.6f}\n"
        f"  SL:     {plan.sl:.6f}\n"
        f"  TP1:    {plan.tp1:.6f}  │  TP2: {plan.tp2:.6f}  │  TP3: {plan.tp3:.6f}\n"
        f"{'═' * 55}"
    )


def _log_event(ev):
    """TP/SL/BE olaylarını terminale yazdırır."""
    if ev["type"] == "close":
        icon = "✅ WIN" if ev["win"] else "❌ LOSS"
        log.info(f"📦 KAPANDI │ {ev['symbol']} │ {icon} ({ev['reason']}) │ R: {ev['r_multiple']:+.1f}")
    elif ev["type"] == "breakeven":
        log.info(f"🛡️  BREAK-EVEN │ {ev['symbol']} │ SL → {ev['price']:.6f}")
    elif ev["type"] == "trailing_activated":
        log.info(f"🔄 İZLEYEN STOP │ {ev['symbol']} │ Aktif, SL güncellendi → {ev['price']:.6f}")
    elif ev["type"] == "sl_escalation":
        log.info(f"🔒 SL YÜKSELTİLDİ │ {ev['symbol']} │ SL → {ev['price']:.6f} ({ev.get('level', '')} seviyesine)")
    else:
        tp_icons = {"tp1": "🎯 TP1", "tp2": "🎯🎯 TP2", "tp3": "🏆 TP3"}
        log.info(f"{tp_icons.get(ev['type'], '?')} │ {ev['symbol']} │ Fiyat: {ev['price']:.6f}")


def _print_round_summary(stats: ScanStats, elapsed: float):
    """Her tur sonunda özet yazdırır."""
    log.info(f"{'─' * 55}")
    log.info(f"📊 Tur Özeti: {stats.scanned} sembol tarandı ({elapsed:.1f}s)")

    # --- Whale Alert: GLOBAL sapma sırasına göre en yüksek N tanesi ---
    if stats.whale_alerts:
        top = sorted(stats.whale_alerts, key=lambda w: abs(w["deviation"]), reverse=True)
        top_n = top[:SETTINGS.WHALE_ALERT_MAX_PER_CYCLE]
        log.info(f"   🐋 Whale alert: {len(stats.whale_alerts)} aday → en yüksek {len(top_n)} tanesi bildiriliyor "
                 f"(sapma sırasına göre)")
        for i, w in enumerate(top_n, start=1):
            send_telegram(fmt_whale_alert(w, rank=i))

    if stats.signals:
        log.info(f"   ⚡ {len(stats.signals)} sinyal: "
                 f"\033[32m{stats.long_count} LONG\033[0m │ "
                 f"\033[31m{stats.short_count} SHORT\033[0m")
        for sym, d, entry, sl, tp1, score in stats.signals:
            side = "\033[32mL\033[0m" if d == 1 else "\033[31mS\033[0m"
            log.info(f"      {side} {sym:<12} skor={score:.0f}  giriş={entry:.6f}")
    else:
        log.info("   ➖ Sinyal bulunamadı")

    if stats.events:
        closes = [e for e in stats.events if e["type"] == "close"]
        tps = [e for e in stats.events if e["type"].startswith("tp")]
        if closes:
            wins = sum(1 for c in closes if c["win"])
            log.info(f"   📦 {len(closes)} pozisyon kapandı: {wins} win / {len(closes) - wins} loss")
        if tps:
            log.info(f"   🎯 {len(tps)} TP tetiklendi")

    # Açık pozisyonlar
    open_pos = state.get_open_positions_summary()
    if open_pos:
        log.info(f"   📂 Açık pozisyonlar ({len(open_pos)}):")
        for sym, pos in open_pos.items():
            side = "\033[32mLONG\033[0m" if pos["direction"] == 1 else "\033[31mSHORT\033[0m"
            log.info(f"      {side} {sym:<12} giriş={pos['entry']:.6f}  SL={pos['sl']:.6f}")

    # Genel Kalıcı Performans İstatistikleri
    perf = state.get_performance_summary()
    if perf["total_trades"] > 0:
        log.info(
            f"   🏆 Kalıcı Performans: {perf['total_trades']} İşlem │ "
            f"\033[32m{perf['wins']} WIN\033[0m (%{perf['win_rate']:.1f}) │ "
            f"\033[31m{perf['losses']} LOSS\033[0m │ {perf['breakevens']} BE │ "
            f"R: {perf['total_r_multiple']:+.2f}"
        )

    if stats.errors:
        log.warning(f"   ⚠️  {stats.errors} hata oluştu")
    log.info(f"{'─' * 55}")


def track_open_positions(stats: ScanStats):
    """Ara döngülerde, sadece açık pozisyonları 1 dakikalık mumlarla kontrol eder."""
    open_pos = state.get_open_positions_summary()
    if not open_pos:
        return

    for sym, pos in open_pos.items():
        try:
            # Sadece son 1m mumu alarak anlık high/low kontrolü
            df_1m = get_klines(sym, interval="1m", limit=2)
            if df_1m is not None and not df_1m.empty:
                # Son güncel mum
                curr_candle = df_1m.iloc[-1]
                high = curr_candle["high"]
                low = curr_candle["low"]
                
                events = state.update_on_bar(sym, high, low, close=curr_candle["close"])
                if events:
                    for ev in events:
                        _log_event(ev)
                        stats.events.append(ev)
                    send_telegram(fmt_events(events))
        except Exception as e:
            log.debug(f"Pozisyon takibi hatası ({sym}): {e}")


def run_once():
    stats = ScanStats()
    request_counts_before = get_request_stats()

    symbols = screen_symbols() if SETTINGS.USE_SCREENER else get_trading_symbols()
    log.info(f"🔍 Taranan sembol sayısı: {len(symbols)}")

    # Whale Alert aktifse mark price haritasını tur başında TEK seferde çek.
    # Bu sayede 150 sembol için 150 ayrı /premiumIndex yerine 1 istek yeterli.
    mark_price_map = {}
    if SETTINGS.USE_WHALE_ALERT:
        try:
            mark_price_map = load_mark_price_map()
            log.debug(f"Mark price map yüklendi: {len(mark_price_map)} sembol")
        except Exception as e:
            log.warning(f"Mark price map yüklenemedi, fallback kullanılacak: {e}")

    with ThreadPoolExecutor(max_workers=SETTINGS.MAX_WORKERS) as ex:
        futures = {ex.submit(process_symbol, s, stats, mark_price_map): s for s in symbols}
        candidates = []
        for f in as_completed(futures):
            sym = futures[f]
            try:
                cand = f.result()
                if cand:
                    candidates.append(cand)
            except Exception as e:
                stats.errors += 1
                log.error(f"❌ {sym}: {e}", exc_info=True)

    # (Analiz) Korelasyon kapısı → geçen adaylar açılır
    opened = _gate_candidates(candidates)
    for cand in opened:
        # (Analiz) Tek adaydaki hata kalan adayları ve tur özetini düşürmesin
        try:
            state.open_position(cand["symbol"], cand["plan"], cand["score"],
                                cand["bar_time"], setup=cand["setup"])
            state.mark_alerted(cand["symbol"], cand["bar_time"])
            _log_signal(cand["symbol"], cand["plan"], cand["score"], cand["htf_bias"], cand["setup"])
            stats.signals.append((cand["symbol"], cand["plan"].direction, cand["plan"].entry,
                                  cand["plan"].sl, cand["plan"].tp1, cand["score"]))
            send_telegram(fmt_entry(cand["symbol"], cand["plan"], cand["score"], cand["setup"]))
        except Exception as e:
            stats.errors += 1
            log.error(f"❌ Aday pozisyon açma hatası {cand['symbol']}: {e}", exc_info=True)

    request_counts_after = get_request_stats()
    request_delta = {
        endpoint: count - request_counts_before.get(endpoint, 0)
        for endpoint, count in request_counts_after.items()
        if count - request_counts_before.get(endpoint, 0) > 0
    }
    if request_delta:
        summary = ", ".join(f"{endpoint}={count}" for endpoint, count in sorted(request_delta.items()))
        log.debug(f"Bu tarama turundaki Binance HTTP denemeleri (retry dahil): {summary}")
    return stats


if __name__ == "__main__":
    log.info("🚀 CryptoWatch Sweep Bot başlatılıyor...")
    log.info(f"   Zaman dilimi: {SETTINGS.INTERVAL} │ Screener: {'AÇIK' if SETTINGS.USE_SCREENER else 'KAPALI'} │ "
             f"HTF Filtre: {'AÇIK (' + SETTINGS.HTF_INTERVAL + ')' if SETTINGS.USE_HTF_FILTER else 'KAPALI'}")
    log.info(f"   CHoCH onayı: {'AÇIK' if SETTINGS.REQUIRE_CHOCH else 'KAPALI'} │ "
             f"Min Skor: {SETTINGS.MIN_SWEEP_SCORE} │ Workers: {SETTINGS.MAX_WORKERS}")

    try:
        # Saklanacak stats listesi (ana taramadan bağımsız olarak ara olayları tutmak isterseniz)
        current_stats = ScanStats()
        _last_radar = 0.0  # Funding Radar son gönderim zamanı
        _last_pulse = 0.0  # Funding Pulse son çalışma zamanı
        _last_dominance = 0.0  # Volume Dominance son çalışma zamanı
        _last_risk_watch = 0.0
        _last_oi_breadth = 0.0
        _last_sentiment_radar = 0.0
        _last_fear_greed = 0.0
        _last_cmc_context = 0.0

        while not _shutdown_requested:
            start = time.time()
            try:
                # 1. Ana Tarama
                current_stats = run_once()
                elapsed = time.time() - start
                _print_round_summary(current_stats, elapsed)
            except Exception as e:
                log.error(f"❌ Ana döngü hatası: {e}", exc_info=True)
                elapsed = time.time() - start

            if _shutdown_requested:
                break

            # --- Funding Radar (saatte 1 kez) ---
            if SETTINGS.USE_FUNDING_RADAR and time.time() - _last_radar >= SETTINGS.FUNDING_RADAR_INTERVAL_SEC:
                try:
                    radar_result = scan_funding_radar()
                    if radar_result:
                        send_telegram(fmt_funding_radar(radar_result))
                    _last_radar = time.time()
                except Exception as e:
                    log.error(f"❗ Funding Radar hatası: {e}")

            # --- Market-Wide Funding Pulse (bağımsız zamanlayıcı) ---
            if getattr(SETTINGS, "FUNDING_PULSE_ENABLED", False):
                pulse_interval_sec = getattr(SETTINGS, "FUNDING_PULSE_INTERVAL_MIN", 60) * 60
                if time.time() - _last_pulse >= pulse_interval_sec:
                    try:
                        pulse_state = _load_pulse_state()
                        pulse_rep = run_funding_pulse_check(pulse_state)
                        if pulse_rep:
                            send_funding_pulse(pulse_rep)
                        _last_pulse = time.time()
                    except Exception as e:
                        log.error(f"❗ Funding Pulse hatası: {e}")

            # --- Futures Volume Dominance (bağımsız zamanlayıcı) ---
            if getattr(SETTINGS, "FUTURES_VOLUME_DOMINANCE_ENABLED", False):
                dominance_interval_sec = getattr(SETTINGS, "FUTURES_VOLUME_DOMINANCE_INTERVAL_MIN", 60) * 60
                if time.time() - _last_dominance >= dominance_interval_sec:
                    try:
                        dominance_state = _load_dominance_state()
                        dominance_rep = run_volume_dominance_check(dominance_state)
                        if dominance_rep:
                            send_volume_dominance(dominance_rep)
                        _last_dominance = time.time()
                    except Exception as e:
                        log.error(f"❗ Futures Volume Dominance hatası: {e}")

            # --- Exchange Risk Watch (bağımsız değişiklik izleyici) ---
            if getattr(SETTINGS, "RISK_WATCH_ENABLED", True):
                risk_interval_sec = getattr(SETTINGS, "RISK_WATCH_INTERVAL_MIN", 240) * 60
                if time.time() - _last_risk_watch >= risk_interval_sec:
                    try:
                        risk_state = _load_risk_state()
                        risk_report = run_risk_watch_check(risk_state)
                        if risk_report:
                            send_risk_watch(risk_report)
                        _last_risk_watch = time.time()
                    except Exception as e:
                        log.error(f"❗ Exchange Risk Watch hatası: {e}")

            # --- Open Interest Market Breadth (sınırlı watchlist) ---
            if getattr(SETTINGS, "OI_BREADTH_ENABLED", True):
                oi_breadth_interval_sec = getattr(SETTINGS, "OI_BREADTH_INTERVAL_MIN", 30) * 60
                if time.time() - _last_oi_breadth >= oi_breadth_interval_sec:
                    try:
                        oi_breadth_state = _load_oi_breadth_state()
                        oi_breadth_report = run_oi_breadth_check(oi_breadth_state)
                        if oi_breadth_report:
                            send_oi_breadth(oi_breadth_report)
                        _last_oi_breadth = time.time()
                    except Exception as e:
                        log.error(f"❗ OI Market Breadth hatası: {e}")

            # --- Market Sentiment Radar (4 endpoint × sınırlı watchlist) ---
            if getattr(SETTINGS, "SENTIMENT_RADAR_ENABLED", True):
                sentiment_interval_sec = getattr(SETTINGS, "SENTIMENT_RADAR_INTERVAL_MIN", 60) * 60
                if time.time() - _last_sentiment_radar >= sentiment_interval_sec:
                    try:
                        sentiment_state = _load_sentiment_state()
                        sentiment_report = run_sentiment_radar_check(sentiment_state)
                        if sentiment_report:
                            send_sentiment_radar(sentiment_report)
                        _last_sentiment_radar = time.time()
                    except Exception as e:
                        log.error(f"❗ Market Sentiment Radar hatası: {e}")

            # --- External Crypto Fear & Greed context (free, no API key) ---
            if getattr(SETTINGS, "CRYPTO_FNG_ENABLED", True):
                fear_greed_interval_sec = getattr(SETTINGS, "CRYPTO_FNG_INTERVAL_MIN", 360) * 60
                if time.time() - _last_fear_greed >= fear_greed_interval_sec:
                    try:
                        fng_state = _load_fng_state()
                        fng_report = run_fear_greed_check(fng_state)
                        if fng_report:
                            send_fear_greed(fng_report)
                        _last_fear_greed = time.time()
                    except Exception as e:
                        log.error(f"❗ Crypto Fear & Greed hatası: {e}")

            # --- CoinMarketCap global spot-market context (single low-frequency call) ---
            if getattr(SETTINGS, "CMC_ENABLED", True):
                cmc_interval_sec = max(15, getattr(SETTINGS, "CMC_INTERVAL_MIN", 360)) * 60
                if time.time() - _last_cmc_context >= cmc_interval_sec:
                    try:
                        cmc_report = run_cmc_context_check()
                        if cmc_report:
                            send_cmc_context(cmc_report)
                        discovery_report = run_cmc_discovery_check()
                        if discovery_report:
                            send_cmc_discovery(discovery_report)
                        _last_cmc_context = time.time()
                    except Exception as e:
                        log.error(f"❗ CoinMarketCap piyasa özeti hatası: {e}")

            wait = max(30, SETTINGS.SCAN_INTERVAL_SECONDS - elapsed)
            log.info(f"⏰ {wait:.0f}s sonra tekrar taranacak (Açık pozisyonlar 1dk aralıkla takip ediliyor)...\n")
            
            # 2. Ara Bekleme ve Anlık Takip (1 Dakikada bir)
            wait_end = time.time() + wait
            last_track = time.time()
            
            while time.time() < wait_end and not _shutdown_requested:
                if time.time() - last_track >= 60:
                    track_open_positions(current_stats)
                    last_track = time.time()
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("👋 Bot kapatılıyor, durum diske yazılıyor...")
        # StateManager zaten her değişiklikte persist ediyor, ek işlem gerekmez
        log.info("✅ Bot güvenli şekilde kapatıldı.")
