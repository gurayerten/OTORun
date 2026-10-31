import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional

from config import SETTINGS
from logger import get_logger

log = get_logger("strategy")


# ──────────────────────────────────────────
#  RSI Divergence tespiti
# ──────────────────────────────────────────

def rsi(series, length=14):
    """Standart RSI hesaplama."""
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _find_swing_lows(values, window=3):
    idxs = []
    for i in range(window, len(values) - window):
        if all(values[i] < values[i - j] for j in range(1, window + 1)) and \
           all(values[i] < values[i + j] for j in range(1, window + 1)):
            idxs.append(i)
    return idxs


def _find_swing_highs(values, window=3):
    idxs = []
    for i in range(window, len(values) - window):
        if all(values[i] > values[i - j] for j in range(1, window + 1)) and \
           all(values[i] > values[i + j] for j in range(1, window + 1)):
            idxs.append(i)
    return idxs


def compute_rsi_divergence_array(df, lookback=None, stride=None):
    """RSI divergence tespiti -- tam dizi. +1 bullish, -1 bearish, 0 yok.
    Swing noktalari bir kez hesaplanir; her barda sadece pencere icindeki
    son iki swing bisect ile bulunur (O(n log s))."""
    from bisect import bisect_left, bisect_right
    n = len(df) if df is not None else 0
    res = np.zeros(n, dtype=int)
    if df is None or n < 20: return res
    lookback = lookback or SETTINGS.RSI_DIVERGENCE_LOOKBACK
    stride = stride or getattr(SETTINGS, "RSI_DIVERGENCE_STRIDE", 1)
    tol = max(3, lookback // 5)
    rsi_vals = rsi(df["close"], SETTINGS.RSI_LENGTH).values
    lows = df["low"].values
    highs = df["high"].values

    # Tum swing noktalari tek geciste (window=3, _find_swing_* ile ayni)
    w = 3
    p_low_s = _find_swing_lows(lows, w)
    r_low_s = _find_swing_lows(rsi_vals, w)
    p_high_s = _find_swing_highs(highs, w)
    r_high_s = _find_swing_highs(rsi_vals, w)

    last_val = 0
    for i in range(lookback+10, n):
        if (i - (lookback+10)) % stride == 0:
            lb = min(lookback, i+1)
            lo = i - lb + 1 + w   # pencere-slice swing araligi: [i-lb+1+w, i-w]
            hi = i - w
            curr = 0

            def _last2(sw):
                a = bisect_left(sw, lo); b = bisect_right(sw, hi)
                if b - a >= 2: return sw[b-2], sw[b-1]
                return None

            lp2 = _last2(p_low_s); lr2 = _last2(r_low_s)
            if lp2 and lr2:
                p1, p2 = lp2; r1, r2 = lr2
                # tol karsilastirmasi slice-icindeki goreli indekslerle yapiliyordu;
                # mutlak farklar slice kaymasindan etkilenmez.
                if abs(p2 - r2) <= tol and abs(p1 - r1) <= tol:
                    if lows[p2] < lows[p1] and rsi_vals[r2] > rsi_vals[r1]:
                        curr = 1

            if curr == 0:
                hp2 = _last2(p_high_s); hr2 = _last2(r_high_s)
                if hp2 and hr2:
                    p1, p2 = hp2; r1, r2 = hr2
                    if abs(p2 - r2) <= tol and abs(p1 - r1) <= tol:
                        if highs[p2] > highs[p1] and rsi_vals[r2] < rsi_vals[r1]:
                            curr = -1
            last_val = curr
        res[i] = last_val
    return res


def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def atr(df, length):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def _wilder_smoothing(values, length):
    n = len(values)
    smoothed = np.zeros(n)
    if n == 0:
        return smoothed
    smoothed[0] = values[0]
    for i in range(1, n):
        smoothed[i] = smoothed[i-1] - (smoothed[i-1] / length) + values[i]
    return smoothed


def compute_adx_di(df, length=14):
    high = df["high"].values; low = df["low"].values; close = df["close"].values
    n = len(df)
    if n == 0:
        return np.zeros(n), np.zeros(n), np.zeros(n)
    prev_close = np.roll(close, 1); prev_close[0] = close[0]
    prev_high = np.roll(high, 1); prev_high[0] = high[0]
    prev_low = np.roll(low, 1); prev_low[0] = low[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    dm_plus_raw = high - prev_high; dm_minus_raw = prev_low - low
    dm_plus = np.where((dm_plus_raw > dm_minus_raw) & (dm_plus_raw > 0), dm_plus_raw, 0.0)
    dm_minus = np.where((dm_minus_raw > dm_plus_raw) & (dm_minus_raw > 0), dm_minus_raw, 0.0)
    smoothed_tr = _wilder_smoothing(tr, length)
    smoothed_dm_plus = _wilder_smoothing(dm_plus, length)
    smoothed_dm_minus = _wilder_smoothing(dm_minus, length)
    di_plus = np.zeros(n); di_minus = np.zeros(n)
    mask = smoothed_tr > 0
    di_plus[mask] = (smoothed_dm_plus[mask] / smoothed_tr[mask]) * 100.0
    di_minus[mask] = (smoothed_dm_minus[mask] / smoothed_tr[mask]) * 100.0
    dx = np.zeros(n); sum_di = di_plus + di_minus; mask2 = sum_di > 0
    dx[mask2] = (np.abs(di_plus[mask2] - di_minus[mask2]) / sum_di[mask2]) * 100.0
    adx = pd.Series(dx).rolling(window=length).mean().values.copy()
    adx[np.isnan(adx)] = 0.0
    return adx, di_plus, di_minus


def compute_supertrend(df, period=10, multiplier=3.0, change_atr=True):
    high = df["high"].values; low = df["low"].values; close = df["close"].values
    n = len(df)
    if n == 0:
        return np.ones(n, dtype=int), np.zeros(n), np.zeros(n)
    src = (high + low) / 2.0
    prev_close = np.roll(close, 1); prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr_vals = (pd.Series(tr).ewm(alpha=1/period, adjust=False).mean().values.copy() if change_atr
                else pd.Series(tr).rolling(window=period).mean().fillna(method="bfill").values.copy())
    up = np.zeros(n); dn = np.zeros(n); trend = np.ones(n, dtype=int)
    up[0] = src[0] - multiplier * atr_vals[0]; dn[0] = src[0] + multiplier * atr_vals[0]
    for i in range(1, n):
        curr_up = src[i] - multiplier * atr_vals[i]
        up[i] = max(curr_up, up[i-1]) if close[i-1] > up[i-1] else curr_up
        curr_dn = src[i] + multiplier * atr_vals[i]
        dn[i] = min(curr_dn, dn[i-1]) if close[i-1] < dn[i-1] else curr_dn
        if trend[i-1] == -1 and close[i] > dn[i-1]: trend[i] = 1
        elif trend[i-1] == 1 and close[i] < up[i-1]: trend[i] = -1
        else: trend[i] = trend[i-1]
    return trend, up, dn


def compute_pmax(df, atr_length=10, multiplier=3.0, ma_type="EMA", ma_length=10,
                 change_atr=True, normalize=False):
    """PMax (Profit Maximizer). Doener: (pmax_vals, mavg_vals, pmax_dir)"""
    high = df["high"].values; low = df["low"].values; close = df["close"].values
    n = len(df)
    if n == 0:
        return np.zeros(n), np.zeros(n), np.zeros(n, dtype=int)
    src = (high + low) / 2.0
    prev_close = np.roll(close, 1); prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr_vals = (pd.Series(tr).ewm(alpha=1/atr_length, adjust=False).mean().values.copy() if change_atr
                else pd.Series(tr).rolling(window=atr_length).mean().values.copy())
    atr_vals[np.isnan(atr_vals)] = tr[np.isnan(atr_vals)]
    src_series = pd.Series(src, dtype=float)
    if ma_type == "SMA":
        mavg = src_series.rolling(window=ma_length).mean().values.copy()
    elif ma_type == "EMA":
        mavg = src_series.ewm(span=ma_length, adjust=False).mean().values.copy()
    elif ma_type == "WMA":
        weights = np.arange(1, ma_length + 1, dtype=float)
        mavg = src_series.rolling(window=ma_length).apply(
            lambda x: np.dot(x, weights) / weights.sum(), raw=True).values.copy()
    elif ma_type == "TMA":
        h1 = int(np.ceil(ma_length / 2)); h2 = int(np.floor(ma_length / 2)) + 1
        mavg = src_series.rolling(h1).mean().rolling(h2).mean().values.copy()
    elif ma_type == "VAR":
        valpha = 2.0 / (ma_length + 1); var_vals = np.zeros(n)
        vud = np.zeros(n); vdd = np.zeros(n)
        for i in range(1, n):
            d = src[i] - src[i-1]
            if d > 0: vud[i] = d
            elif d < 0: vdd[i] = -d
        vs = pd.Series(vud).rolling(9).sum().values; vs[np.isnan(vs)] = 0.0
        ds = pd.Series(vdd).rolling(9).sum().values; ds[np.isnan(ds)] = 0.0
        for i in range(n):
            denom = vs[i] + ds[i]
            vcmo = (vs[i] - ds[i]) / denom if denom > 0 else 0.0
            var_vals[i] = src[i] if i == 0 else valpha * abs(vcmo) * src[i] + (1 - valpha * abs(vcmo)) * var_vals[i-1]
        mavg = var_vals
    elif ma_type == "WWMA":
        ww = 1.0 / ma_length; wwv = np.zeros(n); wwv[0] = src[0]
        for i in range(1, n): wwv[i] = ww * src[i] + (1 - ww) * wwv[i-1]
        mavg = wwv
    elif ma_type == "ZLEMA":
        lag = ma_length // 2 if ma_length % 2 == 0 else (ma_length - 1) // 2
        zd = np.array([src[i] + (src[i] - src[max(0, i - lag)]) for i in range(n)])
        mavg = pd.Series(zd).ewm(span=ma_length, adjust=False).mean().values.copy()
    elif ma_type == "TSF":
        mavg = np.zeros(n)
        for i in range(ma_length - 1, n):
            w = src[i - ma_length + 1:i + 1]; x = np.arange(ma_length, dtype=float)
            sx = x.sum(); sy = w.sum(); sxy = (x * w).sum(); sx2 = (x * x).sum()
            denom = ma_length * sx2 - sx ** 2
            if denom == 0: mavg[i] = src[i]; continue
            m = (ma_length * sxy - sx * sy) / denom; c = (sy - m * sx) / ma_length
            mavg[i] = m * (ma_length - 1) + c + m
        if ma_length - 1 < n: mavg[:ma_length-1] = mavg[ma_length-1]
    else:
        mavg = src_series.ewm(span=ma_length, adjust=False).mean().values.copy()
    mc = np.where(np.isnan(mavg), src, mavg)
    ls = np.zeros(n); ss = np.zeros(n); pd_ = np.ones(n, dtype=int); pv = np.zeros(n)
    for i in range(n):
        band = multiplier * atr_vals[i] / close[i] if normalize and close[i] > 0 else multiplier * atr_vals[i]
        cl = mc[i] - band; cs = mc[i] + band
        if i == 0:
            ls[i] = cl; ss[i] = cs; pd_[i] = 1
        else:
            ls[i] = max(cl, ls[i-1]) if mc[i] > ls[i-1] else cl
            ss[i] = min(cs, ss[i-1]) if mc[i] < ss[i-1] else cs
            if pd_[i-1] == -1 and mc[i] > ss[i-1]: pd_[i] = 1
            elif pd_[i-1] == 1 and mc[i] < ls[i-1]: pd_[i] = -1
            else: pd_[i] = pd_[i-1]
        pv[i] = ls[i] if pd_[i] == 1 else ss[i]
    return pv, mc, pd_


def compute_market_regime(df, adx_vals, bb_length=20, bb_mult=2.0,
                          squeeze_percentile=20.0, adx_range_thresh=20.0,
                          adx_trend_thresh=25.0):
    """Piyasa rejimi -- TAM DIZI (look-ahead yok). Doener: (regime_v, bb_width_v, bb_squeeze_v)
    #12 Genişletilmiş rejimler: STRONG_TREND, WEAK_TREND, COMPRESSION, EXPANSION,
    MEAN_REVERSION, RANGE, TRANSITION
    """
    n = len(df) if df is not None else 0
    regime_v = np.array(["TRANSITION"] * n, dtype=object)
    bb_width_v = np.zeros(n); bb_squeeze_v = np.zeros(n, dtype=bool)
    if df is None or n < bb_length + 10:
        return regime_v, bb_width_v, bb_squeeze_v
    try:
        close = df["close"]
        volume = df["volume"].values if "volume" in df.columns else np.ones(n)
        bbs = close.rolling(bb_length).mean(); bbd = close.rolling(bb_length).std(ddof=0)
        bw = ((bbs + bb_mult*bbd - (bbs - bb_mult*bbd)) / bbs.replace(0, np.nan)).fillna(0.0).values
        vol_ma = pd.Series(volume).rolling(20).mean().values

        # RSI for mean reversion detection
        rsi_vals = rsi(close, 14).values

        lb_r = min(n, 100)
        vbw = pd.Series(np.where(np.isnan(bw), np.nan, bw))
        sq_thr = vbw.rolling(lb_r, min_periods=6).quantile(squeeze_percentile / 100.0).values

        for i in range(n):
            bb_width_v[i] = float(bw[i])
            if not np.isnan(sq_thr[i]): bb_squeeze_v[i] = float(bw[i]) <= sq_thr[i]
            ca = float(adx_vals[i]) if i < len(adx_vals) else 25.0
            cv = float(volume[i]) if i < len(volume) else 0.0
            cv_ma = float(vol_ma[i]) if i < len(vol_ma) and not np.isnan(vol_ma[i]) else cv
            r_val = float(rsi_vals[i]) if i < len(rsi_vals) and not np.isnan(rsi_vals[i]) else 50.0

            # BB expanding (width increasing over last 5 bars)
            bb_expanding = False
            if i >= 5:
                bb_expanding = bw[i] > bw[i-1] > bw[i-2]

            if ca >= 40 and not bb_squeeze_v[i]:
                regime_v[i] = "STRONG_TREND"
            elif ca < adx_range_thresh and bb_squeeze_v[i]:
                # Check if it's compression (squeeze + low volatility)
                regime_v[i] = "COMPRESSION" if ca < 15 else "RANGE"
            elif bb_expanding and cv_ma > 0 and cv > 1.5 * cv_ma:
                regime_v[i] = "EXPANSION"
            elif ca < adx_range_thresh and (r_val > 70 or r_val < 30):
                regime_v[i] = "MEAN_REVERSION"
            elif ca >= adx_trend_thresh:
                regime_v[i] = "WEAK_TREND" if ca < 40 else "STRONG_TREND"
            else:
                regime_v[i] = "TRANSITION"
        return regime_v, bb_width_v, bb_squeeze_v
    except Exception:
        return regime_v, bb_width_v, bb_squeeze_v


def compute_fvg_zones(df, lookback=50, proximity_atr=0.5):
    """Fair Value Gap tespiti. Doener: (fvg_bull_near, fvg_bear_near) np.array[bool]
    Bolgeler dogum sirasina gore siralidir; her barda sadece aktif pencere
    (i-lookback .. i) taranir, eski bolgeler pointer ile atlanir."""
    n = len(df); high = df["high"].values; low = df["low"].values; close = df["close"].values
    atr_v = atr(df, 14).values
    fb = np.zeros(n, dtype=bool); fd = np.zeros(n, dtype=bool)
    bfvg = []; dfvg = []
    for i in range(2, n):
        if high[i-2] < low[i]: bfvg.append((high[i-2], low[i], i))
        if low[i-2] > high[i]: dfvg.append((high[i], low[i-2], i))
    hb = 0; hd = 0  # aktif pencere baslangiclari
    for i in range(n):
        if np.isnan(atr_v[i]) or atr_v[i] <= 0: continue
        px = proximity_atr * atr_v[i]
        while hb < len(bfvg) and i - bfvg[hb][2] > lookback: hb += 1
        while hd < len(dfvg) and i - dfvg[hd][2] > lookback: hd += 1
        for k in range(hb, len(bfvg)):
            bo, to, fb_i = bfvg[k]
            if fb_i > i: break
            if close[i] <= to + px and close[i] >= bo - px: fb[i] = True; break
        for k in range(hd, len(dfvg)):
            bo, to, fd_i = dfvg[k]
            if fd_i > i: break
            if close[i] >= bo - px and close[i] <= to + px: fd[i] = True; break
    return fb, fd


def compute_order_blocks(df, lookback=80, impulse_atr_mult=2.0, proximity_atr=0.75, max_age_bars=60):
    """Order Block tespiti. Doener: (ob_bull_near, ob_bear_near) np.array[bool]"""
    n = len(df); high = df["high"].values; low = df["low"].values
    open_ = df["open"].values; close = df["close"].values; atr_v = atr(df, 14).values
    ob_b = np.zeros(n, dtype=bool); ob_d = np.zeros(n, dtype=bool)
    bull_obs = []; bear_obs = []
    for i in range(4, n):
        if np.isnan(atr_v[i]) or atr_v[i] <= 0: continue
        ca = atr_v[i]
        if all(close[i-j] > open_[i-j] for j in range(3)):
            if sum(abs(close[i-j] - open_[i-j]) for j in range(3)) > impulse_atr_mult * ca:
                if close[i-3] < open_[i-3]: bull_obs.append((low[i-3], high[i-3], i-3))
        if all(close[i-j] < open_[i-j] for j in range(3)):
            if sum(abs(close[i-j] - open_[i-j]) for j in range(3)) > impulse_atr_mult * ca:
                if close[i-3] > open_[i-3]: bear_obs.append((low[i-3], high[i-3], i-3))
    hb = 0; hd = 0  # aktif pencere baslangiclari (max_age_bars)
    for i in range(n):
        if np.isnan(atr_v[i]) or atr_v[i] <= 0: continue
        px = proximity_atr * atr_v[i]
        while hb < len(bull_obs) and i - bull_obs[hb][2] > max_age_bars: hb += 1
        while hd < len(bear_obs) and i - bear_obs[hd][2] > max_age_bars: hd += 1
        for k in range(hb, len(bull_obs)):
            ol, oh, ob_i = bull_obs[k]
            if ob_i > i: break
            if close[i] >= ol - px and close[i] <= oh + px: ob_b[i] = True; break
        for k in range(hd, len(bear_obs)):
            ol, oh, ob_i = bear_obs[k]
            if ob_i > i: break
            if close[i] >= ol - px and close[i] <= oh + px: ob_d[i] = True; break
    return ob_b, ob_d


def compute_ema_cross(df, fast_len=9, slow_len=21, cross_lookback=5):
    """EMA crossover. Doener: (ema_cross_v, ema_position_v) np.array[int]"""
    n = len(df); close = df["close"].values
    ef = ema(df["close"], fast_len).values; es = ema(df["close"], slow_len).values
    ec = np.zeros(n, dtype=int); ep = np.zeros(n, dtype=int)
    for i in range(1, n):
        if ef[i] > es[i] and ef[i-1] <= es[i-1]:
            for j in range(i, min(i + cross_lookback, n)): ec[j] = 1
        elif ef[i] < es[i] and ef[i-1] >= es[i-1]:
            for j in range(i, min(i + cross_lookback, n)): ec[j] = -1
        ep[i] = 1 if close[i] > ef[i] else -1
    return ec, ep


def compute_engulfing(df, body_mult=1.5, body_lookback=20):
    """Engulfing mum deseni. Doener: engulfing_v np.array[int]"""
    n = len(df); open_ = df["open"].values; close = df["close"].values
    body = np.abs(close - open_)
    avg_body = pd.Series(body).rolling(window=body_lookback, min_periods=5).mean().values
    ev = np.zeros(n, dtype=int)
    for i in range(1, n):
        cbt = max(open_[i], close[i]); cbb = min(open_[i], close[i])
        pbt = max(open_[i-1], close[i-1]); pbb = min(open_[i-1], close[i-1])
        is_m = not np.isnan(avg_body[i]) and avg_body[i] > 0 and body[i] > body_mult * avg_body[i]
        if close[i] > open_[i] and close[i-1] < open_[i-1]:
            if cbb <= pbb and cbt >= pbt: ev[i] = 2 if is_m else 1
        elif close[i] < open_[i] and close[i-1] > open_[i-1]:
            if cbb <= pbb and cbt >= pbt: ev[i] = -2 if is_m else -1
    return ev


def compute_lux_liquidity(df, liq_len=7, liq_mar=1.45):
    """LuxAlgo Liquidity -- tam dizi. Doener: (bull_liq_sweep, bear_liq_sweep) bool arrays.
    Pivot tespiti rolling max/min ile vektorize; bolen bolgeleri (zoneler) sirali
    listede tutulup bisect ile taranir."""
    from bisect import bisect_left, bisect_right, insort
    from collections import deque
    n = len(df)
    bull_sweep = np.zeros(n, dtype=bool); bear_sweep = np.zeros(n, dtype=bool)
    if df is None or n < liq_len + 5: return bull_sweep, bear_sweep

    high = df["high"].values; low = df["low"].values
    atr_v = atr(df, 10).values

    # i-1 bari, onceki liq_len barin tumunden kat'i yuksek/dusukse pivot adayi
    prev_max_h = pd.Series(high).rolling(liq_len).max().shift(2).values
    prev_min_l = pd.Series(low).rolling(liq_len).min().shift(2).values

    # Sirali bolge listeleri: (top, ) buyside / (bottom, ) sellside
    buyside_zones = deque()   # (top,) artan sirali
    sellside_zones = deque()  # (bottom,) artan sirali
    ph_history = []
    pl_history = []

    for i in range(liq_len + 1, n):
        if not np.isnan(prev_max_h[i]) and high[i-1] > prev_max_h[i] and high[i] < high[i-1]:
            ph = high[i-1]; ca = atr_v[i]
            margin = ca / liq_mar if liq_mar > 0 else 0
            count = 0; min_p = ph; max_p = ph
            for p in reversed(ph_history):
                if p > ph + margin: break
                if ph - margin <= p <= ph + margin:
                    count += 1
                    min_p = min(min_p, p); max_p = max(max_p, p)
            if count >= 2:
                mid = (min_p + max_p) / 2.0
                insort(buyside_zones, (mid + margin,))
            ph_history.append(ph)
            if len(ph_history) > 50: ph_history.pop(0)

        if not np.isnan(prev_min_l[i]) and low[i-1] < prev_min_l[i] and low[i] > low[i-1]:
            pl = low[i-1]; ca = atr_v[i]
            margin = ca / liq_mar if liq_mar > 0 else 0
            count = 0; min_p = pl; max_p = pl
            for p in reversed(pl_history):
                if p < pl - margin: break
                if pl - margin <= p <= pl + margin:
                    count += 1
                    min_p = min(min_p, p); max_p = max(max_p, p)
            if count >= 2:
                mid = (min_p + max_p) / 2.0
                insort(sellside_zones, (mid - margin,))
            pl_history.append(pl)
            if len(pl_history) > 50: pl_history.pop(0)

        k = bisect_left(buyside_zones, (high[i],))
        if k > 0:
            for _ in range(k): buyside_zones.popleft()
            bull_sweep[i] = True
        k = bisect_right(sellside_zones, (low[i],))
        n_pop = len(sellside_zones) - k
        if n_pop > 0:
            for _ in range(n_pop): sellside_zones.pop()
            bear_sweep[i] = True

    return bull_sweep, bear_sweep


def compute_bos(df, pivot_length=1, max_age_bars=50):
    """Break of Structure (BoS) tespiti. Doener: (bull_bos, bear_bos) bool arrays.
    max_age_bars: pivot seviyesinin gecerlilik suresi; i - pivot_bar > max_age_bars ise
    seviye sifirlanir (cok eski pivotlara gore BoS uretilmez). <=0 sinirsiz demektir."""
    n = len(df)
    bull_bos = np.zeros(n, dtype=bool); bear_bos = np.zeros(n, dtype=bool)
    if df is None or n < pivot_length * 2 + 5: return bull_bos, bear_bos
    
    high = df["high"].values; low = df["low"].values; close = df["close"].values
    top = np.nan; bottom = np.nan
    top_bar = -1; bottom_bar = -1
    
    for i in range(pivot_length * 2, n):
        is_ph = True; is_pl = True
        ph_val = high[i - pivot_length]; pl_val = low[i - pivot_length]
        
        for j in range(i - pivot_length * 2, i + 1):
            if high[j] > ph_val: is_ph = False
            if low[j] < pl_val: is_pl = False
            
        if is_ph: top = ph_val; top_bar = i - pivot_length
        if is_pl: bottom = pl_val; bottom_bar = i - pivot_length
        
        if max_age_bars > 0:
            if not np.isnan(top) and i - top_bar > max_age_bars: top = np.nan
            if not np.isnan(bottom) and i - bottom_bar > max_age_bars: bottom = np.nan
        
        if not np.isnan(top) and close[i] > top and close[i-1] <= top: bull_bos[i] = True
        if not np.isnan(bottom) and close[i] < bottom and close[i-1] >= bottom: bear_bos[i] = True
            
    return bull_bos, bear_bos


def compute_stacked_ob_fvg(fgb, fgd, obb, obd):
    """Wrapper: Mevcut FVG ve OB dizilerini kullanarak Stacked (Üst üste binen) momentumu tespit eder."""
    return fgb & obb, fgd & obd


def find_pivots(df, length):
    """Pine-Script pivot high/low -- length bar sonra bilinir (repaint yok)."""
    high, low = df["high"].values, df["low"].values
    n = len(df); ph = np.full(n, np.nan); pl = np.full(n, np.nan)
    for i in range(length, n - length):
        wh = high[i-length:i+length+1]; wl = low[i-length:i+length+1]
        if high[i] == wh.max() and (wh == high[i]).sum() == 1: ph[i] = high[i]
        if low[i] == wl.min() and (wl == low[i]).sum() == 1: pl[i] = low[i]
    return ph, pl


def compute_htf_bias(htf_df):
    if htf_df is None or len(htf_df) < SETTINGS.HTF_EMA_LENGTH: return 0
    ev = ema(htf_df["close"], SETTINGS.HTF_EMA_LENGTH)
    return 1 if htf_df["close"].iloc[-1] > ev.iloc[-1] else -1


# ──────────────────────────────────────────
#  TTM Squeeze (LazyBear) -- BB, Keltner kanali icine girince squeeze
# ──────────────────────────────────────────

def compute_ttm_squeeze(df, bb_length=20, bb_mult=2.0, kc_mult=1.5):
    """TTM Squeeze -- tam dizi. Doener: (squeeze_on bool[], mom_dir int[])
    squeeze_on: BB, Keltner icerisinde (volatilite sikismasi).
    mom_dir: squeeze momentumunun yonu (+1/-1) -- linreg(close - ortanca) isareti."""
    n = len(df) if df is not None else 0
    sq = np.zeros(n, dtype=bool); md = np.zeros(n, dtype=int)
    if df is None or n < bb_length + 10: return sq, md
    try:
        close = df["close"]; high = df["high"]; low = df["low"]
        ma = close.rolling(bb_length).mean()
        sd = close.rolling(bb_length).std(ddof=0)
        bb_u = ma + bb_mult * sd; bb_l = ma - bb_mult * sd
        atr_v = atr(df, bb_length)
        kc_u = ma + kc_mult * atr_v; kc_l = ma - kc_mult * atr_v
        squeeze = (bb_l > kc_l) & (bb_u < kc_u)
        sq = squeeze.fillna(False).values

        # Momentum: linreg(close - ( (hh+ll)/2 + sma )/2 ), pencere = bb_length
        hh = high.rolling(bb_length).max(); ll = low.rolling(bb_length).min()
        mom_src = (close - ((hh + ll) / 2.0 + ma) / 2.0).values
        x = np.arange(bb_length, dtype=float)
        sx = x.sum(); sx2 = (x * x).sum()
        denom = bb_length * sx2 - sx ** 2
        sy = pd.Series(np.nan_to_num(mom_src)).rolling(bb_length).sum().values
        sxy = pd.Series(np.nan_to_num(mom_src)).rolling(bb_length).apply(
            lambda w: float(np.dot(x, w)), raw=True).values
        for i in range(bb_length - 1, n):
            if denom == 0 or np.isnan(sxy[i]): continue
            m = (bb_length * sxy[i] - sx * sy[i]) / denom
            md[i] = 1 if m > 0 else (-1 if m < 0 else 0)
        return sq, md
    except Exception:
        return sq, md


# ──────────────────────────────────────────
#  Ichimoku Bulutu -- fiyat bulutun ustunde/altinda (look-ahead yok)
# ──────────────────────────────────────────

def compute_ichimoku(df, tenkan=9, kijun=26, senkou_b=52):
    """Ichimoku -- tam dizi. Doener: ichi_dir int[]  (+1 ustunde, -1 altinda, 0 icinde).
    Bulut kenarlari mevcut bar ile hesaplanir (shift uygulanmaz; sinyal amacli)."""
    n = len(df) if df is not None else 0
    dv = np.zeros(n, dtype=int)
    if df is None or n < senkou_b: return dv
    try:
        high = df["high"]; low = df["low"]; close = df["close"].values
        ssa = ((high.rolling(tenkan).max() + low.rolling(tenkan).min()) / 2.0
               + (high.rolling(kijun).max() + low.rolling(kijun).min()) / 2.0) / 2.0
        ssb = (high.rolling(senkou_b).max() + low.rolling(senkou_b).min()) / 2.0
        top = np.maximum(ssa.values, ssb.values); bot = np.minimum(ssa.values, ssb.values)
        for i in range(n):
            if np.isnan(top[i]): continue
            if close[i] > top[i]: dv[i] = 1
            elif close[i] < bot[i]: dv[i] = -1
        return dv
    except Exception:
        return dv


# ──────────────────────────────────────────
#  Gercek CVD -- taker buy volume tabanli kumulatif delta egimi
# ──────────────────────────────────────────

def compute_cvd_slope(df, lookback=20):
    """CVD egimi -- tam dizi. Taker buy (agresif alici) hacmi gercek delta verir:
    delta = 2*taker_buy - volume. Doener: cvd_dir int[] (+1/-1/0).
    taker_buy kolonu yoksa (backtest verisi) (close-open) tahmini kullanilir."""
    n = len(df) if df is not None else 0
    dv = np.zeros(n, dtype=int)
    if df is None or n < lookback + 2: return dv
    try:
        vol = df["volume"].values
        if "taker_buy" in df.columns:
            delta = 2.0 * df["taker_buy"].values - vol
        else:
            delta = np.sign(df["close"].values - df["open"].values) * vol
        cvd = np.cumsum(delta)
        for i in range(lookback, n):
            if cvd[i] > cvd[i - lookback]: dv[i] = 1
            elif cvd[i] < cvd[i - lookback]: dv[i] = -1
        return dv
    except Exception:
        return dv


# ──────────────────────────────────────────
#  Arac 2: Breakout Finder (Dizi Versiyonu)
# ──────────────────────────────────────────

def compute_breakout(df, prd=5, bo_len=200, cwidth_pct=3.0, mintest=2):
    """Breakout Finder -- tam dizi (look-ahead yok). Doener: (bull_bo_v, bear_bo_v) bool arrays.
    Pivotlar tum dizi icin bir kez hesaplanir; her barda onceki bo_len bar icindeki
    pivotlar pointer ile alinir (O(n) toplam, eskiden O(n^2*prd))."""
    n = len(df) if df is not None else 0
    if df is None or n < prd*2+5: return np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    try:
        h = df["high"].values; l = df["low"].values; c = df["close"].values; o = df["open"].values
        bv = np.zeros(n, dtype=bool); dv = np.zeros(n, dtype=bool)

        # Tum pivotlar tek geciste (find_pivots ile ayni kriter)
        ph, pl = find_pivots(df, prd)
        ph_idx = np.nonzero(~np.isnan(ph))[0]
        pl_idx = np.nonzero(~np.isnan(pl))[0]
        ph_ptr = 0; pl_ptr = 0  # pencere baslangici isaretcisi

        for i in range(prd*2+5, n):
            h_ = h[max(0, i+1-300):i+1].max(); l_ = l[max(0, i+1-300):i+1].min()
            chwidth = (h_ - l_) * (cwidth_pct / 100.0)

            # [i-bo_len, i-prd] araligindaki pivotlar
            lo_b = max(prd, i - bo_len); hi_b = i - prd
            a = int(np.searchsorted(ph_idx, lo_b)); b = int(np.searchsorted(ph_idx, hi_b, "right"))
            ph_vals = [float(ph[j]) for j in ph_idx[a:b]]
            a = int(np.searchsorted(pl_idx, lo_b)); b = int(np.searchsorted(pl_idx, hi_b, "right"))
            pl_vals = [float(pl[j]) for j in pl_idx[a:b]]

            cc = c[i]; co = o[i]
            hgst = h[max(0, i-prd):i].max() if i >= prd else h[0]
            lwst = l[max(0, i-prd):i].min()  if i >= prd else l[0]
            bull_bo = False
            if len(ph_vals) >= mintest and cc > co and cc > hgst:
                bm = ph_vals[-1]; xx = 0
                for x in range(len(ph_vals)):
                    if ph_vals[x] >= cc: break
                    xx = x; bm = max(bm, ph_vals[x])
                if xx >= mintest - 1 and co <= bm:
                    if sum(1 for x in range(xx+1) if bm-chwidth <= ph_vals[x] <= bm) >= mintest and hgst < bm: bull_bo = True
            bear_bo = False
            if len(pl_vals) >= mintest and cc < co and cc < lwst:
                bm = pl_vals[-1]; xx = 0
                for x in range(len(pl_vals)):
                    if pl_vals[x] <= cc: break
                    xx = x; bm = min(bm, pl_vals[x])
                if xx >= mintest - 1 and co >= bm:
                    if sum(1 for x in range(xx+1) if bm <= pl_vals[x] <= bm+chwidth) >= mintest and lwst > bm: bear_bo = True
            bv[i] = bull_bo; dv[i] = bear_bo
        return bv, dv
    except Exception:
        return np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)


# ──────────────────────────────────────────
#  Arac 3&4: Volume Profile -- Dizi Versiyonu
# ──────────────────────────────────────────

def _compute_volume_profile_at(sh, sl, sv, num_bars, va_pct):
    hp = sh.max(); lp = sl.min()
    if hp <= lp: return np.nan, np.nan, np.nan
    pi = (hp - lp) / (num_bars - 1); vols = np.zeros(num_bars)
    for k in range(len(sh)):
        lo, hi, vol = sl[k], sh[k], sv[k]
        if hi <= lo or vol == 0: continue
        ss = max(int((lo - lp) / pi), 0); es = min(int((hi - lp) / pi), num_bars-1)
        for j in range(ss, es+1):
            pl_j = lp + pi * j
            if lo >= pl_j and hi > pl_j + pi: prop = (pl_j + pi - lo) / (hi - lo)
            elif hi <= pl_j + pi and lo < pl_j: prop = (hi - pl_j) / (hi - lo)
            elif lo >= pl_j and hi <= pl_j + pi: prop = 1.0
            else: prop = pi / (hi - lo)
            vols[j] += vol * prop
    pi_idx = int(np.argmax(vols)); poc = lp + pi * pi_idx
    tv = vols.sum(); vvt = tv * (va_pct / 100.0)
    vu = vd = pi_idx; vs = vols[pi_idx]
    while vs < vvt:
        vu_v = vols[vu+1] if vu < num_bars-1 else 0.0
        vd_v = vols[vd-1] if vd > 0 else 0.0
        if vu_v == 0 and vd_v == 0: break
        if vu_v >= vd_v: vs += vu_v; vu += 1
        else: vs += vd_v; vd -= 1
    return poc, lp + pi * (vu+1), lp + pi * vd


def compute_volume_profile(df, num_bars=100, lookback=200, va_pct=68.0, stride=None):
    """Volume Profile -- tam dizi (look-ahead yok, stride ile hizlandirilmis).
    Doener: (poc_v, vah_v, val_v) float arrays.
    """
    n = len(df) if df is not None else 0
    pv = np.full(n, np.nan); vhv = np.full(n, np.nan); vlv = np.full(n, np.nan)
    if df is None or n < 20: return pv, vhv, vlv
    stride = stride or SETTINGS.VP_SERIES_STRIDE
    ha = df["high"].values; la = df["low"].values; va = df["volume"].values
    lp = lh = ll = np.nan
    try:
        for i in range(20, n):
            if (i - 20) % stride == 0:
                lb = min(lookback, i+1)
                try: lp, lh, ll = _compute_volume_profile_at(ha[i-lb+1:i+1], la[i-lb+1:i+1], va[i-lb+1:i+1], num_bars, va_pct)
                except Exception: pass
            pv[i] = lp; vhv[i] = lh; vlv[i] = ll
        return pv, vhv, vlv
    except Exception:
        return pv, vhv, vlv


# ──────────────────────────────────────────
#  Arac 5: Linear Regression Oscillator -- Dizi Versiyonu
# ──────────────────────────────────────────

def compute_linreg_osc(df, length=20, norm_window=100):
    """LinReg Osc -- tam dizi (look-ahead yok). Doener: np.array[float]"""
    n = len(df) if df is not None else 0
    if df is None or n < length + norm_window: return np.zeros(n)
    try:
        src = df["close"].values; raw = np.zeros(n)
        for end in range(length-1, n):
            w = src[end-length+1:end+1]; x = np.arange(length, dtype=float)
            sx = x.sum(); sy = w.sum(); sxy = (x*w).sum(); sx2 = (x*x).sum()
            denom = length * sx2 - sx**2
            if denom == 0: raw[end] = 0.0; continue
            m = (length * sxy - sx * sy) / denom; c = (sy - m * sx) / length
            raw[end] = -1.0 * (m * (length-1) + c)
        rs = pd.Series(raw)
        sma = rs.rolling(norm_window).mean()
        std = rs.rolling(norm_window).std(ddof=0).replace(0, np.nan)
        return ((rs - sma) / std).fillna(0.0).values
    except Exception:
        return np.zeros(n)


# ──────────────────────────────────────────
#  Arac 6: Dynamic Swing Anchored VWAP -- Dizi Versiyonu
# ──────────────────────────────────────────

def compute_swing_vwap(df, prd=50, apt=20.0):
    """Swing VWAP -- tam dizi. İçiçe for döngülerinden arındırılıp (O(n)) incremental hale getirildi."""
    n = len(df) if df is not None else 0
    wv = np.full(n, np.nan); dv = np.zeros(n, dtype=int)
    if df is None or n < prd*2+5: return wv, dv
    try:
        high = df["high"].values; low = df["low"].values
        close = df["close"].values; vol = df["volume"].values
        hlc3 = (high + low + close) / 3.0
        decay = np.exp(-np.log(2.0) / max(1.0, apt)); alpha = 1.0 - decay
        
        hm = df["high"].rolling(prd).max().values
        lm = df["low"].rolling(prd).min().values

        ph_loc = 0; pl_loc = 0
        last_ai = -1
        curr_p = 0.0; curr_v = 0.0
        
        for i in range(n):
            if not np.isnan(hm[i]) and high[i] >= hm[i]: ph_loc = i
            if not np.isnan(lm[i]) and low[i] <= lm[i]: pl_loc = i
            if i < prd*2+5: continue
            
            dir_ = 1 if pl_loc > ph_loc else -1
            ai = pl_loc if dir_ == 1 else ph_loc
            
            if ai != last_ai:
                last_ai = ai
                curr_p = hlc3[ai] * vol[ai]
                curr_v = vol[ai]
                for k in range(ai+1, i+1):
                    curr_p = (1.0-alpha)*curr_p + alpha*(hlc3[k]*vol[k])
                    curr_v = (1.0-alpha)*curr_v + alpha*vol[k]
            else:
                curr_p = (1.0-alpha)*curr_p + alpha*(hlc3[i]*vol[i])
                curr_v = (1.0-alpha)*curr_v + alpha*vol[i]
                
            wv[i] = curr_p/curr_v if curr_v > 0 else np.nan; dv[i] = dir_
        return wv, dv
    except Exception:
        return wv, dv


# ──────────────────────────────────────────
#  Arac 4: True Strength Index -- Dizi Versiyonu
# ──────────────────────────────────────────

def compute_tsi(df, long_len=25, short_len=13, signal_len=13):
    """TSI -- tam dizi (look-ahead yok). Doener: (tsi_v, sig_v) arrays."""
    n = len(df) if df is not None else 0
    if df is None or n < long_len + short_len + signal_len + 5:
        return np.zeros(n), np.zeros(n)
    try:
        pc = df["close"].diff(); apc = pc.abs()
        dsp = pc.ewm(span=long_len, adjust=False).mean().ewm(span=short_len, adjust=False).mean()
        dsa = apc.ewm(span=long_len, adjust=False).mean().ewm(span=short_len, adjust=False).mean()
        ts = 100.0 * (dsp / dsa.replace(0, np.nan)).fillna(0.0)
        return ts.values, ts.ewm(span=signal_len, adjust=False).mean().values
    except Exception:
        return np.zeros(n), np.zeros(n)


# ──────────────────────────────────────────
#  Arac 3: Multi-Indicator Divergence -- Dizi Versiyonu
# ──────────────────────────────────────────

def _compute_indicator_series(df):
    close = df["close"]; high = df["high"]; low = df["low"]; volume = df["volume"]; n = len(df)
    r = {}
    r["rsi"] = rsi(close, 14)
    ef = close.ewm(span=12, adjust=False).mean(); es = close.ewm(span=26, adjust=False).mean()
    ml = ef - es; sl = ml.ewm(span=9, adjust=False).mean(); r["macd_hist"] = ml - sl
    lm = low.rolling(14).min(); hm = high.rolling(14).max()
    r["stoch"] = 100.0 * (close - lm) / (hm - lm).replace(0, np.nan)
    tp = (high + low + close) / 3.0; tm = tp.rolling(10).mean()
    tmd = tp.rolling(10).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    r["cci"] = (tp - tm) / (0.015 * tmd.replace(0, np.nan))
    r["momentum"] = close.diff(10)
    r["obv"] = (volume * np.sign(close.diff().fillna(0))).cumsum()
    
    # Vectorized MFI calculation
    diff = tp.diff()
    raw_flow = tp * volume
    pos_flow = pd.Series(np.where(diff > 0, raw_flow, 0.0), index=df.index).rolling(14).sum()
    neg_flow = pd.Series(np.where(diff < 0, raw_flow, 0.0), index=df.index).rolling(14).sum()
    mfr = pos_flow / neg_flow.replace(0, np.nan)
    mfi = 100.0 - (100.0 / (1.0 + mfr))
    r["mfi"] = mfi.fillna(50.0)
    return r


def _pivot_low_idx(price_lows, prd):
    n = len(price_lows)
    if n < prd*2+5: return []
    return [i for i in range(prd, n-prd)
            if price_lows[i] == price_lows[i-prd:i+prd+1].min()
            and (price_lows[i-prd:i+prd+1] == price_lows[i]).sum() == 1]


def _pivot_high_idx(price_highs, prd):
    n = len(price_highs)
    if n < prd*2+5: return []
    return [i for i in range(prd, n-prd)
            if price_highs[i] == price_highs[i-prd:i+prd+1].max()
            and (price_highs[i-prd:i+prd+1] == price_highs[i]).sum() == 1]


def _find_bullish_div(price_lows, indicator_vals, prd, max_pp, max_bars, pli=None):
    n = len(price_lows)
    if n < prd*2+5: return False
    try:
        if pli is None: pli = _pivot_low_idx(price_lows, prd)
        if len(pli) < 2: return False
        ci = pli[-1]
        if n-1-ci > max_bars: return False
        cp = price_lows[ci]; cv = indicator_vals[ci]
        for pi in reversed(pli[:-1]):
            if ci-pi > max_bars or ci-pi < 5: continue
            if cp < price_lows[pi] and cv > indicator_vals[pi]: return True
        return False
    except Exception: return False


def _find_bearish_div(price_highs, indicator_vals, prd, max_pp, max_bars, phi=None):
    n = len(price_highs)
    if n < prd*2+5: return False
    try:
        if phi is None: phi = _pivot_high_idx(price_highs, prd)
        if len(phi) < 2: return False
        ci = phi[-1]
        if n-1-ci > max_bars: return False
        cp = price_highs[ci]; cv = indicator_vals[ci]
        for pi in reversed(phi[:-1]):
            if ci-pi > max_bars or ci-pi < 5: continue
            if cp > price_highs[pi] and cv < indicator_vals[pi]: return True
        return False
    except Exception: return False


def compute_multi_divergence(df, prd=5, lookback=100, max_pp=10, stride=None):
    """Multi-Indicator Divergence -- tam dizi (look-ahead yok, stride ile hizlandirilmis).
    Fiyat pivotlari stride adiminda bir kez hesaplanir; 7 gosterge icin tekrar kullanilir.
    Doener: (bull_v, bear_v) float arrays.  (#7 Weighted Divergence)
    """
    n = len(df) if df is not None else 0
    bv = np.zeros(n, dtype=float); dv = np.zeros(n, dtype=float)
    if df is None or n < max(lookback, prd*2+10): return bv, dv
    stride = stride or SETTINGS.MULTI_DIV_STRIDE
    weights = SETTINGS.DIVERGENCE_WEIGHTS
    lb_ = lb_d = 0.0
    try:
        for i in range(lookback, n):
            if (i - lookback) % stride == 0:
                lb = min(lookback, i+1)
                sub = df.iloc[i-lb+1:i+1].copy().reset_index(drop=True)
                inds = _compute_indicator_series(sub)
                pl = sub["low"].values; ph = sub["high"].values
                pli = _pivot_low_idx(pl, prd)
                phi = _pivot_high_idx(ph, prd)
                lb_ = sum(weights.get(k, 1) for k, s in inds.items() if _find_bullish_div(pl, s.fillna(0.0).values, prd, max_pp, lb, pli))
                lb_d = sum(weights.get(k, 1) for k, s in inds.items() if _find_bearish_div(ph, s.fillna(0.0).values, prd, max_pp, lb, phi))
            bv[i] = lb_; dv[i] = lb_d
        return bv, dv
    except Exception:
        return bv, dv


def compute_whale_absorption_profile(df, lookback=200, num_bins=35):
    if df is None or len(df) < 20: return [], []
    try:
        lb = min(lookback, len(df)); sub = df.iloc[-lb:]
        mnp = sub["low"].min(); mxp = sub["high"].max()
        if mxp <= mnp: return [], []
        step = (mxp - mnp) / num_bins
        ba = np.zeros(num_bins); da = np.zeros(num_bins)
        for _, row in sub.iterrows():
            o, h, l, c, vol = row["open"], row["high"], row["low"], row["close"], row["volume"]
            rng = h - l
            if rng <= 0 or vol == 0: continue
            if c > o:
                wl = max(o,c); wh = h; wlen = wh - wl
                if wlen > 0:
                    av = vol * (wlen / rng)
                    for j in range(num_bins):
                        bl = mnp + j*step; bh = bl + step
                        ov = max(0.0, min(bh, wh) - max(bl, wl))
                        if ov > 0: da[j] += av * (ov / wlen)
            elif c < o:
                wl = l; wh = min(o,c); wlen = wh - wl
                if wlen > 0:
                    av = vol * (wlen / rng)
                    for j in range(num_bins):
                        bl = mnp + j*step; bh = bl + step
                        ov = max(0.0, min(bh, wh) - max(bl, wl))
                        if ov > 0: ba[j] += av * (ov / wlen)
        bp = [mnp+(j+0.5)*step for j in range(1,num_bins-1) if ba[j]>ba[j-1] and ba[j]>ba[j+1] and ba[j]>0]
        dp_ = [mnp+(j+0.5)*step for j in range(1,num_bins-1) if da[j]>da[j-1] and da[j]>da[j+1] and da[j]>0]
        return bp, dp_
    except Exception:
        return [], []


def compute_delta_dominance(df, lookback=200, percentile=95.0):
    if df is None or len(df) < 5: return 0
    try:
        lb = min(lookback, len(df)); sub = df.iloc[-lb:].copy()
        h = sub["high"].values; l = sub["low"].values
        o = sub["open"].values; c = sub["close"].values; v = sub["volume"].values
        rng = np.where(h-l <= 0, 1e-8, h-l)
        deltas = v * (c-o) / rng; ad = np.abs(deltas)
        thr = np.percentile(ad, percentile); cd = deltas[-1]
        if abs(cd) >= thr and thr > 0: return 1 if cd > 0 else -1
        return 0
    except Exception: return 0


def compute_whale_arrays(df, atr_v=None, lookback=200, num_bins=35, delta_percentile=95.0, stride=1):
    """Whale Absorption ve Delta Dominance değerlerini tam dizi döner. (Look-ahead yok, stride destekli)"""
    n = len(df) if df is not None else 0
    wba_v = np.zeros(n, dtype=bool)
    wda_v = np.zeros(n, dtype=bool)
    wdd_v = np.zeros(n, dtype=int)
    if df is None or n < 20: return wba_v, wda_v, wdd_v
    
    close = df["close"].values
    if atr_v is None:
        atr_v = atr(df, 14).values
    last_wba = False; last_wda = False; last_wdd = 0
    for i in range(20, n):
        if (i - 20) % stride == 0:
            lb = min(lookback, i+1)
            sub = df.iloc[i-lb+1:i+1]
            try:
                bp, dp2 = compute_whale_absorption_profile(sub, lookback, num_bins)
                ca_ = atr_v[i]
                last_wba = any(abs(close[i]-p) <= 0.5*ca_ for p in bp) if ca_ > 0 else False
                last_wda = any(abs(close[i]-p) <= 0.5*ca_ for p in dp2) if ca_ > 0 else False
                last_wdd = compute_delta_dominance(sub, lookback, delta_percentile)
            except Exception: pass
        wba_v[i] = last_wba
        wda_v[i] = last_wda
        wdd_v[i] = last_wdd
    return wba_v, wda_v, wdd_v


# ──────────────────────────────────────────
#  Chaikin Money Flow (CMF) -- para akisi baskisi
# ──────────────────────────────────────────

def compute_cmf(df, length=20):
    """CMF -- tam dizi. Doener: np.array[float] (-1..+1).
    MF multiplier = ((C-L)-(H-C))/(H-L); CMF = sum(MF*V)/sum(V)."""
    n = len(df) if df is not None else 0
    if df is None or n < length + 2: return np.zeros(n)
    try:
        h = df["high"]; l = df["low"]; c = df["close"]; v = df["volume"]
        hl = (h - l).replace(0, np.nan)
        mfm = (((c - l) - (h - c)) / hl).fillna(0.0)
        mfv = mfm * v
        cmf = (mfv.rolling(length).sum() / v.rolling(length).sum().replace(0, np.nan)).fillna(0.0)
        return cmf.values
    except Exception:
        return np.zeros(n)


# ──────────────────────────────────────────
#  Stochastic RSI -- asiri bolge tespiti (anti-chase / pullback)
# ──────────────────────────────────────────

def compute_stoch_rsi(df, rsi_length=14, stoch_length=14, smooth=3):
    """StochRSI -- tam dizi. Doener: np.array[float] (0..100, %K)."""
    n = len(df) if df is not None else 0
    if df is None or n < rsi_length + stoch_length + smooth + 2: return np.full(n, 50.0)
    try:
        r = rsi(df["close"], rsi_length)
        rmin = r.rolling(stoch_length).min(); rmax = r.rolling(stoch_length).max()
        rng_ = (rmax - rmin).replace(0, np.nan)
        k = (100.0 * (r - rmin) / rng_).fillna(50.0).rolling(smooth).mean()
        return k.fillna(50.0).values
    except Exception:
        return np.full(n, 50.0)


@dataclass
class SweepSetup:
    direction: int
    level: float
    score: float
    anchor_extreme: float
    bar_index: int
    rsi_div: int = 0
    funding_rate: float = 0.0
    squeeze_risk: str = ""
    adx_value: float = 0.0
    di_plus: float = 0.0
    di_minus: float = 0.0
    supertrend_trend: int = 0
    poc_price: float = 0.0
    vah_price: float = 0.0
    val_price: float = 0.0
    linreg_osc: float = 0.0
    swing_vwap: float = 0.0
    swing_vwap_dir: int = 0
    breakout_signal: int = 0
    tsi_value: float = 0.0
    tsi_signal: float = 0.0
    multi_div_bull: float = 0.0
    multi_div_bear: float = 0.0
    whale_bull_absorption: bool = False
    whale_bear_absorption: bool = False
    whale_delta_dominance: int = 0
    pmax_trend: int = 0
    pmax_cross: int = 0
    market_regime: str = "TRANSITION"
    fvg_bull_near: bool = False
    fvg_bear_near: bool = False
    ob_bull_near: bool = False
    ob_bear_near: bool = False
    ema_cross: int = 0
    ema_position: int = 0
    engulfing: int = 0
    lux_liq_sweep: bool = False
    bos_signal: int = 0
    stacked_ob_fvg: int = 0
    ttm_squeeze: int = 0
    ichimoku_trend: int = 0
    cvd_trend: int = 0
    cmf_value: float = 0.0
    stoch_rsi: float = 50.0
    ob_imbalance_ratio: float = 0.0
    ob_favorable: bool = False
    confluence_score: float = 0.0
    grade: str = ""
    probability: float = 0.0
    setup_type: str = "Liq Sweep"
    htf_counter_trend: bool = False
    engine: str = "sweep"


class SweepEngine:
    """Sembol basina bir ornek tutulan, durumlu likidite-supurmesi + CHoCH motoru."""

    def __init__(self, htf_bias: int = 0, funding_info: dict = None, rsi_div: int = 0):
        self.hi_levels = []; self.lo_levels = []
        self.pending = None
        self.last_minor_high = np.nan; self.last_minor_low = np.nan
        self.htf_bias = htf_bias; self.funding_info = funding_info or {}; self.rsi_div = rsi_div

    @staticmethod
    def _prune(lst, max_len=25):
        while len(lst) > max_len: lst.pop(0)

    def iter_signals(self, df):
        """Dataframe boyunca ilerler, onaylanan her sinyali sirayla uretiyor (generator).
        Look-ahead yok: tum gostergeler dongu oncesinde tam dizi olarak hesaplanir.
        """
        if "volume" not in df.columns:
            df["volume"] = 0.0
            
        n = len(df); sl = SETTINGS.SWING_LENGTH; ml = SETTINGS.MINOR_PIVOT_LENGTH
        ph, pl = find_pivots(df, sl); mph, mpl = find_pivots(df, ml)
        atr_v = atr(df, SETTINGS.ATR_LEN).values
        vol_ma = df["volume"].rolling(SETTINGS.VOLUME_MA_LEN).mean().values
        volume = df["volume"].values
        close, high, low, open_ = (df[c].values for c in ["close", "high", "low", "open"])

        # ── PRECOMPUTE: tum gostergeler tam dizi (look-ahead yok) ──
        adx_v, di_p, di_m = compute_adx_di(df, length=SETTINGS.ADX_LENGTH)
        st_v, _, _ = compute_supertrend(df, period=SETTINGS.SUPERTREND_PERIOD,
                      multiplier=SETTINGS.SUPERTREND_MULTIPLIER, change_atr=SETTINGS.SUPERTREND_CHANGE_ATR)

        bbo = np.zeros(n, dtype=bool); dbo = np.zeros(n, dtype=bool)
        if SETTINGS.USE_BREAKOUT_FILTER:
            bbo, dbo = compute_breakout(df, prd=SETTINGS.BREAKOUT_PRD, bo_len=SETTINGS.BREAKOUT_BO_LEN,
                cwidth_pct=SETTINGS.BREAKOUT_CWIDTH_PCT, mintest=SETTINGS.BREAKOUT_MINTEST)

        poc_v = vah_v = val_v = np.full(n, np.nan)
        if SETTINGS.USE_VOLUME_PROFILE:
            poc_v, vah_v, val_v = compute_volume_profile(df, num_bars=SETTINGS.VP_NUM_BARS,
                lookback=SETTINGS.VP_LOOKBACK, va_pct=SETTINGS.VP_VA_PCT, stride=SETTINGS.VP_SERIES_STRIDE)

        lr_v = np.zeros(n)
        if SETTINGS.USE_LINREG_OSC:
            lr_v = compute_linreg_osc(df, length=SETTINGS.LINREG_LENGTH)

        wv = np.full(n, np.nan); wdv = np.zeros(n, dtype=int)
        if SETTINGS.USE_SWING_VWAP:
            wv, wdv = compute_swing_vwap(df, prd=SETTINGS.SWING_VWAP_PRD, apt=SETTINGS.SWING_VWAP_APT)

        tv = np.zeros(n); tsv = np.zeros(n)
        if SETTINGS.USE_TSI:
            tv, tsv = compute_tsi(df, long_len=SETTINGS.TSI_LONG_LEN,
                short_len=SETTINGS.TSI_SHORT_LEN, signal_len=SETTINGS.TSI_SIGNAL_LEN)

        mdb = np.zeros(n, dtype=float); mdd = np.zeros(n, dtype=float)
        if SETTINGS.USE_MULTI_DIV:
            mdb, mdd = compute_multi_divergence(df, prd=SETTINGS.MULTI_DIV_PRD,
                lookback=SETTINGS.MULTI_DIV_LOOKBACK, max_pp=SETTINGS.MULTI_DIV_MAX_PP,
                stride=SETTINGS.MULTI_DIV_STRIDE)

        pmxd = np.zeros(n, dtype=int); pmxc = np.zeros(n, dtype=int)
        if SETTINGS.USE_PMAX:
            pv_, mv_, pmxd = compute_pmax(df, atr_length=SETTINGS.PMAX_ATR_LENGTH,
                multiplier=SETTINGS.PMAX_ATR_MULTIPLIER, ma_type=SETTINGS.PMAX_MA_TYPE,
                ma_length=SETTINGS.PMAX_MA_LENGTH, change_atr=SETTINGS.PMAX_CHANGE_ATR,
                normalize=SETTINGS.PMAX_NORMALIZE)
            for ci in range(1, n):
                if mv_[ci] > pv_[ci] and mv_[ci-1] <= pv_[ci-1]:
                    for cj in range(ci, min(ci+SETTINGS.PMAX_CROSS_LOOKBACK, n)): pmxc[cj] = 1
                elif mv_[ci] < pv_[ci] and mv_[ci-1] >= pv_[ci-1]:
                    for cj in range(ci, min(ci+SETTINGS.PMAX_CROSS_LOOKBACK, n)): pmxc[cj] = -1

        rgv = np.array(["TRANSITION"]*n, dtype=object)
        if SETTINGS.USE_MARKET_REGIME:
            rgv, _, _ = compute_market_regime(df, adx_v, bb_length=SETTINGS.REGIME_BB_LENGTH,
                bb_mult=SETTINGS.REGIME_BB_MULT, squeeze_percentile=SETTINGS.REGIME_BB_SQUEEZE_PERCENTILE,
                adx_range_thresh=SETTINGS.REGIME_ADX_RANGE_THRESHOLD, adx_trend_thresh=SETTINGS.REGIME_ADX_TREND_THRESHOLD)

        fgb = np.zeros(n, dtype=bool); fgd = np.zeros(n, dtype=bool)
        if SETTINGS.USE_FVG:
            fgb, fgd = compute_fvg_zones(df, lookback=SETTINGS.FVG_LOOKBACK, proximity_atr=SETTINGS.FVG_PROXIMITY_ATR)

        obb = np.zeros(n, dtype=bool); obd = np.zeros(n, dtype=bool)
        if SETTINGS.USE_ORDER_BLOCK:
            obb, obd = compute_order_blocks(df, lookback=SETTINGS.OB_LOOKBACK,
                impulse_atr_mult=SETTINGS.OB_IMPULSE_ATR_MULT, proximity_atr=SETTINGS.OB_PROXIMITY_ATR,
                max_age_bars=SETTINGS.OB_MAX_AGE_BARS)

        ecv = np.zeros(n, dtype=int); epv = np.zeros(n, dtype=int)
        if SETTINGS.USE_EMA_CROSS:
            ecv, epv = compute_ema_cross(df, fast_len=SETTINGS.EMA_FAST_LEN, slow_len=SETTINGS.EMA_SLOW_LEN,
                cross_lookback=SETTINGS.EMA_CROSS_LOOKBACK)

        env = np.zeros(n, dtype=int)
        if SETTINGS.USE_ENGULFING:
            env = compute_engulfing(df, body_mult=SETTINGS.ENGULFING_BODY_MULT, body_lookback=SETTINGS.ENGULFING_LOOKBACK)

        rsi_div_v = np.zeros(n, dtype=int)
        if SETTINGS.USE_RSI_DIVERGENCE:
            rsi_div_v = compute_rsi_divergence_array(df, lookback=SETTINGS.RSI_DIVERGENCE_LOOKBACK, stride=getattr(SETTINGS, "RSI_DIVERGENCE_STRIDE", 1))

        wba_v = np.zeros(n, dtype=bool); wda_v = np.zeros(n, dtype=bool); wdd_v = np.zeros(n, dtype=int)
        if SETTINGS.USE_WHALE_PROFILE:
            wba_v, wda_v, wdd_v = compute_whale_arrays(df, atr_v=atr_v, lookback=SETTINGS.WHALE_LOOKBACK, num_bins=SETTINGS.WHALE_BINS, 
                                                       delta_percentile=SETTINGS.WHALE_DELTA_PERCENTILE, stride=getattr(SETTINGS, "WHALE_STRIDE", 1))

        lux_bull_sweep = np.zeros(n, dtype=bool); lux_bear_sweep = np.zeros(n, dtype=bool)
        if getattr(SETTINGS, "USE_LUX_LIQUIDITY", False):
            lux_bull_sweep, lux_bear_sweep = compute_lux_liquidity(df, liq_len=getattr(SETTINGS, "LUX_LIQ_LENGTH", 7), liq_mar=getattr(SETTINGS, "LUX_LIQ_MARGIN", 1.45))

        bull_bos = np.zeros(n, dtype=bool); bear_bos = np.zeros(n, dtype=bool)
        if getattr(SETTINGS, "USE_BOS", False):
            bull_bos, bear_bos = compute_bos(df, pivot_length=getattr(SETTINGS, "BOS_PIVOT_LENGTH", 1),
                max_age_bars=getattr(SETTINGS, "BOS_MAX_AGE_BARS", 50))

        stacked_bull = np.zeros(n, dtype=bool); stacked_bear = np.zeros(n, dtype=bool)
        if getattr(SETTINGS, "USE_STACKED_OB_FVG", False) and SETTINGS.USE_FVG and SETTINGS.USE_ORDER_BLOCK:
            stacked_bull, stacked_bear = compute_stacked_ob_fvg(fgb, fgd, obb, obd)

        sq_on = np.zeros(n, dtype=bool); ttm_dir = np.zeros(n, dtype=int)
        if getattr(SETTINGS, "USE_TTM_SQUEEZE", False):
            sq_on, ttm_dir = compute_ttm_squeeze(df, bb_length=SETTINGS.TTM_BB_LENGTH,
                bb_mult=SETTINGS.TTM_BB_MULT, kc_mult=SETTINGS.TTM_KC_MULT)

        ichi_v = np.zeros(n, dtype=int)
        if getattr(SETTINGS, "USE_ICHIMOKU", False):
            ichi_v = compute_ichimoku(df, tenkan=SETTINGS.ICHIMOKU_TENKAN,
                kijun=SETTINGS.ICHIMOKU_KIJUN, senkou_b=SETTINGS.ICHIMOKU_SENKOU_B)

        cvd_v = np.zeros(n, dtype=int)
        if getattr(SETTINGS, "USE_CVD", False):
            cvd_v = compute_cvd_slope(df, lookback=SETTINGS.CVD_LOOKBACK)

        cmf_v = np.zeros(n)
        if getattr(SETTINGS, "USE_CMF", False):
            cmf_v = compute_cmf(df, length=SETTINGS.CMF_LENGTH)

        srsi_v = np.full(n, 50.0)
        if getattr(SETTINGS, "USE_STOCH_RSI", False):
            srsi_v = compute_stoch_rsi(df, rsi_length=SETTINGS.STOCH_RSI_LENGTH,
                stoch_length=SETTINGS.STOCH_RSI_STOCH_LENGTH, smooth=SETTINGS.STOCH_RSI_SMOOTH)

        # ── ANA DONGU ──
        for i in range(n):
            if not np.isnan(ph[i]): self.hi_levels.append([ph[i], i, False]); self._prune(self.hi_levels)
            if not np.isnan(pl[i]): self.lo_levels.append([pl[i], i, False]); self._prune(self.lo_levels)
            if not np.isnan(mph[i]): self.last_minor_high = mph[i]
            if not np.isnan(mpl[i]): self.last_minor_low = mpl[i]

            if i < max(sl*2, 50) or np.isnan(atr_v[i]) or atr_v[i] <= 0:
                continue

            bull_sw = bear_sw = None
            for lvl in reversed(self.lo_levels):
                price, bidx, used = lvl
                if used or i - bidx > SETTINGS.MAX_SWEEP_DISTANCE_BARS: continue
                if close[i] < price: lvl[2] = True
                elif low[i] < price and close[i] > price:
                    lvl[2] = True
                    if bull_sw is None: bull_sw = (price, bidx)
            for lvl in reversed(self.hi_levels):
                price, bidx, used = lvl
                if used or i - bidx > SETTINGS.MAX_SWEEP_DISTANCE_BARS: continue
                if close[i] > price: lvl[2] = True
                elif high[i] > price and close[i] < price:
                    lvl[2] = True
                    if bear_sw is None: bear_sw = (price, bidx)

            vc = 0.5
            if SETTINGS.USE_VOLUME_FILTER and vol_ma[i] and not np.isnan(vol_ma[i]) and vol_ma[i] > 0:
                ratio = volume[i] / vol_ma[i]
                vc = min(max((ratio - 1.0) / max(SETTINGS.VOLUME_SPIKE_MULT - 1.0, 0.1), 0.0), 1.0)

            def _htf(d):
                if self.htf_bias == 0: return 0.5
                return 1.0 if self.htf_bias == d else 0.0

            ca = adx_v[i]; cdp = di_p[i]; cdm = di_m[i]; cst = st_v[i]
            clr = float(lr_v[i])
            cvw = float(wv[i]) if not np.isnan(wv[i]) else 0.0; cvwd = int(wdv[i])
            ct = float(tv[i]); cts = float(tsv[i])
            cmb = float(mdb[i]); cmd = float(mdd[i])
            cp = float(poc_v[i]) if not np.isnan(poc_v[i]) else 0.0
            cvh = float(vah_v[i]) if not np.isnan(vah_v[i]) else 0.0
            cvl = float(val_v[i]) if not np.isnan(val_v[i]) else 0.0
            cbb_ = bool(bbo[i]); cbd = bool(dbo[i]); crg = str(rgv[i])
            clux_b = bool(lux_bull_sweep[i]); clux_s = bool(lux_bear_sweep[i])
            cbos_b = bool(bull_bos[i]); cbos_s = bool(bear_bos[i])
            cstk_b = bool(stacked_bull[i]); cstk_s = bool(stacked_bear[i])
            cttm = int(ttm_dir[i])
            cichi = int(ichi_v[i]); ccvd = int(cvd_v[i])
            ccmf = float(cmf_v[i]); csrsi = float(srsi_v[i])

            curr_rsi_div = 0
            if (bull_sw or bear_sw) and SETTINGS.USE_RSI_DIVERGENCE:
                curr_rsi_div = self.rsi_div if (i == n-1 and self.rsi_div != 0) else int(rsi_div_v[i])

            wba = False; wda = False; wdd = 0
            if (bull_sw or bear_sw) and SETTINGS.USE_WHALE_PROFILE:
                wba = bool(wba_v[i])
                wda = bool(wda_v[i])
                wdd = int(wdd_v[i])

            ems = SETTINGS.MIN_SWEEP_SCORE
            if SETTINGS.USE_MARKET_REGIME and crg == "RANGE": ems += SETTINGS.REGIME_RANGE_SCORE_THRESHOLD_ADD

            def score(d, level, rdiv):
                rng = high[i] - low[i]
                if rng <= 0: return 0.0

                # --- (Analiz) Rejim kapısı: TRANSITION yalnız gerçek sweep'e açık ---
                # 27 TRANSITION işlemi 0 galibiyet ≈ -11.5R → neredeyse tamamen kapalı.
                # USE_MARKET_REGIME kapalıysa rgv hep TRANSITION kalır; o durumda
                # kapı da devre dışı bırakılır (yoksa her sinyal bloklanırdı).
                if SETTINGS.REGIME_GATE_ENABLED and SETTINGS.USE_MARKET_REGIME and crg in SETTINGS.REGIME_BLOCKED:
                    if crg == "TRANSITION":
                        _ok = (ca >= SETTINGS.REGIME_TRANSITION_MIN_ADX)
                        if _ok and SETTINGS.REGIME_TRANSITION_REQUIRE_SWEEP:
                            _ok = ((d == 1 and clux_b) or (d == -1 and clux_s))
                        if _ok and SETTINGS.REGIME_TRANSITION_REQUIRE_CMF:
                            _ok = ((d == 1 and ccmf >= SETTINGS.CMF_THRESHOLD)
                                   or (d == -1 and ccmf <= -SETTINGS.CMF_THRESHOLD))
                        if not _ok:
                            return 0.0
                    else:
                        return 0.0

                # --- (Analiz) Fade-stretch vetosu: aşırı gerilime karşı giriş yok ---
                # 28 Ağu kümesi (SUI/ARB/WAL/MANA A/A+ SHORT, 5/5 SL) bu desendi.
                if SETTINGS.FADE_STRETCH_VETO_ENABLED and SETTINGS.USE_LINREG_OSC:
                    _fst = SETTINGS.FADE_STRETCH_VETO_THRESHOLD
                    if (d == 1 and clr <= -_fst) or (d == -1 and clr >= _fst):
                        return 0.0

                # --- (Analiz) Chase vetosu: kovalama yalnız izinli rejimlerde ---
                # Kayıp LONG'lar stoch 87-100, kayıp SHORT'lar 0-34'te giriyordu;
                # 2 galibiyet de EXPANSION'da chase ile geldi.
                if SETTINGS.CHASE_VETO_ENABLED and SETTINGS.USE_STOCH_RSI:
                    _chase = ((d == 1 and csrsi >= SETTINGS.STOCH_RSI_OVERBOUGHT)
                              or (d == -1 and csrsi <= SETTINGS.STOCH_RSI_OVERSOLD))
                    if _chase and crg not in SETTINGS.CHASE_ALLOWED_REGIMES:
                        return 0.0

                # --- (Analiz) Çift taraflı çelişki: pivot gürültüsü tespiti ---
                # A+ kayıplarında whale/FVG/OB iki taraf birden yanıyordu.
                _bs_conflict = 0
                if SETTINGS.BOTH_SIDES_CONFLICT_ENABLED:
                    _bs_conflict = ((1 if (wba and wda) else 0)
                                    + (1 if (fgb[i] and fgd[i]) else 0)
                                    + (1 if (obb[i] and obd[i]) else 0))
                    if _bs_conflict >= SETTINGS.BOTH_SIDES_CONFLICT_VETO_COUNT:
                        return 0.0

                wick = (min(open_[i], close[i]) - low[i]) if d == 1 else (high[i] - max(open_[i], close[i]))
                reclaim = (close[i] - level) if d == 1 else (level - close[i])
                cpos = (close[i] - low[i]) / rng
                cpp = cpos if d == 1 else 1.0 - cpos
                wc = min(max(wick, 0.0) / atr_v[i], 1.0)
                rc = min(max(reclaim, 0.0) / atr_v[i], 1.0)
                base = (wc*SETTINGS.SCORE_W_WICK + rc*SETTINGS.SCORE_W_RECLAIM +
                        cpp*SETTINGS.SCORE_W_CLOSE_POS + vc*SETTINGS.SCORE_W_VOLUME +
                        _htf(d)*SETTINGS.SCORE_W_HTF) * 100.0

                # Çift taraflı çelişki tavana düşmediyse ceza uygula
                if _bs_conflict > 0:
                    base -= SETTINGS.BOTH_SIDES_CONFLICT_PENALTY * _bs_conflict

                if SETTINGS.USE_RSI_DIVERGENCE and rdiv == d: base += SETTINGS.RSI_DIV_BONUS
                if SETTINGS.USE_FUNDING_FILTER and self.funding_info:
                    sq = self.funding_info.get("squeeze_risk")
                    if sq == "SHORT_SQUEEZE" and d == 1: base += SETTINGS.FUNDING_SQUEEZE_BONUS
                    elif sq == "LONG_SQUEEZE" and d == -1: base += SETTINGS.FUNDING_SQUEEZE_BONUS
                    if self.funding_info.get("extreme_funding"):
                        fr = self.funding_info.get("funding_rate", 0)
                        if fr > 0 and d == 1: base -= SETTINGS.FUNDING_EXTREME_PENALTY
                        elif fr < 0 and d == -1: base -= SETTINGS.FUNDING_EXTREME_PENALTY

                # --- #10 Setup Validator: ST + PMax + HTF + karşı div + CVD ---
                # Aktif bileşenlerin en az yarısı sinyale ters ise hard reject
                if SETTINGS.SETUP_VALIDATOR_ENABLED:
                    _sv_contra = 0; _sv_active = 0
                    if SETTINGS.USE_SUPERTREND_SCORE_BONUS:
                        _sv_active += 1
                        if cst == -d: _sv_contra += 1
                    if SETTINGS.USE_PMAX and SETTINGS.USE_PMAX_SCORE_BONUS:
                        _sv_active += 1
                        if pmxd[i] == -d: _sv_contra += 1
                    if SETTINGS.USE_HTF_FILTER and self.htf_bias != 0:
                        _sv_active += 1
                        if self.htf_bias == -d: _sv_contra += 1
                    # Karşı diverjans baskınlığı (sinyal yönüne karşı div açık ara güçlü)
                    if SETTINGS.USE_MULTI_DIV:
                        _sv_active += 1
                        _sv_contra_div = cmd if d == 1 else cmb
                        _sv_same_div = cmb if d == 1 else cmd
                        if (_sv_contra_div > _sv_same_div * SETTINGS.SETUP_VALIDATOR_DIV_RATIO
                                and _sv_contra_div >= SETTINGS.SETUP_VALIDATOR_DIV_MIN):
                            _sv_contra += 1
                    # CVD emir akışı çelişkisi
                    if getattr(SETTINGS, "USE_CVD", False):
                        _sv_active += 1
                        if ccvd == -d: _sv_contra += 1
                    if _sv_active >= 3 and _sv_contra >= _sv_active * 0.5:
                        return 0.0

                # --- #9 HTF Sert Filtre ---
                if SETTINGS.USE_HTF_FILTER and self.htf_bias != 0 and self.htf_bias == -d:
                    if SETTINGS.HTF_FILTER_MODE == "strict_reject":
                        return 0.0
                    elif SETTINGS.HTF_FILTER_MODE == "counter_trend_label":
                        base -= SETTINGS.HTF_COUNTER_TREND_PENALTY

                if SETTINGS.USE_ADX_FILTER:
                    if SETTINGS.ADX_FILTER_MODE == "range_only" and ca >= SETTINGS.ADX_THRESHOLD: return 0.0
                    elif SETTINGS.ADX_FILTER_MODE == "trend_direction" and ca > SETTINGS.ADX_THRESHOLD:
                        if d == 1 and cdp <= cdm: return 0.0
                        elif d == -1 and cdm <= cdp: return 0.0
                if SETTINGS.USE_ADX_SCORE_BONUS and ca >= SETTINGS.ADX_THRESHOLD:
                    if d == 1 and cdp > cdm: base += SETTINGS.ADX_SCORE_BONUS
                    elif d == -1 and cdm > cdp: base += SETTINGS.ADX_SCORE_BONUS
                    # --- #2 DI Cezası: ADX eşik üstü + DI sinyale ters ---
                    # ADX güçlüyse çelişki daha ciddi → kademeli ceza
                    else:
                        base -= (SETTINGS.DI_CONTRA_PENALTY_STRONG
                                 if ca >= SETTINGS.DI_CONTRA_STRONG_ADX
                                 else SETTINGS.DI_CONTRA_PENALTY)
                if SETTINGS.USE_SUPERTREND_FILTER and cst != d: return 0.0
                if SETTINGS.USE_SUPERTREND_SCORE_BONUS:
                    if cst == d: base += SETTINGS.SUPERTREND_SCORE_BONUS
                    elif cst == -d: base -= SETTINGS.SUPERTREND_CONTRA_PENALTY
                if SETTINGS.USE_PMAX_FILTER and SETTINGS.USE_PMAX and pmxd[i] != d: return 0.0
                if SETTINGS.USE_PMAX_SCORE_BONUS and SETTINGS.USE_PMAX:
                    if pmxd[i] == d: base += SETTINGS.PMAX_SCORE_BONUS
                    elif pmxd[i] == -d: base -= SETTINGS.PMAX_CONTRA_PENALTY
                    if pmxc[i] == d: base += SETTINGS.PMAX_CROSS_BONUS

                # --- #3 Çifte Çelişki: Supertrend + PMax ikisi birden ters ---
                if SETTINGS.USE_SUPERTREND_SCORE_BONUS and SETTINGS.USE_PMAX_SCORE_BONUS:
                    if cst == -d and pmxd[i] == -d:
                        base -= SETTINGS.DOUBLE_CONTRA_PENALTY

                if SETTINGS.USE_BREAKOUT_FILTER:
                    if d == 1 and cbb_: base += SETTINGS.BREAKOUT_SCORE_BONUS
                    elif d == -1 and cbd: base += SETTINGS.BREAKOUT_SCORE_BONUS
                if SETTINGS.USE_VOLUME_PROFILE and not np.isnan(poc_v[i]) and atr_v[i] > 0:
                    if abs(close[i] - poc_v[i]) / atr_v[i] <= 0.5: base += SETTINGS.VP_POC_SCORE_BONUS
                if SETTINGS.USE_LINREG_OSC:
                    if d == 1 and clr >= SETTINGS.LINREG_UPPER: base -= SETTINGS.LINREG_PENALTY
                    elif d == -1 and clr <= SETTINGS.LINREG_LOWER: base -= SETTINGS.LINREG_PENALTY
                    elif d == 1 and clr <= SETTINGS.LINREG_LOWER: base += SETTINGS.LINREG_PENALTY
                    elif d == -1 and clr >= SETTINGS.LINREG_UPPER: base += SETTINGS.LINREG_PENALTY
                if SETTINGS.USE_SWING_VWAP and cvwd != 0:
                    if d == cvwd: base += SETTINGS.SWING_VWAP_SCORE_BONUS
                    elif d == -cvwd: base -= SETTINGS.SWING_VWAP_CONTRA_PENALTY
                if SETTINGS.USE_TSI and ct != 0.0:
                    if d == 1 and ct > 0: base += SETTINGS.TSI_SCORE_BONUS
                    elif d == -1 and ct < 0: base += SETTINGS.TSI_SCORE_BONUS
                    if d == 1 and ct > cts: base += SETTINGS.TSI_SIGNAL_BONUS
                    elif d == -1 and ct < cts: base += SETTINGS.TSI_SIGNAL_BONUS

                # --- #7 Weighted Divergence + #5 Karşı Diverjans Kademeli ---
                if SETTINGS.USE_MULTI_DIV:
                    # Karşı diverjans baskınlığı kontrolü (mutlak eşik → hard reject)
                    if d == 1 and cmd > cmb * SETTINGS.CONTRA_DIV_DOMINANCE_RATIO and cmd >= SETTINGS.CONTRA_DIV_DOMINANCE_MIN:
                        return 0.0
                    elif d == -1 and cmb > cmd * SETTINGS.CONTRA_DIV_DOMINANCE_RATIO and cmb >= SETTINGS.CONTRA_DIV_DOMINANCE_MIN:
                        return 0.0
                    # Uyumlu diverjans bonusu (ağırlıklı toplam zaten cmb/cmd olarak gelir)
                    if d == 1 and cmb > 0: base += min(cmb * SETTINGS.MULTI_DIV_PER_INDICATOR, SETTINGS.MULTI_DIV_MAX_BONUS)
                    elif d == -1 and cmd > 0: base += min(cmd * SETTINGS.MULTI_DIV_PER_INDICATOR, SETTINGS.MULTI_DIV_MAX_BONUS)
                    # Karşı yön diverjans kademeli cezası (#5)
                    contra_div_count = cmd if d == 1 else cmb
                    if contra_div_count > 0:
                        contra_penalty = 0.0
                        for tier_max, tier_pen in SETTINGS.CONTRA_DIV_PENALTY_TIERS:
                            if contra_div_count <= tier_max:
                                contra_penalty = tier_pen
                                break
                        else:
                            contra_penalty = SETTINGS.CONTRA_DIV_PENALTY_TIERS[-1][1]
                        base -= min(contra_penalty, SETTINGS.CONTRA_DIV_MAX_PENALTY)

                if SETTINGS.USE_WHALE_PROFILE:
                    if d == 1 and wba: base += SETTINGS.WHALE_ABSORPTION_SCORE_BONUS
                    elif d == -1 and wda: base += SETTINGS.WHALE_ABSORPTION_SCORE_BONUS
                    if d == 1:
                        if wdd == 1: base += SETTINGS.WHALE_DELTA_SCORE_BONUS
                        elif wdd == -1: base -= SETTINGS.WHALE_DELTA_SCORE_BONUS
                    elif d == -1:
                        if wdd == -1: base += SETTINGS.WHALE_DELTA_SCORE_BONUS
                        elif wdd == 1: base -= SETTINGS.WHALE_DELTA_SCORE_BONUS
                if SETTINGS.USE_MARKET_REGIME and crg == "RANGE": base -= SETTINGS.REGIME_RANGE_SCORE_PENALTY
                # (Analiz) FVG/OB/LUX/BOS/STACKED bonusları artık tek grupta
                # toplanıp CONFLUENCE_STRUCTURE_CAP ile sınırlanır — A+ 92'lik
                # sahte confluence sinyalleri (5/5 kayıp) böylece imkansızlaşır.
                _conf_struct = 0.0
                if SETTINGS.USE_FVG:
                    if d == 1 and fgb[i]: _conf_struct += SETTINGS.FVG_SCORE_BONUS
                    elif d == -1 and fgd[i]: _conf_struct += SETTINGS.FVG_SCORE_BONUS
                if SETTINGS.USE_ORDER_BLOCK:
                    if d == 1 and obb[i]: _conf_struct += SETTINGS.OB_SCORE_BONUS
                    elif d == -1 and obd[i]: _conf_struct += SETTINGS.OB_SCORE_BONUS
                if SETTINGS.USE_EMA_CROSS:
                    if ecv[i] == d: base += SETTINGS.EMA_CROSS_SCORE_BONUS
                    if epv[i] == d: base += SETTINGS.EMA_POSITION_BONUS
                    elif epv[i] == -d: base -= SETTINGS.EMA_POSITION_CONTRA_PENALTY
                if SETTINGS.USE_ENGULFING:
                    eg = env[i]
                    if d == 1 and eg > 0: base += SETTINGS.ENGULFING_SCORE_BONUS if eg == 2 else SETTINGS.ENGULFING_BASIC_BONUS
                    elif d == -1 and eg < 0: base += SETTINGS.ENGULFING_SCORE_BONUS if eg == -2 else SETTINGS.ENGULFING_BASIC_BONUS
                
                if getattr(SETTINGS, "USE_LUX_LIQUIDITY", False):
                    if d == 1 and clux_b: _conf_struct += getattr(SETTINGS, "LUX_LIQ_SCORE_BONUS", 10.0)
                    elif d == -1 and clux_s: _conf_struct += getattr(SETTINGS, "LUX_LIQ_SCORE_BONUS", 10.0)
                if getattr(SETTINGS, "USE_BOS", False):
                    if d == 1 and cbos_b: _conf_struct += getattr(SETTINGS, "BOS_SCORE_BONUS", 9.0)
                    elif d == -1 and cbos_s: _conf_struct += getattr(SETTINGS, "BOS_SCORE_BONUS", 9.0)
                if getattr(SETTINGS, "USE_STACKED_OB_FVG", False):
                    if d == 1 and cstk_b: _conf_struct += getattr(SETTINGS, "STACKED_OB_FVG_SCORE_BONUS", 9.0)
                    elif d == -1 and cstk_s: _conf_struct += getattr(SETTINGS, "STACKED_OB_FVG_SCORE_BONUS", 9.0)
                base += min(_conf_struct, SETTINGS.CONFLUENCE_STRUCTURE_CAP)

                # --- TTM Squeeze yon uyumu ---
                if getattr(SETTINGS, "USE_TTM_SQUEEZE", False):
                    if cttm == d: base += SETTINGS.TTM_SQUEEZE_SCORE_BONUS
                    elif cttm == -d: base -= SETTINGS.TTM_SQUEEZE_CONTRA_PENALTY
                # --- Ichimoku bulut uyumu ---
                if getattr(SETTINGS, "USE_ICHIMOKU", False):
                    if cichi == d: base += SETTINGS.ICHIMOKU_SCORE_BONUS
                    elif cichi == -d: base -= SETTINGS.ICHIMOKU_CONTRA_PENALTY
                # --- Gercek CVD (emir akisi) uyumu ---
                if getattr(SETTINGS, "USE_CVD", False):
                    if ccvd == d: base += SETTINGS.CVD_SCORE_BONUS
                    elif ccvd == -d: base -= SETTINGS.CVD_CONTRA_PENALTY
                # --- CMF para akisi uyumu ---
                if getattr(SETTINGS, "USE_CMF", False):
                    if d == 1 and ccmf >= SETTINGS.CMF_THRESHOLD: base += SETTINGS.CMF_SCORE_BONUS
                    elif d == -1 and ccmf <= -SETTINGS.CMF_THRESHOLD: base += SETTINGS.CMF_SCORE_BONUS
                    elif d == 1 and ccmf <= -SETTINGS.CMF_THRESHOLD: base -= SETTINGS.CMF_CONTRA_PENALTY
                    elif d == -1 and ccmf >= SETTINGS.CMF_THRESHOLD: base -= SETTINGS.CMF_CONTRA_PENALTY
                # --- StochRSI: pullback bonusu / kovalama cezasi ---
                if getattr(SETTINGS, "USE_STOCH_RSI", False):
                    # Long sinyal asiri satimdan donuyorsa (pullback kalitesi) bonus,
                    # asiri alim bolgesinde long kovaliyorsa ceza (ve tersi)
                    if d == 1 and csrsi <= SETTINGS.STOCH_RSI_OVERSOLD: base += SETTINGS.STOCH_RSI_PULLBACK_BONUS
                    elif d == -1 and csrsi >= SETTINGS.STOCH_RSI_OVERBOUGHT: base += SETTINGS.STOCH_RSI_PULLBACK_BONUS
                    elif d == 1 and csrsi >= SETTINGS.STOCH_RSI_OVERBOUGHT: base -= SETTINGS.STOCH_RSI_CHASE_PENALTY
                    elif d == -1 and csrsi <= SETTINGS.STOCH_RSI_OVERSOLD: base -= SETTINGS.STOCH_RSI_CHASE_PENALTY

                # --- #4 ADX Kademeli Ceza (eski düz ceza yerine) ---
                for adx_tier_max, adx_tier_pen in SETTINGS.ADX_PENALTY_TIERS:
                    if ca < adx_tier_max:
                        base -= adx_tier_pen
                        break

                # --- #1 Trend Conflict Engine: 5 motor oylaması → maks skor sınırlama ---
                if SETTINGS.CONFLICT_ENGINE_ENABLED:
                    _ce_agree = 0; _ce_total = 0
                    # Supertrend
                    if SETTINGS.USE_SUPERTREND_SCORE_BONUS:
                        _ce_total += 1
                        if cst == d: _ce_agree += 1
                    # PMax
                    if SETTINGS.USE_PMAX and SETTINGS.USE_PMAX_SCORE_BONUS:
                        _ce_total += 1
                        if pmxd[i] == d: _ce_agree += 1
                    # HTF
                    if SETTINGS.USE_HTF_FILTER and self.htf_bias != 0:
                        _ce_total += 1
                        if self.htf_bias == d: _ce_agree += 1
                    # EMA Position
                    if SETTINGS.USE_EMA_CROSS:
                        _ce_total += 1
                        if epv[i] == d: _ce_agree += 1
                    # BoS (Market Structure)
                    if getattr(SETTINGS, "USE_BOS", False):
                        _ce_total += 1
                        if (d == 1 and cbos_b) or (d == -1 and cbos_s): _ce_agree += 1

                    if _ce_total > 0:
                        if _ce_agree <= 1 and _ce_total >= 3:
                            return 0.0  # ≤1/N (N>=3) → reject
                        cap = SETTINGS.CONFLICT_SCORE_CAPS.get(_ce_agree, 65)
                        base = min(base, cap)

                return min(max(base, 0.0), 100.0)

            bq = dq = False; bs = ds = 0.0
            if bull_sw: bs = score(1, bull_sw[0], curr_rsi_div); bq = bs >= ems
            if bear_sw: ds = score(-1, bear_sw[0], curr_rsi_div); dq = ds >= ems

            def _mk(d, level, s, wick):
                fr = self.funding_info.get("funding_rate", 0.0)
                sq = self.funding_info.get("squeeze_risk", "")

                # --- #9 HTF Counter Trend tespiti ---
                _htf_ct = (SETTINGS.USE_HTF_FILTER and self.htf_bias != 0 and self.htf_bias == -d)

                # --- #8 Confluence Puanı (yapısal grup tavanlı — score() ile uyumlu) ---
                _conf_struct = 0.0
                if getattr(SETTINGS, "USE_LUX_LIQUIDITY", False) and ((d == 1 and clux_b) or (d == -1 and clux_s)):
                    _conf_struct += SETTINGS.LUX_LIQ_SCORE_BONUS
                if getattr(SETTINGS, "USE_BOS", False) and ((d == 1 and cbos_b) or (d == -1 and cbos_s)):
                    _conf_struct += SETTINGS.BOS_SCORE_BONUS
                if getattr(SETTINGS, "USE_STACKED_OB_FVG", False) and ((d == 1 and cstk_b) or (d == -1 and cstk_s)):
                    _conf_struct += SETTINGS.STACKED_OB_FVG_SCORE_BONUS
                if SETTINGS.USE_ORDER_BLOCK and ((d == 1 and obb[i]) or (d == -1 and obd[i])):
                    _conf_struct += SETTINGS.OB_SCORE_BONUS
                if SETTINGS.USE_FVG and ((d == 1 and fgb[i]) or (d == -1 and fgd[i])):
                    _conf_struct += SETTINGS.FVG_SCORE_BONUS
                _conf = min(_conf_struct, SETTINGS.CONFLUENCE_STRUCTURE_CAP)
                if SETTINGS.USE_EMA_CROSS and ecv[i] == d:
                    _conf += SETTINGS.EMA_CROSS_SCORE_BONUS
                if SETTINGS.USE_SWING_VWAP and cvwd == d:
                    _conf += SETTINGS.SWING_VWAP_SCORE_BONUS
                if SETTINGS.USE_SUPERTREND_SCORE_BONUS and cst == d:
                    _conf += SETTINGS.SUPERTREND_SCORE_BONUS
                if SETTINGS.USE_PMAX_SCORE_BONUS and SETTINGS.USE_PMAX and pmxd[i] == d:
                    _conf += SETTINGS.PMAX_SCORE_BONUS

                # --- #14 Setup Type Sınıflandırması ---
                _stype = "Liq Sweep"
                _all_trend = (cst == d and (not SETTINGS.USE_PMAX or pmxd[i] == d)
                              and (not SETTINGS.USE_HTF_FILTER or self.htf_bias == 0 or self.htf_bias == d))
                _has_bos = getattr(SETTINGS, "USE_BOS", False) and ((d == 1 and cbos_b) or (d == -1 and cbos_s))
                _has_bo = (d == 1 and cbb_) or (d == -1 and cbd)
                _has_ob_fvg = ((SETTINGS.USE_ORDER_BLOCK and ((d == 1 and obb[i]) or (d == -1 and obd[i])))
                               or (SETTINGS.USE_FVG and ((d == 1 and fgb[i]) or (d == -1 and fgd[i]))))
                _is_range = (SETTINGS.USE_MARKET_REGIME and crg == "RANGE")
                _has_div = (SETTINGS.USE_MULTI_DIV and ((d == 1 and cmb > 0) or (d == -1 and cmd > 0)))
                _linreg_extreme = (SETTINGS.USE_LINREG_OSC and
                                   ((d == 1 and clr <= SETTINGS.LINREG_LOWER) or (d == -1 and clr >= SETTINGS.LINREG_UPPER)))

                if _has_bos and _has_ob_fvg:
                    _stype = "BoS Retest"
                elif _all_trend and not _is_range and ca >= SETTINGS.TREND_CONTINUATION_MIN_ADX:
                    _stype = "Trend Continuation"
                elif _all_trend and ca < SETTINGS.TREND_CONTINUATION_MIN_ADX:
                    # ADX < eşik → trend yok; farklı etiket, farklı değerlendirme
                    _stype = "Weak Trend Drift"
                elif _has_bo:
                    _stype = "Breakout"
                elif _is_range and SETTINGS.USE_VOLUME_PROFILE and not np.isnan(poc_v[i]):
                    _stype = "Range Bounce"
                elif _linreg_extreme and _has_div:
                    _stype = "Mean Reversion"
                elif _htf_ct:
                    _stype = "Counter Trend"

                # --- (Analiz) Setup tip kapıları ---
                # BoS Retest: 9 işlem 0W/7L = -7R → tamamen kapalı.
                if _stype in SETTINGS.DISABLED_SETUP_TYPES:
                    return None
                # Weak Trend Drift: yalnız ADX >= WTD_MIN_ADX'de yaşar
                # (kaybeden WTD'lerin çoğu ADX 10-16 bandındaydı; kazanan SUI 18.6).
                if _stype == "Weak Trend Drift" and ca < SETTINGS.WTD_MIN_ADX:
                    return None
                # Trend Continuation: zayıf DI farkı yetmez (kazanan BTW 44,
                # kaybeden COW 2.7 idi).
                if _stype == "Trend Continuation" and abs(cdp - cdm) < SETTINGS.TREND_CONTINUATION_MIN_DI_SPREAD:
                    return None

                # --- #15 Weak Trend Drift skor kesintisi (düşük ADX'li trend sinyali) ---
                if _stype == "Weak Trend Drift":
                    s = max(s - SETTINGS.WEAK_TREND_DRIFT_PENALTY, 0.0)
                    # (Analiz) bq kontrolü kesintiden ÖNCEKİ skorla yapıldığı için
                    # kesinti sonrası eşik altına düşen sinyal burada reddedilir;
                    # yoksa 43 puanlı sinyal MIN_SWEEP_SCORE=50'i delip geçer.
                    if s < ems:
                        return None

                # --- #13 Grade & Probability (kesinti düşülmüş skorla) ---
                _grade = "D"; _prob = 0.25
                for _g_min, _g_lbl, _g_prob in SETTINGS.GRADE_TABLE:
                    if s >= _g_min:
                        _grade = _g_lbl; _prob = _g_prob; break

                return SweepSetup(d, level, s, wick, i,
                    curr_rsi_div, fr, sq, ca, cdp, cdm, cst,
                    cp, cvh, cvl, clr, cvw, cvwd,
                    1 if cbb_ else (-1 if cbd else 0),
                    ct, cts, cmb, cmd,
                    wba, wda, wdd,
                    int(pmxd[i]) if SETTINGS.USE_PMAX else 0,
                    int(pmxc[i]) if SETTINGS.USE_PMAX else 0,
                    crg,
                    bool(fgb[i]), bool(fgd[i]),
                    bool(obb[i]), bool(obd[i]),
                    int(ecv[i]), int(epv[i]), int(env[i]),
                    clux_b if d == 1 else clux_s,
                    1 if cbos_b else (-1 if cbos_s else 0),
                    1 if cstk_b else (-1 if cstk_s else 0),
                    ttm_squeeze=cttm,
                    ichimoku_trend=cichi,
                    cvd_trend=ccvd,
                    cmf_value=ccmf,
                    stoch_rsi=csrsi,
                    confluence_score=_conf,
                    grade=_grade,
                    probability=_prob,
                    setup_type=_stype,
                    htf_counter_trend=_htf_ct,
                )

            if SETTINGS.REQUIRE_CHOCH:
                if self.pending and i - self.pending["start_bar"] > SETTINGS.CONFIRM_WINDOW_BARS:
                    self.pending = None
                if bq:
                    self.pending = {"dir": 1, "start_bar": i, "level": bull_sw[0],
                        "wick": low[i], "score": bs, "wba": wba, "wda": wda, "wdd": wdd,
                        "pmh": self.last_minor_high, "pml": self.last_minor_low}
                if dq:
                    self.pending = {"dir": -1, "start_bar": i, "level": bear_sw[0],
                        "wick": high[i], "score": ds, "wba": wba, "wda": wda, "wdd": wdd,
                        "pmh": self.last_minor_high, "pml": self.last_minor_low}

                if (self.pending and self.pending["dir"] == 1
                        and not np.isnan(self.pending.get("pmh", np.nan))
                        and close[i] > self.pending["pmh"]):
                    _sig = _mk(1, self.pending["level"], self.pending["score"], self.pending["wick"])
                    self.pending = None
                    if _sig is not None:
                        yield _sig
                elif (self.pending and self.pending["dir"] == -1
                        and not np.isnan(self.pending.get("pml", np.nan))
                        and close[i] < self.pending["pml"]):
                    _sig = _mk(-1, self.pending["level"], self.pending["score"], self.pending["wick"])
                    self.pending = None
                    if _sig is not None:
                        yield _sig
            else:
                if bq:
                    _sig = _mk(1, bull_sw[0], bs, low[i])
                    if _sig is not None:
                        yield _sig
                if dq:
                    _sig = _mk(-1, bear_sw[0], ds, high[i])
                    if _sig is not None:
                        yield _sig
