"""
Signal Analyzer — Sinyal İstihbarat ve Öğrenme Motoru

Kapanmış işlemleri ve sinyal loglarını analiz ederek:
  1. Hangi strateji türlerinin kârlı olduğunu
  2. Hangi gösterge kombinasyonlarının başarılı sinyaller ürettiğini
  3. Hangi skor aralıklarının/grade'lerin en iyi sonuç verdiğini
  4. Piyasa rejimi bazında performansı
  5. Geçmiş sinyallerin forward return analizini (1h, 4h, 12h, 24h)

tespit eder ve strateji geliştirme önerileri sunar.

Kullanım:
  python signal_analyzer.py                # Tam rapor (terminale + state/analysis_report.md)
  python signal_analyzer.py --summary      # Kısa özet
  python signal_analyzer.py --retroactive  # Geçmiş sinyalleri Binance'tan tarayarak forward return ölç
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

# Projenin kök dizinini ayarla
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SETTINGS
from logger import get_logger

log = get_logger("analyzer")


# ─────────────────────────────────────────────────────────────────────
# Yardımcı Fonksiyonlar
# ─────────────────────────────────────────────────────────────────────

def _load_json(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"JSON yükleme hatası ({path}): {e}. Varsayılan değer kullanılacak.")
        return []


def _save_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except (PermissionError, OSError):
            time.sleep(0.05)


def _pct(part, total):
    return (part / total * 100.0) if total > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────
# Ana Analiz Fonksiyonları
# ─────────────────────────────────────────────────────────────────────

def analyze_by_dimension(trades, key_fn, label):
    """Verilen anahtar fonksiyonuna göre grupla ve WIN/LOSS/BE dağılımını hesapla."""
    groups = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        groups[k].append(t)

    results = []
    for k in sorted(groups.keys(), key=str):
        group = groups[k]
        total = len(group)
        wins = sum(1 for t in group if t.get("outcome") == "WIN")
        losses = sum(1 for t in group if t.get("outcome") == "LOSS")
        be = sum(1 for t in group if t.get("outcome") == "BREAKEVEN")
        total_r = sum(float(t.get("r_multiple", 0.0)) for t in group)
        decisive = wins + losses
        wr = _pct(wins, decisive)
        results.append({
            "key": k, "total": total, "wins": wins, "losses": losses,
            "breakevens": be, "win_rate": round(wr, 1),
            "total_r": round(total_r, 1),
            "avg_r": round(total_r / total, 2) if total > 0 else 0.0,
        })
    return label, results


def score_range(score):
    if score < 50: return "0-49"
    elif score < 60: return "50-59"
    elif score < 70: return "60-69"
    elif score < 80: return "70-79"
    elif score < 90: return "80-89"
    elif score <= 100: return "90-100"
    else: return "100+"


def analyze_indicators(trades):
    """Gösterge değerlerinin WIN vs LOSS korelasyonunu analiz eder."""
    numeric_fields = [
        "adx_value", "di_plus", "di_minus", "confluence_score",
        "ob_imbalance_ratio", "linreg_osc", "tsi_value", "tsi_signal",
    ]
    bool_fields = [
        "fvg_bull_near", "fvg_bear_near", "ob_bull_near", "ob_bear_near",
        "whale_bull_absorption", "whale_bear_absorption", "lux_liq_sweep",
        "ob_favorable", "htf_counter_trend",
    ]
    categorical_fields = [
        "market_regime", "supertrend_trend", "pmax_trend", "ema_cross",
        "engulfing", "bos_signal", "whale_delta_dominance",
    ]

    results = {}

    # Numerik göstergeler: WIN vs LOSS ortalamaları
    for field in numeric_fields:
        win_vals = []
        loss_vals = []
        for t in trades:
            ind = t.get("indicators", {})
            val = ind.get(field)
            if val is None:
                continue
            try:
                fval = float(val)
            except (ValueError, TypeError):
                continue
            if t.get("outcome") == "WIN":
                win_vals.append(fval)
            elif t.get("outcome") == "LOSS":
                loss_vals.append(fval)

        if win_vals or loss_vals:
            results[field] = {
                "type": "numeric",
                "win_avg": round(sum(win_vals) / len(win_vals), 2) if win_vals else None,
                "win_count": len(win_vals),
                "loss_avg": round(sum(loss_vals) / len(loss_vals), 2) if loss_vals else None,
                "loss_count": len(loss_vals),
            }

    # Boolean göstergeler: True/False durumlarında WIN oranı
    for field in bool_fields:
        true_wins = 0; true_total = 0
        false_wins = 0; false_total = 0
        for t in trades:
            ind = t.get("indicators", {})
            val = ind.get(field)
            if val is None:
                continue
            if val:
                true_total += 1
                if t.get("outcome") == "WIN":
                    true_wins += 1
            else:
                false_total += 1
                if t.get("outcome") == "WIN":
                    false_wins += 1

        if true_total > 0 or false_total > 0:
            results[field] = {
                "type": "boolean",
                "true_win_rate": round(_pct(true_wins, true_total), 1),
                "true_total": true_total,
                "false_win_rate": round(_pct(false_wins, false_total), 1),
                "false_total": false_total,
            }

    # Kategorik göstergeler: Her değer için WIN oranı
    for field in categorical_fields:
        cats = defaultdict(lambda: {"wins": 0, "total": 0})
        for t in trades:
            ind = t.get("indicators", {})
            val = ind.get(field)
            if val is None:
                continue
            cats[str(val)]["total"] += 1
            if t.get("outcome") == "WIN":
                cats[str(val)]["wins"] += 1

        if cats:
            cat_results = {}
            for cat_val, counts in cats.items():
                cat_results[cat_val] = {
                    "win_rate": round(_pct(counts["wins"], counts["total"]), 1),
                    "total": counts["total"],
                }
            results[field] = {"type": "categorical", "values": cat_results}

    return results


# ─────────────────────────────────────────────────────────────────────
# Retroaktif Analiz (Geçmiş sinyallerin forward return analizi)
# ─────────────────────────────────────────────────────────────────────

def retroactive_forward_returns(alerted_file, max_symbols=None, delay=0.15):
    """alerted.json'daki geçmiş sinyaller için Binance'tan veri çekerek
    sinyal anından sonraki 1h, 4h, 12h, 24h forward return hesaplar.

    Rate-limit korumalı: her API çağrısı arasında 'delay' saniye bekler.
    """
    from exchange import get_klines_paginated

    alerted = _load_json(alerted_file)
    if isinstance(alerted, dict):
        entries = [(sym, bar_time) for sym, bar_time in alerted.items()]
    else:
        return []

    if max_symbols and len(entries) > max_symbols:
        entries = entries[-max_symbols:]  # En son sinyallerden başla

    results = []
    total = len(entries)
    log.info(f"📊 Retroaktif analiz başlatılıyor: {total} sinyal")

    for idx, (symbol, bar_time_str) in enumerate(entries):
        try:
            # bar_time'dan datetime objesi oluştur
            bar_dt = datetime.strptime(bar_time_str, "%Y-%m-%d %H:%M:%S")

            # Sinyalden sonraki ~2 gün veri çek (15m mumlar = 192 mum)
            df = get_klines_paginated(symbol, "15m", days=2)
            if df is None or df.empty:
                log.debug(f"  [{idx+1}/{total}] {symbol}: Veri alınamadı, atlanıyor")
                continue

            # bar_time'a en yakın mum indeksini bul
            df["open_time_dt"] = df["open_time"]
            mask = df["open_time_dt"] >= bar_dt
            if not mask.any():
                log.debug(f"  [{idx+1}/{total}] {symbol}: Bar time bulunamadı, atlanıyor")
                continue

            signal_idx = mask.idxmax()
            signal_close = df.loc[signal_idx, "close"]

            # Forward return hesaplama (15m bar = 4 bar/saat)
            forward_periods = {
                "1h": 4,    # 4 x 15m
                "4h": 16,   # 16 x 15m
                "12h": 48,  # 48 x 15m
                "24h": 96,  # 96 x 15m
            }

            fr = {"symbol": symbol, "bar_time": bar_time_str, "signal_close": signal_close}
            for period_name, bars_ahead in forward_periods.items():
                future_idx = signal_idx + bars_ahead
                if future_idx < len(df):
                    future_close = df.loc[future_idx, "close"]
                    pct_change = (future_close - signal_close) / signal_close * 100
                    fr[f"return_{period_name}"] = round(pct_change, 3)
                    fr[f"close_{period_name}"] = future_close
                else:
                    fr[f"return_{period_name}"] = None

            # Bu sembolün trade_history'deki sonucunu bul
            trade_history = _load_json(SETTINGS.TRADE_HISTORY_FILE)
            matching = [t for t in trade_history if t.get("symbol") == symbol
                        and t.get("opened_at") == bar_time_str]
            if matching:
                fr["outcome"] = matching[0].get("outcome", "UNKNOWN")
                fr["r_multiple"] = matching[0].get("r_multiple", 0)
                fr["setup_type"] = matching[0].get("setup_type", "?")
                fr["direction"] = matching[0].get("direction", "?")
                fr["score"] = matching[0].get("score", 0)
            else:
                fr["outcome"] = "NO_TRADE"

            results.append(fr)

            if (idx + 1) % 10 == 0:
                log.info(f"  📊 [{idx+1}/{total}] işlendi...")

            time.sleep(delay)  # Rate-limit koruması

        except Exception as e:
            log.debug(f"  [{idx+1}/{total}] {symbol}: Hata — {e}")
            continue

    log.info(f"✅ Retroaktif analiz tamamlandı: {len(results)}/{total} sinyal başarıyla işlendi")
    return results


def reconstruct_and_evaluate_alerted(alerted_file, max_symbols=None, delay=0.15, force=False):
    """state/alerted.json dosyasındaki tüm benzersiz (symbol, bar_time) çiftleri için:
    1. Rate limit korumalı olarak Binance'tan geçmiş mum verisini çeker.
    2. Sinyal anındaki indikatörleri ve sinyal yapısını (SweepEngine ve ImpulseEngine) yeniden inşa eder.
    3. Sinyal sonrasındaki mum hareketlerini adım adım simüle ederek gerçek sonucunu (WIN/LOSS/BREAKEVEN) ve R çarpanını hesaplar.
    4. trade_history.json, signal_log.json ve stats.json dosyalarını zenginleştirerek günceller.
    """
    import pandas as pd
    from exchange import get_klines_paginated
    from strategy import SweepEngine, atr
    from impulse_engine import ImpulseEngine
    from risk import build_trade_plan
    from state_manager import StateManager

    alerted = _load_json(alerted_file)
    if not isinstance(alerted, dict):
        log.warning("alerted.json okunamadı veya geçerli bir sözlük değil.")
        return []

    # Benzersiz (symbol, bar_time) çiftlerini temizle ve sırala (duplikasyon önleme)
    raw_entries = [(sym, bar_time) for sym, bar_time in alerted.items()]
    unique_entries = []
    seen = set()
    for sym, bar_time in raw_entries:
        key = (sym, str(bar_time))
        if key not in seen:
            seen.add(key)
            unique_entries.append((sym, str(bar_time)))

    # Kronolojik olarak sırala (en eskiden en yeniye)
    try:
        unique_entries.sort(key=lambda x: datetime.strptime(x[1], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass

    if max_symbols and len(unique_entries) > max_symbols:
        unique_entries = unique_entries[-max_symbols:]

    total = len(unique_entries)
    log.info(f"🔍 Retroaktif Sinyal İnceleme & Öğrenme Başlatılıyor: {total} benzersiz sinyal")

    trade_history = _load_json(SETTINGS.TRADE_HISTORY_FILE)
    signal_log = _load_json(SETTINGS.SIGNAL_LOG_FILE)

    history_map = {(t["symbol"], t["opened_at"]): t for t in trade_history}
    log_map = {(s["symbol"], s["bar_time"]): s for s in signal_log}

    new_trades = 0
    updated_trades = 0

    for idx, (symbol, bar_time_str) in enumerate(unique_entries):
        try:
            bar_dt = pd.to_datetime(bar_time_str)
        except Exception:
            continue

        # Zaten kaydedilmiş ve indicators alanı doluysa (ve force=False ise) es geç
        existing_trade = history_map.get((symbol, bar_time_str))
        if existing_trade and existing_trade.get("indicators") and not force:
            continue

        # Binance'tan kline çek (rate limit korumalı)
        try:
            df = get_klines_paginated(symbol, "15m", days=7)
            time.sleep(delay)
        except Exception as e:
            log.debug(f"  [{idx+1}/{total}] {symbol}: Klines alma hatası ({e})")
            continue

        if df is None or df.empty:
            log.debug(f"  [{idx+1}/{total}] {symbol}: Klines alınamadı")
            continue

        # Sinyal mumunun indeksini bul
        idx_mask = df["open_time"] == bar_dt
        if not idx_mask.any():
            diffs = (df["open_time"] - bar_dt).abs()
            nearest_idx = diffs.idxmin()
            if diffs.loc[nearest_idx] > pd.Timedelta(minutes=30):
                log.debug(f"  [{idx+1}/{total}] {symbol}: Bar bulunamadı ({bar_time_str})")
                continue
            target_idx = nearest_idx
        else:
            target_idx = df[idx_mask].index[0]

        if target_idx < 30:  # Yeterli geçmiş yoksa
            continue

        # Sinyal anına kadarki alt veri kümesini al
        sub_df = df.iloc[: target_idx + 1].copy().reset_index(drop=True)

        # Sinyal motorlarını çalıştır
        se = SweepEngine()
        ie = ImpulseEngine()

        se_setups = list(se.iter_signals(sub_df))
        ie_setups = list(ie.iter_signals(sub_df))

        # Uygun setup seçimi
        setup = None
        if se_setups and se_setups[-1].bar_index == len(sub_df) - 1:
            setup = se_setups[-1]
        elif ie_setups and ie_setups[-1].bar_index == len(sub_df) - 1:
            setup = ie_setups[-1]
        elif se_setups:
            setup = se_setups[-1]
        elif ie_setups:
            setup = ie_setups[-1]

        if not setup:
            log.debug(f"  [{idx+1}/{total}] {symbol}: Reconstructed setup bulunamadı")
            continue

        entry = sub_df["close"].iloc[-1]
        atr_v = atr(sub_df, SETTINGS.ATR_LEN).values[-1]
        plan = build_trade_plan(setup.direction, entry, setup.anchor_extreme, atr_v, regime=setup.market_regime)
        indicators = StateManager._extract_indicators(setup)

        # Sinyal sonrasını adım adım simüle et
        future_df = df.iloc[target_idx + 1 :].copy().reset_index(drop=True)

        pos = {
            "direction": plan.direction, "entry": plan.entry, "sl": plan.sl,
            "tp1": plan.tp1, "tp2": plan.tp2, "tp3": plan.tp3,
            "tp1_reached": False, "tp2_reached": False, "tp3_reached": False, "be_active": False,
        }

        outcome = "EXPIRED"
        reason = "EXPIRED"
        r_multiple = 0.0
        closed_at_str = bar_time_str

        for _, f_row in future_df.iterrows():
            high = f_row["high"]
            low = f_row["low"]
            d = pos["direction"]
            sl_hit = (low <= pos["sl"]) if d == 1 else (high >= pos["sl"])
            tp1_hit = (high >= pos["tp1"]) if d == 1 else (low <= pos["tp1"])
            tp2_hit = (high >= pos["tp2"]) if d == 1 else (low <= pos["tp2"])
            tp3_hit = (high >= pos["tp3"]) if d == 1 else (low <= pos["tp3"])

            if tp1_hit and not pos["tp1_reached"] and not sl_hit:
                pos["tp1_reached"] = True
                pos["sl"] = pos["entry"]
                pos["be_active"] = True

            if tp2_hit and not pos["tp2_reached"] and not sl_hit:
                pos["tp2_reached"] = True
                pos["sl"] = pos["tp1"]

            if tp3_hit and not pos["tp3_reached"] and not sl_hit:
                pos["tp3_reached"] = True

            if sl_hit or pos["tp3_reached"]:
                win = pos["tp1_reached"]
                reason = "TP3" if pos["tp3_reached"] else ("BE" if pos["be_active"] else "SL")
                if reason == "BE" or (sl_hit and pos["be_active"]):
                    outcome = "BREAKEVEN"
                    r_multiple = 0.0
                elif win or pos["tp3_reached"]:
                    outcome = "WIN"
                    r_multiple = SETTINGS.TP3_R if pos["tp3_reached"] else SETTINGS.TP1_R
                else:
                    outcome = "LOSS"
                    r_multiple = -1.0
                closed_at_str = str(f_row["open_time"])
                break

        # Record oluştur
        trade_record = {
            "symbol": symbol,
            "direction": "LONG" if setup.direction == 1 else "SHORT",
            "entry": plan.entry,
            "close_price": pos["tp3"] if pos["tp3_reached"] else pos["sl"],
            "score": round(setup.score, 2),
            "setup_type": setup.setup_type,
            "grade": getattr(setup, "grade", "B"),
            "market_regime": setup.market_regime,
            "opened_at": bar_time_str,
            "closed_at": closed_at_str,
            "reason": reason,
            "outcome": outcome,
            "win": (outcome == "WIN"),
            "r_multiple": r_multiple,
            "tp1_reached": pos["tp1_reached"],
            "tp2_reached": pos["tp2_reached"],
            "tp3_reached": pos["tp3_reached"],
            "indicators": indicators,
        }

        # trade_history güncelle veya ekle
        if existing_trade:
            existing_trade.update(trade_record)
            updated_trades += 1
        else:
            trade_history.append(trade_record)
            history_map[(symbol, bar_time_str)] = trade_record
            new_trades += 1

        # signal_log güncelle veya ekle
        if (symbol, bar_time_str) not in log_map:
            sig_record = {
                "symbol": symbol,
                "bar_time": bar_time_str,
                "direction": "LONG" if setup.direction == 1 else "SHORT",
                "setup_type": setup.setup_type,
                "score": round(setup.score, 2),
                "grade": getattr(setup, "grade", "B"),
                "market_regime": setup.market_regime,
                "entry": plan.entry,
                "sl": plan.sl,
                "tp1": plan.tp1,
                "tp2": plan.tp2,
                "tp3": plan.tp3,
                "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "indicators": indicators,
            }
            signal_log.append(sig_record)
            log_map[(symbol, bar_time_str)] = sig_record

        log.info(f"  [{idx+1}/{total}] {symbol} ({bar_time_str}): {setup.setup_type} → {outcome} (R={r_multiple:+.1f})")

    # Diske kaydet
    _save_json(SETTINGS.TRADE_HISTORY_FILE, trade_history)
    _save_json(SETTINGS.SIGNAL_LOG_FILE, signal_log)

    # StateManager ile stats.json'ı güncelle
    sm = StateManager()
    sm.trade_history = trade_history
    sm._recalculate_stats()
    sm._persist()

    log.info(f"✅ Retroaktif İnceleme Tamamlandı: {new_trades} yeni işlem eklendi, {updated_trades} işlem güncellendi.")
    return trade_history


def deep_scan_historical(days=30, chunk_days=7, max_symbols=50, delay=0.15, force=False, start_days=0):
    """Binance Futures'taki en hacimli semboller için geriye doğru (örn: geriye dönük 30..60. günler 7'şer günlük bloklar halinde)
    tüm mum verilerini çeker, SweepEngine sinyallerini tarar, tüm indikatör verilerini çıkarır,
    ve gelecekteki mum hareketlerini simüle ederek kârlılığı ölçer.
    Sonuçlar trade_history.json, signal_log.json ve stats.json dosyalarına otomatik kaydedilir.
    """
    import pandas as pd
    from exchange import get_trading_symbols, get_klines_paginated
    from strategy import SweepEngine, atr
    from risk import build_trade_plan
    from state_manager import StateManager

    symbols = get_trading_symbols()
    if max_symbols and len(symbols) > max_symbols:
        symbols = symbols[:max_symbols]

    log.info(f"🚀 DERİN GEÇMİŞ TARAMASI BAŞLATILIYOR: {len(symbols)} sembol │ Geriye Dönük {start_days}..{start_days + days}. Günler ({chunk_days}'er günlük bloklar)")

    trade_history = _load_json(SETTINGS.TRADE_HISTORY_FILE)
    signal_log = _load_json(SETTINGS.SIGNAL_LOG_FILE)

    history_map = {(t["symbol"], t["opened_at"]): t for t in trade_history}
    log_map = {(s["symbol"], s["bar_time"]): s for s in signal_log}

    total_new = 0
    num_chunks = max(1, days // chunk_days)

    for chunk_idx in range(num_chunks):
        end_days_ago = start_days + chunk_idx * chunk_days
        start_days_ago = start_days + (chunk_idx + 1) * chunk_days
        log.info(f"  📅 Blok {chunk_idx + 1}/{num_chunks}: Geriye Dönük {start_days_ago}..{end_days_ago}. Günler Taranıyor...")

        for s_idx, symbol in enumerate(symbols):
            if (s_idx + 1) % 10 == 0 or s_idx == 0:
                log.info(f"    🔍 [{s_idx + 1}/{len(symbols)}] {symbol} taranıyor...")
            try:
                # 7 günlük kline verisini çek (rate limit korumalı)
                df = get_klines_paginated(symbol, "15m", days=start_days_ago)
                time.sleep(delay)

                if df is None or len(df) < 100:
                    continue

                # SweepEngine motorunu çalıştır
                se = SweepEngine()
                setups = list(se.iter_signals(df))

                if not setups:
                    continue

                for setup in setups:
                    bar_idx = setup.bar_index
                    if bar_idx < 30 or bar_idx >= len(df) - 10:
                        continue

                    bar_time_str = str(df["open_time"].iloc[bar_idx])
                    if (symbol, bar_time_str) in history_map and not force:
                        continue

                    sub_df = df.iloc[: bar_idx + 1].copy().reset_index(drop=True)
                    entry = sub_df["close"].iloc[-1]
                    atr_v = atr(sub_df, SETTINGS.ATR_LEN).values[-1]
                    plan = build_trade_plan(setup.direction, entry, setup.anchor_extreme, atr_v, regime=setup.market_regime)
                    indicators = StateManager._extract_indicators(setup)

                    # Gelecekteki mumları simüle et
                    future_df = df.iloc[bar_idx + 1 :].copy().reset_index(drop=True)
                    pos = {
                        "direction": plan.direction, "entry": plan.entry, "sl": plan.sl,
                        "tp1": plan.tp1, "tp2": plan.tp2, "tp3": plan.tp3,
                        "tp1_reached": False, "tp2_reached": False, "tp3_reached": False, "be_active": False,
                    }
                    outcome = "EXPIRED"; reason = "EXPIRED"; r_multiple = 0.0; closed_at_str = bar_time_str

                    for _, f_row in future_df.iterrows():
                        high = f_row["high"]; low = f_row["low"]
                        d = pos["direction"]
                        sl_hit = (low <= pos["sl"]) if d == 1 else (high >= pos["sl"])
                        tp1_hit = (high >= pos["tp1"]) if d == 1 else (low <= pos["tp1"])
                        tp2_hit = (high >= pos["tp2"]) if d == 1 else (low <= pos["tp2"])
                        tp3_hit = (high >= pos["tp3"]) if d == 1 else (low <= pos["tp3"])

                        if tp1_hit and not pos["tp1_reached"] and not sl_hit:
                            pos["tp1_reached"] = True; pos["sl"] = pos["entry"]; pos["be_active"] = True
                        if tp2_hit and not pos["tp2_reached"] and not sl_hit:
                            pos["tp2_reached"] = True; pos["sl"] = pos["tp1"]
                        if tp3_hit and not pos["tp3_reached"] and not sl_hit:
                            pos["tp3_reached"] = True

                        if sl_hit or pos["tp3_reached"]:
                            win = pos["tp1_reached"]
                            reason = "TP3" if pos["tp3_reached"] else ("BE" if pos["be_active"] else "SL")
                            if reason == "BE" or (sl_hit and pos["be_active"]):
                                outcome = "BREAKEVEN"; r_multiple = 0.0
                            elif win or pos["tp3_reached"]:
                                outcome = "WIN"; r_multiple = SETTINGS.TP3_R if pos["tp3_reached"] else SETTINGS.TP1_R
                            else:
                                outcome = "LOSS"; r_multiple = -1.0
                            closed_at_str = str(f_row["open_time"])
                            break

                    trade_record = {
                        "symbol": symbol, "direction": "LONG" if setup.direction == 1 else "SHORT",
                        "entry": plan.entry, "close_price": pos["tp3"] if pos["tp3_reached"] else pos["sl"],
                        "score": round(setup.score, 2), "setup_type": setup.setup_type,
                        "grade": getattr(setup, "grade", "B"), "market_regime": setup.market_regime,
                        "opened_at": bar_time_str, "closed_at": closed_at_str,
                        "reason": reason, "outcome": outcome, "win": (outcome == "WIN"),
                        "r_multiple": r_multiple, "tp1_reached": pos["tp1_reached"],
                        "tp2_reached": pos["tp2_reached"], "tp3_reached": pos["tp3_reached"],
                        "indicators": indicators,
                    }
                    trade_history.append(trade_record)
                    history_map[(symbol, bar_time_str)] = trade_record

                    if (symbol, bar_time_str) not in log_map:
                        sig_record = {
                            "symbol": symbol, "bar_time": bar_time_str,
                            "direction": "LONG" if setup.direction == 1 else "SHORT",
                            "setup_type": setup.setup_type, "score": round(setup.score, 2),
                            "grade": getattr(setup, "grade", "B"), "market_regime": setup.market_regime,
                            "entry": plan.entry, "sl": plan.sl, "tp1": plan.tp1, "tp2": plan.tp2, "tp3": plan.tp3,
                            "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "indicators": indicators,
                        }
                        signal_log.append(sig_record)
                        log_map[(symbol, bar_time_str)] = sig_record

                    total_new += 1
                    log.info(f"    ⚡ [{total_new}] {symbol} ({bar_time_str}): {setup.setup_type} → {outcome} (R={r_multiple:+.1f})")

            except Exception as e:
                log.debug(f"  Derin tarama hatası ({symbol}): {e}")
                continue

    _save_json(SETTINGS.TRADE_HISTORY_FILE, trade_history)
    _save_json(SETTINGS.SIGNAL_LOG_FILE, signal_log)
    sm = StateManager()
    sm.trade_history = trade_history
    sm._recalculate_stats()
    sm._persist()

    log.info(f"🏆 DERİN GEÇMİŞ TARAMASI TAMAMLANDI! {total_new} yeni tarihsel sinyal ve performans verisi kaydedildi.")
    return trade_history


# ─────────────────────────────────────────────────────────────────────
# Rapor Oluşturma
# ─────────────────────────────────────────────────────────────────────

def generate_report(trades, signal_log=None, forward_returns=None):
    """Tam analiz raporu oluşturur ve Markdown formatında döner."""
    lines = []
    lines.append("# 📊 Signal Intelligence — Analiz Raporu")
    lines.append(f"\n> Rapor Zamanı: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> Toplam Kapanmış İşlem: {len(trades)}")
    if signal_log:
        lines.append(f"> Signal Log Kayıt: {len(signal_log)}")
    lines.append("")

    # ── Genel İstatistikler ──
    wins = sum(1 for t in trades if t.get("outcome") == "WIN")
    losses = sum(1 for t in trades if t.get("outcome") == "LOSS")
    be = sum(1 for t in trades if t.get("outcome") == "BREAKEVEN")
    total_r = sum(float(t.get("r_multiple", 0.0)) for t in trades)
    decisive = wins + losses

    lines.append("## Genel Performans")
    lines.append("")
    lines.append("| Metrik | Değer |")
    lines.append("|--------|-------|")
    lines.append(f"| Toplam İşlem | {len(trades)} |")
    lines.append(f"| WIN | {wins} ({_pct(wins, len(trades)):.1f}%) |")
    lines.append(f"| LOSS | {losses} ({_pct(losses, len(trades)):.1f}%) |")
    lines.append(f"| BREAKEVEN | {be} ({_pct(be, len(trades)):.1f}%) |")
    lines.append(f"| Win Rate (Kesin) | {_pct(wins, decisive):.1f}% ({decisive} işlem) |")
    lines.append(f"| Toplam R | {total_r:+.1f}R |")
    lines.append(f"| Ortalama R | {total_r / len(trades):+.2f}R |" if trades else "")
    lines.append("")

    # ── Boyut Bazlı Analizler ──
    dimensions = [
        analyze_by_dimension(trades, lambda t: t.get("setup_type", "?"), "Setup Type"),
        analyze_by_dimension(trades, lambda t: t.get("direction", "?"), "Yön (Direction)"),
        analyze_by_dimension(trades, lambda t: score_range(t.get("score", 0)), "Skor Aralığı"),
        analyze_by_dimension(trades, lambda t: t.get("grade", "?"), "Grade"),
        analyze_by_dimension(trades, lambda t: t.get("market_regime", "?"), "Piyasa Rejimi"),
    ]

    for dim_label, dim_results in dimensions:
        lines.append(f"## {dim_label} Bazında Performans")
        lines.append("")
        lines.append(f"| {dim_label} | İşlem | WIN | LOSS | BE | Win Rate | R | Ort. R |")
        lines.append("|" + "---|" * 8)
        for r in dim_results:
            lines.append(
                f"| {r['key']} | {r['total']} | {r['wins']} | {r['losses']} | "
                f"{r['breakevens']} | {r['win_rate']}% | {r['total_r']:+.1f} | {r['avg_r']:+.2f} |"
            )
        lines.append("")

    # ── Gösterge Korelasyonu ──
    indicator_results = analyze_indicators(trades)
    if indicator_results:
        lines.append("## Gösterge Korelasyonu (WIN vs LOSS)")
        lines.append("")

        # Numerik göstergeler
        numeric = {k: v for k, v in indicator_results.items() if v.get("type") == "numeric"}
        if numeric:
            lines.append("### Numerik Göstergeler (WIN vs LOSS Ortalaması)")
            lines.append("")
            lines.append("| Gösterge | WIN Ort. | WIN N | LOSS Ort. | LOSS N | Fark |")
            lines.append("|" + "---|" * 6)
            for name, data in sorted(numeric.items()):
                wa = data.get("win_avg")
                la = data.get("loss_avg")
                diff = ""
                if wa is not None and la is not None:
                    diff = f"{wa - la:+.2f}"
                lines.append(
                    f"| {name} | {wa if wa is not None else '-'} | {data['win_count']} | "
                    f"{la if la is not None else '-'} | {data['loss_count']} | {diff} |"
                )
            lines.append("")

        # Kategorik göstergeler
        categorical = {k: v for k, v in indicator_results.items() if v.get("type") == "categorical"}
        if categorical:
            lines.append("### Kategorik Göstergeler")
            lines.append("")
            for name, data in sorted(categorical.items()):
                lines.append(f"**{name}:**")
                lines.append("")
                lines.append("| Değer | İşlem | Win Rate |")
                lines.append("|---|---|---|")
                for val, counts in sorted(data["values"].items(), key=lambda x: -x[1]["total"]):
                    lines.append(f"| {val} | {counts['total']} | {counts['win_rate']}% |")
                lines.append("")

    # ── Forward Return Analizi ──
    if forward_returns:
        lines.append("## Forward Return Analizi (Sinyal Sonrası Getiri)")
        lines.append("")
        lines.append("> Sinyalden sonraki fiyat değişimi yüzdeleri")
        lines.append("")

        # Genel ortalamalar
        periods = ["1h", "4h", "12h", "24h"]
        lines.append("### Genel Ortalamalar")
        lines.append("")
        lines.append("| Periyot | Ortalama % | Medyan % | Pozitif % | N |")
        lines.append("|" + "---|" * 5)
        for p in periods:
            vals = [fr.get(f"return_{p}") for fr in forward_returns if fr.get(f"return_{p}") is not None]
            if vals:
                avg = sum(vals) / len(vals)
                sorted_vals = sorted(vals)
                med = sorted_vals[len(sorted_vals) // 2]
                pos_pct = _pct(sum(1 for v in vals if v > 0), len(vals))
                lines.append(f"| {p} | {avg:+.3f}% | {med:+.3f}% | {pos_pct:.1f}% | {len(vals)} |")
        lines.append("")

        # Outcome bazında forward returns
        for outcome in ["WIN", "LOSS", "BREAKEVEN"]:
            outcome_frs = [fr for fr in forward_returns if fr.get("outcome") == outcome]
            if not outcome_frs:
                continue
            lines.append(f"### {outcome} İşlemlerin Forward Returns")
            lines.append("")
            lines.append("| Periyot | Ortalama % | N |")
            lines.append("|---|---|---|")
            for p in periods:
                vals = [fr.get(f"return_{p}") for fr in outcome_frs if fr.get(f"return_{p}") is not None]
                if vals:
                    avg = sum(vals) / len(vals)
                    lines.append(f"| {p} | {avg:+.3f}% | {len(vals)} |")
            lines.append("")

    # ── Strateji Önerileri ──
    lines.append("## 💡 Veriye Dayalı Strateji Önerileri")
    lines.append("")

    # En iyi ve en kötü setup type
    _, setup_results = analyze_by_dimension(trades, lambda t: t.get("setup_type", "?"), "Setup Type")
    if setup_results:
        best = max(setup_results, key=lambda r: r["avg_r"])
        worst = min(setup_results, key=lambda r: r["avg_r"])
        lines.append(f"- **En iyi strateji:** {best['key']} (Ort. R: {best['avg_r']:+.2f}, WR: {best['win_rate']}%, N={best['total']})")
        lines.append(f"- **En kötü strateji:** {worst['key']} (Ort. R: {worst['avg_r']:+.2f}, WR: {worst['win_rate']}%, N={worst['total']})")

    # Skor eşiği önerisi
    _, score_results = analyze_by_dimension(trades, lambda t: score_range(t.get("score", 0)), "Skor")
    profitable_ranges = [r for r in score_results if r["avg_r"] > 0]
    if profitable_ranges:
        lines.append(f"- **Kârlı skor aralıkları:** {', '.join(r['key'] for r in profitable_ranges)}")
    else:
        lines.append("- ⚠️ Hiçbir skor aralığı henüz kârlı değil — daha fazla Liq Sweep verisi gerekiyor")

    # Yeterlilik uyarısı
    liq_sweep_count = sum(1 for t in trades if t.get("setup_type") == "Liq Sweep")
    lines.append(f"\n> **Veri Yeterliliği:** Liq Sweep: {liq_sweep_count} işlem. "
                 f"İstatistiksel güvenilirlik için 50+ işlem gerekli. "
                 f"Bot çalışmaya devam ettikçe veri birikecektir.")
    lines.append("")

    return "\n".join(lines)


def print_summary(trades):
    """Terminal için kısa özet yazdırır."""
    wins = sum(1 for t in trades if t.get("outcome") == "WIN")
    losses = sum(1 for t in trades if t.get("outcome") == "LOSS")
    be = sum(1 for t in trades if t.get("outcome") == "BREAKEVEN")
    total_r = sum(float(t.get("r_multiple", 0.0)) for t in trades)
    decisive = wins + losses

    print(f"\n{'═' * 55}")
    print(f"  📊 SİNYAL İSTİHBARAT ÖZETİ")
    print(f"{'═' * 55}")
    print(f"  Toplam İşlem: {len(trades)}")
    print(f"  WIN: {wins} | LOSS: {losses} | BE: {be}")
    print(f"  Win Rate (Kesin): {_pct(wins, decisive):.1f}% ({decisive} işlem)")
    print(f"  Toplam R: {total_r:+.1f}R | Ortalama R: {total_r / len(trades):+.2f}R")

    # Setup bazında
    print(f"\n  {'─' * 50}")
    print(f"  Setup Type Bazında:")
    _, setup_results = analyze_by_dimension(trades, lambda t: t.get("setup_type", "?"), "Setup")
    for r in setup_results:
        marker = "✅" if r["avg_r"] > 0 else "❌"
        print(f"    {marker} {r['key']:<20} {r['total']:>3} işlem | WR={r['win_rate']:>5.1f}% | R={r['total_r']:>+6.1f} | Ort={r['avg_r']:>+.2f}")

    print(f"{'═' * 55}\n")


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Signal Analyzer — Sinyal İstihbarat Motoru")
    parser.add_argument("--summary", action="store_true", help="Kısa özet yazdır")
    parser.add_argument("--reconstruct", "--scan-alerted", action="store_true", help="alerted.json içerisindeki geçmiş sinyalleri sırayla geriye doğru tarayıp zenginleştirerek sonuçları kaydeder")
    parser.add_argument("--deep-scan", action="store_true", help="Binance Futures'taki en hacimli semboller için geriye dönük 30 günlük derin sinyal taraması ve simülasyon yapar")
    parser.add_argument("--days", type=int, default=30, help="Derin geçmiş taramasında geriye bakılacak gün sayısı")
    parser.add_argument("--start-days", type=int, default=0, help="Derin geçmiş taramasında başlama günü ofseti (örn: 30 = geriye dönük 30..60. günler)")
    parser.add_argument("--retroactive", action="store_true", help="Geçmiş sinyallerin forward return analizi")
    parser.add_argument("--force", action="store_true", help="Zaten kaydedilmiş sembolleri de yeniden incelemeye zorla")
    parser.add_argument("--max-symbols", type=int, default=100, help="Retroaktif analizde max sembol sayısı")
    parser.add_argument("--export", type=str, help="JSON rapor çıktısı dosya yolu")
    args = parser.parse_args()

    if args.deep_scan:
        deep_scan_historical(
            days=args.days,
            chunk_days=7,
            max_symbols=args.max_symbols,
            delay=0.15,
            force=args.force,
            start_days=args.start_days,
        )

    if args.reconstruct:
        reconstruct_and_evaluate_alerted(
            SETTINGS.ALERTED_FILE,
            max_symbols=args.max_symbols,
            delay=0.15,
            force=args.force,
        )

    trades = _load_json(SETTINGS.TRADE_HISTORY_FILE)
    signal_log = _load_json(SETTINGS.SIGNAL_LOG_FILE)

    if not trades:
        print("❌ Hiç kapanmış işlem bulunamadı (trade_history.json boş)")
        return

    if args.summary:
        print_summary(trades)
        return

    # Retroaktif analiz
    forward_returns = None
    if args.retroactive:
        forward_returns = retroactive_forward_returns(
            SETTINGS.ALERTED_FILE,
            max_symbols=args.max_symbols,
            delay=0.15,
        )
        # Sonuçları diske kaydet
        retro_path = "state/retroactive_analysis.json"
        try:
            with open(retro_path, "w", encoding="utf-8") as f:
                json.dump(forward_returns, f, indent=2, ensure_ascii=False)
            log.info(f"📁 Retroaktif sonuçlar kaydedildi: {retro_path}")
        except Exception as e:
            log.warning(f"Retroaktif sonuç kaydetme hatası: {e}")

    # Tam rapor oluştur
    report = generate_report(trades, signal_log=signal_log, forward_returns=forward_returns)

    # Terminale yazdır
    print(report)

    # state/analysis_report.md dosyasına yaz
    report_path = "state/analysis_report.md"
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n📁 Rapor kaydedildi: {report_path}")
    except Exception as e:
        print(f"⚠️ Rapor kaydetme hatası: {e}")

    # JSON export
    if args.export:
        export_data = {
            "generated_at": datetime.now().isoformat(),
            "total_trades": len(trades),
            "setup_types": analyze_by_dimension(trades, lambda t: t.get("setup_type", "?"), "Setup Type")[1],
            "directions": analyze_by_dimension(trades, lambda t: t.get("direction", "?"), "Direction")[1],
            "score_ranges": analyze_by_dimension(trades, lambda t: score_range(t.get("score", 0)), "Score")[1],
            "indicators": analyze_indicators(trades),
        }
        if forward_returns:
            export_data["forward_returns"] = forward_returns
        try:
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False)
            print(f"📁 JSON export kaydedildi: {args.export}")
        except Exception as e:
            print(f"⚠️ JSON export hatası: {e}")


if __name__ == "__main__":
    main()
