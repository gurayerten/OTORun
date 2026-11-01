"""
ImpulseEngine — Sweep gerektirmeyen güçlü fiyat hareketlerini yakalar.

3 alt strateji:
  1. Compression Breakout: Sıkışma sonrası hacimli kırılım
  2. Volume Spike Impulse: Devasa hacim patlaması + güçlü fiyat hareketi
  3. BoS Breakout: Yapısal seviyenin kapanışla kırılması

SweepEngine ile paralel çalışır, aynı SweepSetup dataclass'ını üretir.
Downstream (orderbook, risk, telegram) kodunda sıfır değişiklik.
"""
import numpy as np

from config import SETTINGS
from logger import get_logger
from strategy import (
    SweepSetup,
    atr,
    compute_adx_di,
    compute_supertrend,
    compute_pmax,
    compute_breakout,
    compute_market_regime,
    compute_engulfing,
    compute_ema_cross,
    compute_volume_profile,
    compute_linreg_osc,
    compute_swing_vwap,
    compute_tsi,
    compute_multi_divergence,
    compute_fvg_zones,
    compute_order_blocks,
    compute_lux_liquidity,
    compute_bos,
    compute_stacked_ob_fvg,
)

log = get_logger("impulse")


class ImpulseEngine:
    """Sweep gerektirmeyen momentum / breakout / retest sinyalleri üreten motor."""

    def __init__(self, htf_bias: int = 0, rsi_div: int = 0, funding_info: dict = None):
        self.htf_bias = htf_bias
        self.rsi_div = int(rsi_div) if rsi_div else 0
        self.funding_info = funding_info or {}

    def iter_signals(self, df):
        """SweepEngine ile aynı imza: DataFrame alır, SweepSetup yield eder."""
        if "volume" not in df.columns:
            df["volume"] = 0.0

        n = len(df)
        if n < 60:
            return

        # ── PRECOMPUTE: tüm göstergeler (SweepEngine ile aynı) ──
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        open_ = df["open"].values
        volume = df["volume"].values

        atr_v = atr(df, SETTINGS.ATR_LEN).values
        vol_ma = df["volume"].rolling(SETTINGS.VOLUME_MA_LEN).mean().values

        adx_v, di_p, di_m = compute_adx_di(df, length=SETTINGS.ADX_LENGTH)
        st_v, _, _ = compute_supertrend(
            df, period=SETTINGS.SUPERTREND_PERIOD,
            multiplier=SETTINGS.SUPERTREND_MULTIPLIER,
            change_atr=SETTINGS.SUPERTREND_CHANGE_ATR,
        )

        bbo = np.zeros(n, dtype=bool)
        dbo = np.zeros(n, dtype=bool)
        if SETTINGS.USE_BREAKOUT_FILTER:
            bbo, dbo = compute_breakout(
                df, prd=SETTINGS.BREAKOUT_PRD, bo_len=SETTINGS.BREAKOUT_BO_LEN,
                cwidth_pct=SETTINGS.BREAKOUT_CWIDTH_PCT, mintest=SETTINGS.BREAKOUT_MINTEST,
            )

        pmxd = np.zeros(n, dtype=int)
        if SETTINGS.USE_PMAX:
            pv_, mv_, pmxd = compute_pmax(
                df, atr_length=SETTINGS.PMAX_ATR_LENGTH,
                multiplier=SETTINGS.PMAX_ATR_MULTIPLIER,
                ma_type=SETTINGS.PMAX_MA_TYPE,
                ma_length=SETTINGS.PMAX_MA_LENGTH,
                change_atr=SETTINGS.PMAX_CHANGE_ATR,
                normalize=SETTINGS.PMAX_NORMALIZE,
            )

        rgv = np.array(["TRANSITION"] * n, dtype=object)
        if SETTINGS.USE_MARKET_REGIME:
            rgv, _, _ = compute_market_regime(
                df, adx_v,
                bb_length=SETTINGS.REGIME_BB_LENGTH,
                bb_mult=SETTINGS.REGIME_BB_MULT,
                squeeze_percentile=SETTINGS.REGIME_BB_SQUEEZE_PERCENTILE,
                adx_range_thresh=SETTINGS.REGIME_ADX_RANGE_THRESHOLD,
                adx_trend_thresh=SETTINGS.REGIME_ADX_TREND_THRESHOLD,
            )

        ecv = np.zeros(n, dtype=int)
        epv = np.zeros(n, dtype=int)
        if SETTINGS.USE_EMA_CROSS:
            ecv, epv = compute_ema_cross(
                df, fast_len=SETTINGS.EMA_FAST_LEN, slow_len=SETTINGS.EMA_SLOW_LEN,
                cross_lookback=SETTINGS.EMA_CROSS_LOOKBACK,
            )

        env = np.zeros(n, dtype=int)
        if SETTINGS.USE_ENGULFING:
            env = compute_engulfing(
                df, body_mult=SETTINGS.ENGULFING_BODY_MULT,
                body_lookback=SETTINGS.ENGULFING_LOOKBACK,
            )

        bull_bos = np.zeros(n, dtype=bool)
        bear_bos = np.zeros(n, dtype=bool)
        if getattr(SETTINGS, "USE_BOS", False):
            bull_bos, bear_bos = compute_bos(
                df, pivot_length=getattr(SETTINGS, "BOS_PIVOT_LENGTH", 1),
                max_age_bars=getattr(SETTINGS, "BOS_MAX_AGE_BARS", 50),
            )

        fgb = np.zeros(n, dtype=bool)
        fgd = np.zeros(n, dtype=bool)
        if SETTINGS.USE_FVG:
            fgb, fgd = compute_fvg_zones(
                df, lookback=SETTINGS.FVG_LOOKBACK, proximity_atr=SETTINGS.FVG_PROXIMITY_ATR,
            )

        obb = np.zeros(n, dtype=bool)
        obd = np.zeros(n, dtype=bool)
        if SETTINGS.USE_ORDER_BLOCK:
            obb, obd = compute_order_blocks(
                df, lookback=SETTINGS.OB_LOOKBACK,
                impulse_atr_mult=SETTINGS.OB_IMPULSE_ATR_MULT,
                proximity_atr=SETTINGS.OB_PROXIMITY_ATR,
                max_age_bars=SETTINGS.OB_MAX_AGE_BARS,
            )

        poc_v = vah_v = val_v = np.full(n, np.nan)
        if SETTINGS.USE_VOLUME_PROFILE:
            poc_v, vah_v, val_v = compute_volume_profile(
                df, num_bars=SETTINGS.VP_NUM_BARS,
                lookback=SETTINGS.VP_LOOKBACK, va_pct=SETTINGS.VP_VA_PCT,
                stride=SETTINGS.VP_SERIES_STRIDE,
            )

        lr_v = np.zeros(n)
        if SETTINGS.USE_LINREG_OSC:
            lr_v = compute_linreg_osc(df, length=SETTINGS.LINREG_LENGTH)

        wv = np.full(n, np.nan)
        wdv = np.zeros(n, dtype=int)
        if SETTINGS.USE_SWING_VWAP:
            wv, wdv = compute_swing_vwap(
                df, prd=SETTINGS.SWING_VWAP_PRD, apt=SETTINGS.SWING_VWAP_APT,
            )

        tv = np.zeros(n)
        tsv = np.zeros(n)
        if SETTINGS.USE_TSI:
            tv, tsv = compute_tsi(
                df, long_len=SETTINGS.TSI_LONG_LEN,
                short_len=SETTINGS.TSI_SHORT_LEN, signal_len=SETTINGS.TSI_SIGNAL_LEN,
            )

        mdb = np.zeros(n, dtype=float)
        mdd = np.zeros(n, dtype=float)
        if SETTINGS.USE_MULTI_DIV:
            mdb, mdd = compute_multi_divergence(
                df, prd=SETTINGS.MULTI_DIV_PRD,
                lookback=SETTINGS.MULTI_DIV_LOOKBACK,
                max_pp=SETTINGS.MULTI_DIV_MAX_PP,
                stride=SETTINGS.MULTI_DIV_STRIDE,
            )

        lux_bull = np.zeros(n, dtype=bool)
        lux_bear = np.zeros(n, dtype=bool)
        if getattr(SETTINGS, "USE_LUX_LIQUIDITY", False):
            lux_bull, lux_bear = compute_lux_liquidity(
                df, liq_len=getattr(SETTINGS, "LUX_LIQ_LENGTH", 7),
                liq_mar=getattr(SETTINGS, "LUX_LIQ_MARGIN", 1.45),
            )

        stacked_bull = np.zeros(n, dtype=bool)
        stacked_bear = np.zeros(n, dtype=bool)
        if getattr(SETTINGS, "USE_STACKED_OB_FVG", False) and SETTINGS.USE_FVG and SETTINGS.USE_ORDER_BLOCK:
            stacked_bull, stacked_bear = compute_stacked_ob_fvg(fgb, fgd, obb, obd)

        # BB genişliği — compression tespiti için
        bb_len = SETTINGS.REGIME_BB_LENGTH
        bb_mid = df["close"].rolling(bb_len).mean().values
        bb_std = df["close"].rolling(bb_len).std().values

        # ── ANA DÖNGÜ ──
        for i in range(max(60, bb_len + 5), n):
            if np.isnan(atr_v[i]) or atr_v[i] <= 0:
                continue

            # Mevcut bar verileri
            cst = st_v[i]
            crg = str(rgv[i])
            vol_ratio = volume[i] / vol_ma[i] if vol_ma[i] and not np.isnan(vol_ma[i]) and vol_ma[i] > 0 else 0.0
            bar_range = high[i] - low[i]
            bar_move_pct = abs(close[i] - open_[i]) / open_[i] * 100.0 if open_[i] > 0 else 0.0

            # Yön tespit: mum kapanış yönü
            if close[i] > open_[i]:
                d = 1  # Bullish
            elif close[i] < open_[i]:
                d = -1  # Bearish
            else:
                continue  # Doji — atla

            # ── ALT STRATEJİ TETİKLEME ──
            triggered = False
            setup_type = ""
            base_score = 0.0

            # --- 1. COMPRESSION BREAKOUT ---
            if not triggered:
                is_compression_breakout = False

                # Son N barda sıkışma olmuş mu?
                lb = SETTINGS.IMPULSE_LOOKBACK_COMPRESSION
                if i >= lb + 5:
                    # BB genişliğinin daralma oranı
                    recent_bw = bb_std[i-lb:i]
                    valid_bw = recent_bw[~np.isnan(recent_bw)]
                    if len(valid_bw) >= lb // 2:
                        bw_now = bb_std[i] if not np.isnan(bb_std[i]) else 0.0
                        bw_max = np.max(valid_bw) if len(valid_bw) > 0 else 1.0
                        squeeze_ratio = bw_now / bw_max if bw_max > 0 else 1.0

                        # Son birkaç barda rejim COMPRESSION veya RANGE idi mi?
                        was_compressed = False
                        for j in range(max(0, i - 5), i):
                            if str(rgv[j]) in ("COMPRESSION", "RANGE"):
                                was_compressed = True
                                break

                        # Şu an EXPANSION veya breakout sinyali var mı?
                        now_expanding = crg in ("EXPANSION", "STRONG_TREND")
                        has_breakout = (d == 1 and bool(bbo[i])) or (d == -1 and bool(dbo[i]))

                        if (was_compressed or squeeze_ratio < SETTINGS.IMPULSE_COMPRESSION_BB_SQUEEZE) and \
                           (now_expanding or has_breakout) and vol_ratio >= 2.0:
                            is_compression_breakout = True

                if is_compression_breakout:
                    triggered = True
                    setup_type = "Impulse Breakout"
                    # Baz skor: hacim oranı + fiyat hareketi gücü
                    vol_component = min(vol_ratio / 5.0, 1.0)  # 5x'te tam puan
                    move_component = min(bar_move_pct / 3.0, 1.0)  # %3'te tam puan
                    base_score = (vol_component * 0.5 + move_component * 0.5) * 50.0
                    base_score += SETTINGS.IMPULSE_BREAKOUT_BONUS
                    if crg == "EXPANSION":
                        base_score += SETTINGS.IMPULSE_REGIME_EXPANSION_BONUS

            # --- 2. VOLUME SPIKE IMPULSE ---
            if not triggered:
                if vol_ratio >= SETTINGS.IMPULSE_VOL_SPIKE_MULT and \
                   bar_move_pct >= SETTINGS.IMPULSE_MIN_BAR_MOVE_PCT:
                    triggered = True
                    setup_type = "Volume Spike"
                    vol_component = min(vol_ratio / 8.0, 1.0)  # 8x'te tam puan
                    move_component = min(bar_move_pct / 5.0, 1.0)  # %5'te tam puan
                    base_score = (vol_component * 0.6 + move_component * 0.4) * 55.0
                    base_score += SETTINGS.IMPULSE_VOL_SPIKE_BONUS

            # --- 3. BoS RETEST ---
            if not triggered:
                has_bos = (d == 1 and bool(bull_bos[i])) or (d == -1 and bool(bear_bos[i]))
                has_ob_fvg = ((d == 1 and (bool(obb[i]) or bool(fgb[i]))) or
                              (d == -1 and (bool(obd[i]) or bool(fgd[i]))))
                ema_aligned = int(epv[i]) == d

                if has_bos and (has_ob_fvg or ema_aligned) and vol_ratio >= 1.5:
                    triggered = True
                    # Bu koşul kırılım mumunu yakalar; retest doğrulaması yapmaz.
                    setup_type = "BoS Breakout"
                    vol_component = min(vol_ratio / 4.0, 1.0)
                    base_score = vol_component * 40.0
                    base_score += SETTINGS.IMPULSE_BOS_BONUS
                    if has_ob_fvg:
                        base_score += 5.0  # OB/FVG yakınlık bonusu

            if not triggered:
                continue

            # ── CONFLUENCE SKORLAMASI (tüm alt stratejiler için ortak) ──

            # Supertrend onayı
            if SETTINGS.USE_SUPERTREND_SCORE_BONUS:
                if cst == d:
                    base_score += SETTINGS.SUPERTREND_SCORE_BONUS
                elif cst == -d:
                    base_score -= SETTINGS.SUPERTREND_CONTRA_PENALTY

            # PMax onayı
            if SETTINGS.USE_PMAX_SCORE_BONUS and SETTINGS.USE_PMAX:
                if pmxd[i] == d:
                    base_score += SETTINGS.PMAX_SCORE_BONUS
                elif pmxd[i] == -d:
                    base_score -= SETTINGS.PMAX_CONTRA_PENALTY

            # EMA cross
            if SETTINGS.USE_EMA_CROSS:
                if ecv[i] == d:
                    base_score += SETTINGS.IMPULSE_EMA_CROSS_BONUS
                if epv[i] == d:
                    base_score += SETTINGS.EMA_POSITION_BONUS
                elif epv[i] == -d:
                    base_score -= SETTINGS.EMA_POSITION_CONTRA_PENALTY

            # Engulfing
            if SETTINGS.USE_ENGULFING:
                eg = env[i]
                if d == 1 and eg > 0:
                    base_score += SETTINGS.IMPULSE_ENGULFING_BONUS if eg == 2 else SETTINGS.ENGULFING_BASIC_BONUS
                elif d == -1 and eg < 0:
                    base_score += SETTINGS.IMPULSE_ENGULFING_BONUS if eg == -2 else SETTINGS.ENGULFING_BASIC_BONUS

            # BoS onayı yalnızca BOS tetikleyicisi olmayan setup'lara eklenir.
            if setup_type != "BoS Breakout" and getattr(SETTINGS, "USE_BOS", False):
                if (d == 1 and bool(bull_bos[i])) or (d == -1 and bool(bear_bos[i])):
                    base_score += SETTINGS.IMPULSE_BOS_BONUS

            # ADX / Trend gücü
            if SETTINGS.USE_ADX_SCORE_BONUS and adx_v[i] >= SETTINGS.ADX_THRESHOLD:
                if (d == 1 and di_p[i] > di_m[i]) or (d == -1 and di_m[i] > di_p[i]):
                    base_score += SETTINGS.ADX_SCORE_BONUS
                else:
                    base_score -= SETTINGS.DI_CONTRA_PENALTY

            # Breakout Finder onayı
            if SETTINGS.USE_BREAKOUT_FILTER:
                if (d == 1 and bool(bbo[i])) or (d == -1 and bool(dbo[i])):
                    if setup_type != "Impulse Breakout":
                        base_score += SETTINGS.IMPULSE_BREAKOUT_BONUS

            # LuxLiquidity
            if getattr(SETTINGS, "USE_LUX_LIQUIDITY", False):
                if (d == 1 and bool(lux_bull[i])) or (d == -1 and bool(lux_bear[i])):
                    base_score += getattr(SETTINGS, "LUX_LIQ_SCORE_BONUS", 10.0)

            # Stacked OB+FVG
            if getattr(SETTINGS, "USE_STACKED_OB_FVG", False):
                if (d == 1 and bool(stacked_bull[i])) or (d == -1 and bool(stacked_bear[i])):
                    base_score += getattr(SETTINGS, "STACKED_OB_FVG_SCORE_BONUS", 9.0)

            # TSI momentum
            if SETTINGS.USE_TSI and tv[i] != 0.0:
                if (d == 1 and tv[i] > 0) or (d == -1 and tv[i] < 0):
                    base_score += SETTINGS.TSI_SCORE_BONUS

            # LinReg Oscillator
            if SETTINGS.USE_LINREG_OSC:
                if d == 1 and lr_v[i] >= SETTINGS.LINREG_UPPER:
                    base_score -= SETTINGS.LINREG_PENALTY  # Aşırı alım
                elif d == -1 and lr_v[i] <= SETTINGS.LINREG_LOWER:
                    base_score -= SETTINGS.LINREG_PENALTY  # Aşırı satım

            # ── CEZA MEKANİZMALARI ──

            # RANGE rejim cezası
            if SETTINGS.USE_MARKET_REGIME and crg == "RANGE" and setup_type != "Impulse Breakout":
                base_score -= SETTINGS.IMPULSE_RANGE_PENALTY

            # Çifte çelişki: ST + PMax ikisi birden ters
            if SETTINGS.USE_SUPERTREND_SCORE_BONUS and SETTINGS.USE_PMAX_SCORE_BONUS:
                if cst == -d and pmxd[i] == -d:
                    base_score -= SETTINGS.IMPULSE_CONTRA_TREND_PENALTY

            # HTF Counter Trend cezası
            htf_ct = False
            if SETTINGS.USE_HTF_FILTER and self.htf_bias != 0 and self.htf_bias == -d:
                htf_ct = True
                base_score -= SETTINGS.HTF_COUNTER_TREND_PENALTY

            # Fitil kontrolü (fakeout önleme)
            if bar_range > 0:
                if d == 1:
                    wick_ratio = (high[i] - close[i]) / bar_range
                else:
                    wick_ratio = (close[i] - low[i]) / bar_range
                if wick_ratio > SETTINGS.IMPULSE_WICK_PENALTY_THRESHOLD:
                    base_score -= SETTINGS.IMPULSE_WICK_PENALTY

            # Skor sınırlama
            final_score = min(max(base_score, 0.0), 100.0)

            # Minimum eşik kontrolü
            if final_score < SETTINGS.MIN_IMPULSE_SCORE:
                continue

            # ── Grade hesapla ──
            _grade = "D"
            _prob = 0.25
            for _g_min, _g_lbl, _g_prob in SETTINGS.GRADE_TABLE:
                if final_score >= _g_min:
                    _grade = _g_lbl
                    _prob = _g_prob
                    break

            # ── Confluence puanı ──
            _conf = 0.0
            if getattr(SETTINGS, "USE_LUX_LIQUIDITY", False) and \
               ((d == 1 and bool(lux_bull[i])) or (d == -1 and bool(lux_bear[i]))):
                _conf += getattr(SETTINGS, "LUX_LIQ_SCORE_BONUS", 10.0)
            if getattr(SETTINGS, "USE_BOS", False) and \
               ((d == 1 and bool(bull_bos[i])) or (d == -1 and bool(bear_bos[i]))):
                _conf += getattr(SETTINGS, "BOS_SCORE_BONUS", 9.0)
            if SETTINGS.USE_EMA_CROSS and ecv[i] == d:
                _conf += SETTINGS.EMA_CROSS_SCORE_BONUS
            if SETTINGS.USE_SUPERTREND_SCORE_BONUS and cst == d:
                _conf += SETTINGS.SUPERTREND_SCORE_BONUS
            if SETTINGS.USE_PMAX_SCORE_BONUS and SETTINGS.USE_PMAX and pmxd[i] == d:
                _conf += SETTINGS.PMAX_SCORE_BONUS

            # Anchor extreme: ATR bazlı SL seviyesi (sweep olmadığı için)
            if d == 1:
                anchor = low[i]  # SL bar'ın dibi
            else:
                anchor = high[i]  # SL bar'ın tepesi

            yield SweepSetup(
                direction=d,
                level=close[i],
                score=final_score,
                anchor_extreme=anchor,
                bar_index=i,
                rsi_div=self.rsi_div,
                funding_rate=float(self.funding_info.get("funding_rate", 0.0)) if self.funding_info else 0.0,
                squeeze_risk=str(self.funding_info.get("squeeze_risk", "")) if self.funding_info else "",
                adx_value=adx_v[i],
                di_plus=di_p[i],
                di_minus=di_m[i],
                supertrend_trend=cst,
                poc_price=float(poc_v[i]) if not np.isnan(poc_v[i]) else 0.0,
                vah_price=float(vah_v[i]) if not np.isnan(vah_v[i]) else 0.0,
                val_price=float(val_v[i]) if not np.isnan(val_v[i]) else 0.0,
                linreg_osc=float(lr_v[i]),
                swing_vwap=float(wv[i]) if not np.isnan(wv[i]) else 0.0,
                swing_vwap_dir=int(wdv[i]),
                breakout_signal=1 if bool(bbo[i]) else (-1 if bool(dbo[i]) else 0),
                tsi_value=float(tv[i]),
                tsi_signal=float(tsv[i]),
                multi_div_bull=float(mdb[i]),
                multi_div_bear=float(mdd[i]),
                whale_bull_absorption=False,
                whale_bear_absorption=False,
                whale_delta_dominance=0,
                pmax_trend=int(pmxd[i]) if SETTINGS.USE_PMAX else 0,
                pmax_cross=0,
                market_regime=crg,
                fvg_bull_near=bool(fgb[i]),
                fvg_bear_near=bool(fgd[i]),
                ob_bull_near=bool(obb[i]),
                ob_bear_near=bool(obd[i]),
                ema_cross=int(ecv[i]),
                ema_position=int(epv[i]),
                engulfing=int(env[i]),
                lux_liq_sweep=(bool(lux_bull[i]) if d == 1 else bool(lux_bear[i])),
                bos_signal=1 if bool(bull_bos[i]) else (-1 if bool(bear_bos[i]) else 0),
                stacked_ob_fvg=1 if bool(stacked_bull[i]) else (-1 if bool(stacked_bear[i]) else 0),
                confluence_score=_conf,
                grade=_grade,
                probability=_prob,
                setup_type=setup_type,
                htf_counter_trend=htf_ct,
                engine="impulse",
            )
