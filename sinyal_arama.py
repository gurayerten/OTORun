"""
sinyal_arama.py — Geçmiş Sinyal Toplama Modülü (feedback arşivi)

state/tarama.json içindeki coinleri, canlı botun KULLANDIĞI yapıyı değiştirmeden
(SweepEngine + build_trade_plan + StateManager'ın SL/TP/BE/trailing çıkış
kuralları) son N günün (varsayılan 60) kapanmış mumları üzerinde tarar ve
üretilen TÜM sinyalleri, her biri bağımsız simüle edilmiş sonucuyla birlikte
state/taramasonuc.json'a yazar.

Amaç: geçmiş sinyalleri toplayıp kalitelerini ölçmek ve strateji iyileştirme
kararları için feedback kaydı biriktirmek.

Tasarım kuralları:
  - Sıralı (paralel değil) tarama; exchange._get üzerindeki paylaşılan
    RateLimiter + 429/418 exponential backoff sayesinde rate-limit güvenlidir.
  - Semboller arası kısa pause (SYMBOL_PAUSE_SEC).
  - Canlı bot dosyalarına/state'e DOKUNMAZ; sadece tarama.json okur,
    taramasonuc.json yazar. Her sembol sonrası atomik kayıt (crash-safe).
  - Her sinyal ayrı kaydedilir (tek pozisyon kısıtı yok): maksimum feedback.
  - Geçmişte funding/orderbook verisi bulunmadığından SweepEngine, backtester
    ile aynı sade parametrelerle çalışır (htf_bias=0, funding_info=None);
    canlı bottaki bu bileşenler skorda küçük fark yaratabilir.

Kullanım:
    python sinyal_arama.py                      # 60 gün, tarama.json'daki tüm coinler
    python sinyal_arama.py --days 90            # farklı geçmiş pencere
    python sinyal_arama.py --coins BTCUSDT ...  # sadece belirli coinler
    python sinyal_arama.py --resume              # yarım kalan taramayı devam ettir
    python sinyal_arama.py --out state/test.json # farklı çıktı dosyası
"""
import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, fields as dc_fields
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SETTINGS
from exchange import get_raw_klines, klines_to_dataframe
from logger import get_logger
from risk import build_trade_plan
from strategy import SweepEngine, atr

log = get_logger("sinyal_arama")

TARAMA_FILE = "state/tarama.json"
SONUC_FILE = "state/taramasonuc.json"
SCAN_DAYS = 21
MIN_BARS = 200          # backtester ile aynı minimum veri şartı
PAGE_LIMIT = 1000       # Binance klines sayfa boyutu
SYMBOL_PAUSE_SEC = 0.3  # semboller arası ek güvence (RateLimiter'in üstünde)
STALE_DATA_DAYS = 1.0   # son bar bu kadar günden eskiyse sembol "stale" işaretlenir

BARS_PER_DAY = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}
BAR_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def _json_default(o):
    """JSON'a serileşemeyen tipleri (numpy vb.) güvenli temel tiplere çevirir."""
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def _save_json(path, data):
    """Atomik JSON yazımı (tmp + os.replace); her sembol sonrası çağrılır."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=_json_default, ensure_ascii=False)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except (PermissionError, OSError):
            if attempt < 4:
                time.sleep(0.05 * (attempt + 1))


def _settings_snapshot():
    """Taramanın yapıldığı andaki skalar strateji ayarları (tekrar üretilebilirlik).
    Güvenlik: TOKEN içeren alanlar (Telegram vb.) sonuca ASLA yazılmaz."""
    snap = {}
    for f in dc_fields(SETTINGS):
        if "TOKEN" in f.name or f.name.startswith("TELEGRAM"):
            continue
        v = getattr(SETTINGS, f.name)
        if isinstance(v, (bool, int, float, str)):
            snap[f.name] = v
    return snap


def _settings_hash(snap):
    return hashlib.sha1(json.dumps(snap, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def load_coins(path):
    """state/tarama.json'dan coin listesini okur."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Coin listesi bulunamadı: {path}")
    with open(path, "r", encoding="utf-8") as f:
        coins = json.load(f)
    if not isinstance(coins, list) or not coins:
        raise ValueError(f"{path} bozuk veya boş bir liste içermiyor.")
    return [c for c in coins if isinstance(c, str) and c.strip()]


