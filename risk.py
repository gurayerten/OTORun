from dataclasses import dataclass

from config import SETTINGS


@dataclass
class TradePlan:
    direction: int
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float


def build_trade_plan(direction, entry, anchor_extreme, atr_value, regime="TRANSITION"):
    """Pine'daki risk bloğunun birebir karşılığı: SL, süpürülen fitilin
    `SL_BUFFER_ATR` kadar ötesine konur; minimum mesafe garantisi vardır;
    TP'ler risk mesafesinin R katlarıdır.
    Ek olarak SL mesafesi yüzde bazlı tavan/taban ile sınırlanır.
    
    #11 Dinamik TP/SL: Rejime göre TP/SL çarpanları otomatik ayarlanır.
    """
    # #11 Dinamik TP/SL çarpanları
    if SETTINGS.DYNAMIC_TP_SL_ENABLED and regime in SETTINGS.DYNAMIC_TP_SL_MAP:
        tp1_r, tp2_r, tp3_r, sl_buf = SETTINGS.DYNAMIC_TP_SL_MAP[regime]
    else:
        tp1_r = SETTINGS.TP1_R
        tp2_r = SETTINGS.TP2_R
        tp3_r = SETTINGS.TP3_R
        sl_buf = SETTINGS.SL_BUFFER_ATR

    buf = sl_buf * atr_value
    min_dist = SETTINGS.MIN_SL_DISTANCE_ATR * atr_value

    # Yüzde bazlı SL sınırları
    min_dist_pct = entry * (SETTINGS.MIN_SL_DISTANCE_PCT / 100.0)
    max_dist_pct = entry * (SETTINGS.MAX_SL_DISTANCE_PCT / 100.0)

    if direction == 1:
        dist = max(entry - (anchor_extreme - buf), min_dist, min_dist_pct)
        dist = min(dist, max_dist_pct)
        raw_sl = entry - dist
        return TradePlan(
            direction, entry, raw_sl,
            entry + dist * tp1_r,
            entry + dist * tp2_r,
            entry + dist * tp3_r,
        )
    else:
        dist = max((anchor_extreme + buf) - entry, min_dist, min_dist_pct)
        dist = min(dist, max_dist_pct)
        raw_sl = entry + dist
        return TradePlan(
            direction, entry, raw_sl,
            entry - dist * tp1_r,
            entry - dist * tp2_r,
            entry - dist * tp3_r,
        )