def fetch_history(symbol, interval, days):
    """Son `days` günün kapanmış mumlarını geriye doğru sayfalayarak çeker.

    exchange.get_klines_paginated 5m aralığı için yanlış bar/gün varsaydığından
    (96) burada doğru BARS_PER_DAY haritasıyla kendi sayfalaması yapılır.
    İstekler exchange._get üzerinden gider: paylaşılan RateLimiter (min 0.08s)
    ve 429/418'de exponential backoff otomatik çalışır.
    """
    bpd = BARS_PER_DAY.get(interval, 288)
    needed = days * bpd + 1  # +1: son satır henüz kapanmamış mum olabilir
    out = []
    end_time = None
    for _ in range((needed // PAGE_LIMIT) + 3):  # güvenlik tavanı
        if len(out) >= needed:
            break
        try:
            extra = {"endTime": end_time} if end_time is not None else {}
            raw = get_raw_klines(symbol, interval, PAGE_LIMIT, **extra)
        except Exception as e:
            log.warning(f"{symbol}: veri çekme hatası: {e}")
            return None
        if not raw:
            break
        out = raw + out
        end_time = raw[0][0] - 1
        if len(raw) < PAGE_LIMIT:
            break
    if not out:
        return None
    df = klines_to_dataframe(out)
    if df is None or len(df) < 2:
        return None
    # Canlı bot gibi: son (henüz kapanmamış) mumu at
    return df.tail(needed).iloc[:-1].reset_index(drop=True)


def simulate_outcome(close, high, low, times, i, plan, entry_exec):
    """Sinyal barından itibaren ileriye bağımsız simülasyon.

    state_manager.update_on_bar ile BİREBİR aynı kurallar:
      - Aynı barda SL çarpmışsa TP bayrakları kurulmaz (TP1/TP2 güncellemesi atlanır)
      - TP1 -> SL, entry ± BE_LEVEL_R×risk seviyesine çekilir (yarım R kilidi)
      - TP2 -> SL, TP1 seviyesine yükseltilir (sadece iyiyse)
      - Trailing stop: activation_r sonrası extreme'den distance_r geride
      - Time-stop: MAX_HOLD_BARS sonunda kâr TIME_STOP_MIN_R altındaysa kapanış
      - Çıkış: SL, TP3 veya time-stop; r_multiple ÇIKIŞ FİYATINDAN hesaplanır
        (eski 0/−1 yaklaşımı değil — BE_LEVEL_R ve trailing kilitleri yansır)
    Ek olarak objektif metrikler: r_realized (geometrik gerçeklik) ve
    net_pnl_pct (slippage + komisyon dahil).
    """
    d = plan.direction
    sl = plan.sl
    entry = plan.entry
    risk = abs(entry - plan.sl)
    tp1_lvl, tp2_lvl, tp3_lvl = plan.tp1, plan.tp2, plan.tp3
    be_lock = risk * SETTINGS.BE_LEVEL_R if risk > 0 else 0.0

    tp1_flag = tp2_flag = tp3_flag = False
    be_active = False
    trailing_active = False
    extreme = entry
    same_bar_sl_tp3 = False

    activation = risk * SETTINGS.TRAILING_STOP_ACTIVATION_R if risk > 0 else 0.0
    trail_dist = risk * SETTINGS.TRAILING_STOP_DISTANCE_R if risk > 0 else 0.0

    def _time_str(idx, j):
        try:
            return str(idx.iloc[j])
        except AttributeError:
            return str(idx[j])

    n = len(close)
    for j in range(i + 1, n):
        h, l = high[j], low[j]
        sl_hit = (l <= sl) if d == 1 else (h >= sl)
        t1 = (h >= tp1_lvl) if d == 1 else (l <= tp1_lvl)
        t2 = (h >= tp2_lvl) if d == 1 else (l <= tp2_lvl)
        t3 = (h >= tp3_lvl) if d == 1 else (l <= tp3_lvl)

        if t1 and not tp1_flag and not sl_hit:
            tp1_flag = True
            if SETTINGS.USE_BREAK_EVEN and not be_active:
                lock = entry + be_lock if d == 1 else entry - be_lock
                if (d == 1 and lock > sl) or (d == -1 and lock < sl):
                    sl = lock
                    be_active = True
        if t2 and not tp2_flag and not sl_hit:
            tp2_flag = True
            if (d == 1 and tp1_lvl > sl) or (d == -1 and tp1_lvl < sl):
                sl = tp1_lvl
        if t3 and not tp3_flag and not sl_hit:
            tp3_flag = True

        if SETTINGS.USE_TRAILING_STOP and not sl_hit and risk > 0:
            if d == 1:
                if h > extreme:
                    extreme = h
                if extreme >= entry + activation:
                    trailing_active = True
                    new_sl = extreme - trail_dist
                    if new_sl > sl:
                        sl = new_sl
            else:
                if l < extreme:
                    extreme = l
                if extreme <= entry - activation:
                    trailing_active = True
                    new_sl = extreme + trail_dist
                    if new_sl < sl:
                        sl = new_sl

        # --- Time-stop: MAX_HOLD_BARS doldu ve kâr eşiğin altında → kapat ---
        time_stop = False
        if not sl_hit and not t3 and (j - i) >= SETTINGS.MAX_HOLD_BARS and risk > 0:
            r_now = ((close[j] - entry) * d) / risk
            if r_now < SETTINGS.TIME_STOP_MIN_R:
                time_stop = True

        if sl_hit or t3 or time_stop:
            if time_stop:
                exit_price = float(close[j])
                reason = "TIME"
            elif t3:
                exit_price = tp3_lvl
                reason = "TP3"
            else:
                exit_price = sl
                reason = "BE" if be_active else "SL"
            same_bar_sl_tp3 = sl_hit and t3
            r = ((exit_price - entry) * d / risk) if risk > 0 else 0.0
            if abs(r) < 0.1 and reason != "TP3":
                outcome = "BREAKEVEN"
            elif r > 0:
                outcome = "WIN"
            else:
                outcome = "LOSS"
            return _finish_outcome(plan, entry_exec, exit_price, _time_str(times, j),
                                   j - i, outcome, reason, r, risk,
                                   tp1_flag, tp2_flag, tp3_flag, be_active,
                                   trailing_active, same_bar_sl_tp3)

    # Veri sonuna kadar çıkış yok -> açık pozisyon (mark-to-market)
    exit_price = float(close[n - 1])
    r = ((exit_price - entry) * d / risk) if risk > 0 else 0.0
    return _finish_outcome(plan, entry_exec, exit_price, _time_str(times, n - 1),
                           n - 1 - i, "OPEN", "OPEN", r, risk,
                           tp1_flag, tp2_flag, tp3_flag, be_active,
                           trailing_active, same_bar_sl_tp3)


def _finish_outcome(plan, entry_exec, exit_price, exit_time, bars_held,
                    outcome, reason, r_multiple, risk,
                    tp1_flag, tp2_flag, tp3_flag, be_active,
                    trailing_active, same_bar_sl_tp3):
    d = plan.direction
    r_realized = ((exit_price - plan.entry) * d / risk) if risk > 0 else 0.0
    gross = (exit_price - entry_exec) * d
    fee = (entry_exec + exit_price) * SETTINGS.TAKER_FEE
    net_pnl_pct = ((gross - fee) / entry_exec * 100) if entry_exec else 0.0
    # (Analiz) BE/trailing'in gömdüğü R: ulaşılan TP seviyesinin R değeriyle
    # gerçekleştirilen R arasındaki fark (Q: TP2 dolu +2.83R → 0.17R geri verilmiş).
    r_flag = (SETTINGS.TP3_R if tp3_flag else SETTINGS.TP2_R if tp2_flag
              else SETTINGS.TP1_R if tp1_flag else 0.0)
    r_given_back = round(max(0.0, r_flag - r_realized), 4)
    return {
        "outcome": outcome,
        "reason": reason,
        "exit_price": float(exit_price),
        "exit_time": exit_time,
        "bars_held": int(bars_held),
        "r_multiple": round(float(r_multiple), 4),
        "r_realized": round(float(r_realized), 4),
        "r_given_back": r_given_back,
        "net_pnl_pct": round(float(net_pnl_pct), 4),
        "tp1_reached": bool(tp1_flag),
        "tp2_reached": bool(tp2_flag),
        "tp3_reached": bool(tp3_flag),
        "be_active": bool(be_active),
        "trailing_active": bool(trailing_active),
        "same_bar_sl_tp3": bool(same_bar_sl_tp3),
    }


def scan_symbol(symbol, days):
    """Tek sembolü tarar: (sembol meta'sı, sinyal kayıtları) döner."""
    t0 = time.time()
    df = fetch_history(symbol, SETTINGS.INTERVAL, days)
    if df is None or len(df) < MIN_BARS:
        meta = {
            "symbol": symbol, "ok": False,
            "bars": 0 if df is None else int(len(df)),
            "error": "veri çekilemedi / yetersiz bar",
            "signals": 0, "duration_sec": round(time.time() - t0, 1),
        }
        return meta, []

    # Delist/durdurulmus sembol korumasi: son bar cok eskiyse veri son N takvim
    # gunu degil, sembolun kapanmadan onceki son N gunudur -> isaretle
    last_epoch = int(df["open_time"].values[-1].astype("datetime64[s]").astype("int64"))
    age_days = (time.time() - last_epoch) / 86400.0
    stale = age_days > STALE_DATA_DAYS

    if stale:
        # (Analiz) Stale sembolun eski verisi sahte sinyal uretiyor (EOS: 2025
        # verisinden A+ kayip sinyal geldi, istatistikleri kirletti) -> sinyal
        # uretimi tamamen atlanir; sembol yalnizca meta olarak kaydedilir.
        log.warning(f"{symbol}: STALE VERI (son bar {age_days:.0f} gun once) -- sinyal uretimi atlandi")
        meta = {
            "symbol": symbol, "ok": True,
            "bars": int(len(df)),
            "first_bar": str(df["open_time"].iloc[0]),
            "last_bar": str(df["open_time"].iloc[-1]),
            "stale": True,
            "stale_note": (f"son bar {age_days:.0f} gun once -- sembol muhtemelen delist/durdurulmus, "
                           f"sinyal uretimi ATLANDI"),
            "signals": 0, "wins": 0, "losses": 0, "breakevens": 0, "open": 0,
            "win_rate": 0.0, "total_r": 0.0, "total_r_realized": 0.0,
            "duration_sec": round(time.time() - t0, 1),
        }
        return meta, []

    engine = SweepEngine()
    atr_v = atr(df, SETTINGS.ATR_LEN).values
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    times = df["open_time"]

    signals = []
    for setup in engine.iter_signals(df):
        i = setup.bar_index
        if np.isnan(atr_v[i]) or atr_v[i] <= 0:
            continue
        entry = float(close[i])
        slip = entry * SETTINGS.SLIPPAGE
        entry_exec = entry + slip if setup.direction == 1 else entry - slip
        plan = build_trade_plan(setup.direction, entry, setup.anchor_extreme,
                                float(atr_v[i]), regime=setup.market_regime)
        outcome = simulate_outcome(close, high, low, times, i, plan, entry_exec)
        risk = abs(plan.entry - plan.sl)
        signals.append({
            "symbol": symbol,
            "bar_time": str(times.iloc[i]),
            "bar_index": int(i),
            "direction": "LONG" if setup.direction == 1 else "SHORT",
            "score": round(float(setup.score), 2),
            "grade": setup.grade,
            "setup_type": setup.setup_type,
            "market_regime": setup.market_regime,
            "plan": {
                "entry": float(plan.entry),
                "sl": float(plan.sl),
                "tp1": float(plan.tp1),
                "tp2": float(plan.tp2),
                "tp3": float(plan.tp3),
                "risk_distance": float(risk),
                "entry_exec": float(entry_exec),
            },
            "outcome": outcome,
            "setup": asdict(setup),
        })

    gs = _group_stats(signals)
    meta = {
        "symbol": symbol, "ok": True,
        "bars": int(len(df)),
        "first_bar": str(times.iloc[0]),
        "last_bar": str(times.iloc[-1]),
        "stale": bool(stale),
        "stale_note": (f"son bar {age_days:.0f} gun once -- sembol muhtemelen delist/durdurulmus, "
                       f"tarama sembolun KAPANMADAN ONCEKI son {days} gununu kapsar") if stale else "",
        "signals": gs["total"],
        "wins": gs["wins"], "losses": gs["losses"],
        "breakevens": gs["breakevens"], "open": gs["open"],
        "win_rate": gs["win_rate"], "total_r": gs["total_r"],
        "total_r_realized": gs["total_r_realized"],
        "duration_sec": round(time.time() - t0, 1),
    }
    return meta, signals


def _group_stats(items):
    """Sinyal listesi için WIN/LOSS/BE/OPEN istatistikleri.
    Korelasyon kapısıyla düşürülen (gated) sinyaller istatistiklere girmez.
    r_realized (birincil KPI) ve r_given_back (BE/trailing'in gömdüğü R) eklenir."""
    wins = losses = be = open_ = 0
    total_r = 0.0
    total_r_realized = 0.0
    total_r_given_back = 0.0
    for s in items:
        if isinstance(s, dict) and s.get("gated"):
            continue
        o = s["outcome"] if "outcome" in s else s  # hem sinyal hem outcome kayıtları
        total_r += float(o["r_multiple"])
        total_r_realized += float(o.get("r_realized", o["r_multiple"]))
        total_r_given_back += float(o.get("r_given_back", 0.0))
        if o["outcome"] == "WIN":
            wins += 1
        elif o["outcome"] == "LOSS":
            losses += 1
        elif o["outcome"] == "BREAKEVEN":
            be += 1
        else:
            open_ += 1
    n = wins + losses + be + open_
    closed = wins + losses + be
    decisive = wins + losses
    return {
        "total": n,
        "wins": wins, "losses": losses, "breakevens": be, "open": open_,
        "win_rate": round(wins / closed * 100, 2) if closed else 0.0,
        "decisive_win_rate": round(wins / decisive * 100, 2) if decisive else 0.0,
        "total_r": round(total_r, 2),
        "avg_r": round(total_r / n, 4) if n else 0.0,
        "total_r_realized": round(total_r_realized, 2),
        "avg_r_realized": round(total_r_realized / n, 4) if n else 0.0,
        "total_r_given_back": round(total_r_given_back, 2),
    }


def _group_by(signals, key):
    groups = {}
    for s in signals:
        if isinstance(s, dict) and s.get("gated"):
            continue
        groups.setdefault(s[key], []).append(s)
    return {k: _group_stats(v) for k, v in sorted(groups.items(), key=str)}


def _apply_correlation_gate(signals):
    """(Analiz) Portföy korelasyon kapısı.

    28 Ağu 14:00 olayı: 5 paritede eşzamanlı A/A+ SHORT sinyali, 5/5 SL
    (tek anda -5R). Aynı yönde, CORRELATION_GATE_WINDOW_BARS bar yakınlıkta
    >= CORRELATION_CLUSTER_MIN sinyal varsa küme yalnız en yüksek skorlu
    MAX_SAME_DIR_PER_CYCLE tanesine indirilir.

    Düşürülen sinyaller dosyada tutulur (gated=True, gate_reason etiketli)
    ama istatistiklere girmez. Düşürülen sinyal sayısını döner.
    """
    for s in signals:
        s.setdefault("gated", False)
        s.setdefault("gate_reason", "")
    if not SETTINGS.CORRELATION_GATE_ENABLED:
        return 0

    window_sec = SETTINGS.CORRELATION_GATE_WINDOW_BARS * BAR_SECONDS.get(SETTINGS.INTERVAL, 300)
    dropped = 0
    for direction in ("LONG", "SHORT"):
        group = sorted((s for s in signals
                        if s.get("direction") == direction and not s.get("gated")),
                       key=lambda s: s["bar_time"])
        cluster = []
        prev_t = None

        def _flush(cluster):
            nonlocal dropped
            if len(cluster) >= SETTINGS.CORRELATION_CLUSTER_MIN:
                cluster.sort(key=lambda s: -float(s["score"]))
                for s in cluster[SETTINGS.MAX_SAME_DIR_PER_CYCLE:]:
                    s["gated"] = True
                    s["gate_reason"] = "correlation_cluster"
                    dropped += 1

        for s in group:
            try:
                t = datetime.fromisoformat(s["bar_time"])
            except (ValueError, TypeError):
                continue
            if prev_t is not None and (t - prev_t).total_seconds() > window_sec:
                _flush(cluster)
                cluster = []
            cluster.append(s)
            prev_t = t
        _flush(cluster)
    return dropped


def _recompute_symbol_metas(results):
    """Korelasyon kapısı sonrası sembol metalarını surviving sinyallerden yeniden hesaplar."""
    by_symbol = {}
    for s in results["signals"]:
        if not s.get("gated"):
            by_symbol.setdefault(s["symbol"], []).append(s)
    for meta in results["symbols"]:
        if not meta.get("ok"):
            continue
        gs = _group_stats(by_symbol.get(meta["symbol"], []))
        meta["signals"] = gs["total"]
        meta["wins"] = gs["wins"]
        meta["losses"] = gs["losses"]
        meta["breakevens"] = gs["breakevens"]
        meta["open"] = gs["open"]
        meta["win_rate"] = gs["win_rate"]
        meta["total_r"] = gs["total_r"]
        meta["total_r_realized"] = gs["total_r_realized"]


def build_summary(signals, symbols_meta):
    ok = [m for m in symbols_meta if m.get("ok")]
    failed = [m for m in symbols_meta if not m.get("ok")]
    overall = _group_stats(signals)
    avg_pnl = (round(sum(s["outcome"]["net_pnl_pct"] for s in signals
                         if not s.get("gated")) /
                      max(1, overall["total"]), 4) if signals else 0.0)
    return {
        "symbols_total": len(symbols_meta),
        "symbols_ok": len(ok),
        "symbols_failed": len(failed),
        "failed_symbols": [m["symbol"] for m in failed],
        **overall,
        "avg_net_pnl_pct": avg_pnl,
        "correlation_dropped": sum(1 for s in signals if isinstance(s, dict) and s.get("gated")),
        "by_direction": _group_by(signals, "direction"),
        "by_grade": _group_by(signals, "grade"),
        "by_setup_type": _group_by(signals, "setup_type"),
        "by_regime": _group_by(signals, "market_regime"),
    }


def _empty_results(days, snap, snap_hash):
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scan_days": days,
        "interval": SETTINGS.INTERVAL,
        "settings_hash": snap_hash,
        "settings_snapshot": snap,
        "summary": {},
        "symbols": [],
        "signals": [],
    }


def _load_previous(out_path, days, snap_hash):
    """--resume için önceki sonuç dosyasını doğrulayıp yükler; uyumsuzsa None."""
    if not os.path.exists(out_path):
        return None
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            prev = json.load(f)
        if (prev.get("scan_days") != days
                or prev.get("interval") != SETTINGS.INTERVAL
                or prev.get("settings_hash") != snap_hash
                or not isinstance(prev.get("signals"), list)
                or not isinstance(prev.get("symbols"), list)):
            log.warning("Önceki taramasonuc.json güncel taramayla uyumsuz -- sıfırdan başlanıyor.")
            return None
        return prev
    except Exception as e:
        log.warning(f"Önceki sonuç okunamadı ({e}) -- sıfırdan başlanıyor.")
        return None


def run(days, coins, out_path, resume):
    snap = _settings_snapshot()
    snap_hash = _settings_hash(snap)

    results = _empty_results(days, snap, snap_hash)
    already = set()
    if resume:
        prev = _load_previous(out_path, days, snap_hash)
        if prev:
            results = prev
            already = {m["symbol"] for m in prev["symbols"] if m.get("ok")}
            log.info(f"Resume: {len(already)} sembol atlanacak, "
                     f"{len(prev['signals'])} sinyal korunacak.")

    todo = [c for c in coins if c not in already]
    skipped = len(coins) - len(todo)
    bars_per_coin = BARS_PER_DAY.get(SETTINGS.INTERVAL, 288) * days
    log.info(f"Tarama başlıyor: {len(todo)} sembol x {days} gün "
             f"({SETTINGS.INTERVAL}, sembol başına ~{bars_per_coin} bar)"
             + (f" | {skipped} sembol resume ile atlandı" if skipped else ""))

    t_start = time.time()
    for idx, sym in enumerate(todo, 1):
        try:
            meta, signals = scan_symbol(sym, days)
        except KeyboardInterrupt:
            log.info("Kullanıcı durdurdu -- ara sonuçlar kaydedildi.")
            _save_json(out_path, results)
            return results
        except Exception as e:
            log.warning(f"{sym}: tarama hatası: {e}")
            meta = {"symbol": sym, "ok": False, "bars": 0, "error": str(e),
                    "signals": 0, "duration_sec": 0.0}
            signals = []

        results["symbols"].append(meta)
        results["signals"].extend(signals)
        results["summary"] = build_summary(results["signals"], results["symbols"])
        _save_json(out_path, results)

        if meta["ok"]:
            log.info(f"[{idx}/{len(todo)}] {sym}: {meta['bars']} bar, "
                     f"{meta['signals']} sinyal | WR %{meta['win_rate']} | "
                     f"sınıflandı {meta['total_r']:+.1f}R / gerçekleşti "
                     f"{meta.get('total_r_realized', 0.0):+.1f}R | {meta['duration_sec']} sn")
            if meta.get("stale"):
                log.warning(f"{sym}: STALE VERI -- {meta['stale_note']}")
        else:
            log.info(f"[{idx}/{len(todo)}] {sym}: HATA ({meta.get('error', '?')})")

        if idx < len(todo):
            time.sleep(SYMBOL_PAUSE_SEC)

    results["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # --- (Analiz) Portföy korelasyon kapısı: tüm semboller bittiğinde uygula ---
    dropped = _apply_correlation_gate(results["signals"])
    if dropped:
        log.info(f"🔗 Korelasyon kapısı: {dropped} sinyal correlation_cluster olarak düşürüldü "
                 f"(dosyada gated=True olarak korunuyor)")
        _recompute_symbol_metas(results)

    results["summary"] = build_summary(results["signals"], results["symbols"])
    _save_json(out_path, results)

    s = results["summary"]
    elapsed = time.time() - t_start
    log.info(f"Bitti ({elapsed / 60:.1f} dk) -> {out_path}")
    log.info(f"Toplam: {s['symbols_ok']}/{s['symbols_total']} sembol, "
             f"{s['total']} sinyal | WR %{s['win_rate']} "
             f"(kararlı: %{s['decisive_win_rate']}) | Sınıflandırılmış {s['total_r']:+.2f}R | "
             f"GERÇEKLEŞEN {s['total_r_realized']:+.2f}R (birincil KPI) | "
             f"Geri verilen: {s['total_r_given_back']:.2f}R | Açık: {s['open']}"
             + (f" | Kapıdan düşen: {s['correlation_dropped']}" if s.get("correlation_dropped") else ""))
    if s["by_grade"]:
        log.info("Grade kırılımı: " + " | ".join(
            f"{g}: {v['total']} sinyal, WR %{v['win_rate']}, gerçekleşen {v['total_r_realized']:+.1f}R"
            for g, v in s["by_grade"].items()))
    return results


def main():
    parser = argparse.ArgumentParser(
        description="tarama.json coinlerini geçmişe dönük SweepEngine taraması (feedback arşivi)")
    parser.add_argument("--days", type=int, default=SCAN_DAYS,
                        help=f"Kaç günlük geçmiş taranacak (varsayılan {SCAN_DAYS})")
    parser.add_argument("--coins", nargs="+", default=None,
                        help="Sadece bu coinler taransın (varsayılan: tarama.json)")
    parser.add_argument("--coins-file", default=TARAMA_FILE,
                        help=f"Coin listesi dosyası (varsayılan {TARAMA_FILE})")
    parser.add_argument("--out", default=SONUC_FILE,
                        help=f"Sonuç dosyası (varsayılan {SONUC_FILE})")
    parser.add_argument("--resume", action="store_true",
                        help="Önceki taramayı devam ettir (aynı days/ayarlar)")
    args = parser.parse_args()

    coins = args.coins or load_coins(args.coins_file)
    if args.days < 1:
        raise SystemExit("--days en az 1 olmalı.")

    log.info(f"Sinyal arama modülü: {len(coins)} coin, {args.days} gün, "
             f"çıkış: {args.out}")
    run(args.days, coins, args.out, args.resume)


if __name__ == "__main__":
    main()
