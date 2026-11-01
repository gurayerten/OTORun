import html
import re
import requests

from config import SETTINGS
from logger import get_logger

log = get_logger("telegram")


def esc(s):
    """Telegram HTML parse_mode için dinamik değerleri kaçırır (<, >, &)."""
    return html.escape(str(s), quote=False)


def fmt_p(val):
    """Fiyatı coin büyüklüğüne göre dinamik hassasiyetle biçimlendirir.
    
    Kritik: Küçük fiyatlı altcoinlerde (örn. KATUSDT 0.004159) 4 basamak yuvarlama
    Giriş ve SL değerlerini özdeş (0.0042) göstererek sinyali bozar. Bu fonksiyon
    büyüklüğe göre uygun basamağı seçer.
    
    Örn:
      val >= 100    → 2 basamak  (BTC: 68,450.50)
      val >= 1      → 4 basamak  (ONDO: 1.0025)
      val >= 0.001  → 6 basamak  (KAT: 0.004159, DOGE: 0.144800)
      val < 0.001   → 8 basamak  (PEPE: 0.00000952)
    """
    if val is None:
        return "N/A"
    try:
        f = float(val)
    except (ValueError, TypeError):
        return str(val)
    if f >= 100:
        return f"{f:,.2f}"
    if f >= 1:
        return f"{f:.4f}"
    if f >= 0.001:
        return f"{f:.6f}"
    return f"{f:.8f}"


def fmt_pct(value, decimals: int = 2) -> str:
    """Yüzde değerleri için net, tutarlı formatlama."""
    if value is None:
        return "N/A"
    try:
        f = float(value)
    except (ValueError, TypeError):
        return str(value)
    return f"{f:.{decimals}f}"


def fmt_usd_compact(value) -> str:
    """Büyük USD tutarları için kısaltılmış format (K/M/B).
    
    Toplam piyasa hacmi gibi büyük notional değerleri okunabilir hale getirir.
    Örn: 58_234_123_456 → $58.23B, 1_234_567 → $1.23M
    """
    if value is None:
        return "N/A"
    try:
        f = float(value)
    except (ValueError, TypeError):
        return str(value)
    a = abs(f)
    if a >= 1e9:
        return f"${f / 1e9:.2f}B"
    if a >= 1e6:
        return f"${f / 1e6:.2f}M"
    if a >= 1e3:
        return f"${f / 1e3:.1f}K"
    return f"${f:.0f}"


def send_telegram(text):
    if not SETTINGS.TELEGRAM_BOT_TOKEN or not SETTINGS.TELEGRAM_CHAT_ID:
        log.debug(f"[telegram] Token/ChatID ayarlı değil, mesaj sadece terminalde:\n{text}")
        return
    url = f"https://api.telegram.org/bot{SETTINGS.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": SETTINGS.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if r.status_code == 400 and "can't parse entities" in r.text:
            # Dynamic report text can contain comparison operators (for example,
            # "< -0.05%") which Telegram mistakes for an HTML tag. Retry as plain
            # text while removing only the markup tags used by our formatters.
            plain_text = re.sub(r"</?(?:b|i|u|s|strong|em|code|pre|a)(?:\s+[^>]*)?>", "", text)
            r = requests.post(
                url,
                data={
                    "chat_id": SETTINGS.TELEGRAM_CHAT_ID,
                    "text": plain_text,
                },
                timeout=10,
            )
        if r.status_code != 200:
            log.warning(f"[telegram] gönderilemedi: {r.text}")
    except Exception as e:
        log.error(f"[telegram] hata: {e}")


def fmt_entry(symbol, plan, score, setup=None):
    side = "🟢 LONG" if plan.direction == 1 else "🔴 SHORT"
    ind_str = ""
    vp_str = ""
    linreg_str = ""
    vwap_str = ""
    bo_str = ""
    tsi_str = ""
    div_str = ""
    whale_str = ""
    pmax_str = ""
    regime_str = ""
    confluence_str = ""
    ob_str = ""
    contra_str = ""

    # --- #14 Setup Type + #13 Grade ---
    setup_type_str = "Likidite Süpürmesi"
    grade_str = ""
    htf_ct_str = ""

    if setup:
        # #14 Setup Type
        if hasattr(setup, "setup_type") and setup.setup_type:
            setup_type_str = setup.setup_type

        # #13 Grade & Probability
        if hasattr(setup, "grade") and setup.grade:
            grade_str = f" ({setup.grade} • ~%{setup.probability*100:.0f})"

        # #9 HTF Counter Trend uyarısı
        if hasattr(setup, "htf_counter_trend") and setup.htf_counter_trend:
            htf_ct_str = "⚠️ Counter Trend Sinyali\n"

        st_lbl = "📈 LONG" if setup.supertrend_trend == 1 else "🔴 SHORT" if setup.supertrend_trend == -1 else "➡️ Nötr"
        ind_str = (
            f"ADX: {setup.adx_value:.1f} (DI+: {setup.di_plus:.1f} / DI-: {setup.di_minus:.1f})\n"
            f"Supertrend: {st_lbl}\n"
        )

        # Volume Profile seviyeleri
        if setup.poc_price and setup.poc_price > 0:
            vp_str = (
                f"📊 Volume Profile:\n"
                f"  PoC: {fmt_p(setup.poc_price)}  VAH: {fmt_p(setup.vah_price)}  VAL: {fmt_p(setup.val_price)}\n"
            )

        # Linear Regression Oscillator
        if setup.linreg_osc != 0.0:
            lr_val = setup.linreg_osc
            if lr_val >= 1.5:
                lr_label = "🔺 Aşırı Alım"
            elif lr_val <= -1.5:
                lr_label = "🔻 Aşırı Satım"
            elif lr_val > 0:
                lr_label = "↗️ Yukarı Trend"
            else:
                lr_label = "↘️ Aşağı Trend"
            linreg_str = f"📉 LinReg Osc: {lr_val:.2f} ({lr_label})\n"

        # Swing VWAP
        if setup.swing_vwap and setup.swing_vwap > 0:
            vwap_dir_lbl = "📈 Yukarı" if setup.swing_vwap_dir == 1 else "📉 Aşağı"
            vwap_str = f"〽️ Swing VWAP: {fmt_p(setup.swing_vwap)} ({vwap_dir_lbl})\n"

        # Breakout Finder
        if setup.breakout_signal == 1:
            bo_str = "🚀 Breakout: Yukarı Kırılım ✅\n"
        elif setup.breakout_signal == -1:
            bo_str = "💥 Breakdown: Aşağı Kırılım ✅\n"

        # True Strength Index (TSI)
        if hasattr(setup, "tsi_value") and setup.tsi_value != 0.0:
            tsi_val = setup.tsi_value
            tsi_sig_val = setup.tsi_signal
            if tsi_val > 25:
                tsi_lbl = "🔋 Güçlü Yukarı"
            elif tsi_val > 0:
                tsi_lbl = "↗️ Yukarı Momentum"
            elif tsi_val < -25:
                tsi_lbl = "🔋 Güçlü Aşağı"
            else:
                tsi_lbl = "↘️ Aşağı Momentum"
            tsi_str = f"📊 TSI: {tsi_val:.2f} / Sinyal: {tsi_sig_val:.2f} ({tsi_lbl})\n"

        # Multi-Indicator Divergence (#7 ağırlıklı gösterim)
        if hasattr(setup, "multi_div_bull") and (setup.multi_div_bull > 0 or setup.multi_div_bear > 0):
            total_weight = sum(SETTINGS.DIVERGENCE_WEIGHTS.values()) if hasattr(SETTINGS, 'DIVERGENCE_WEIGHTS') else 25
            div_str = ""
            if setup.direction == 1:  # LONG sinyal
                if setup.multi_div_bull > 0:
                    div_str += f"🔍 Diverjans: {setup.multi_div_bull:.0f}/{total_weight} ağırlıklı puan Bullish ✅\n"
                if setup.multi_div_bear > 0:
                    div_str += f"⚠️ Karşı Diverjans: {setup.multi_div_bear:.0f}/{total_weight} Bearish\n"
            else:  # SHORT sinyal
                if setup.multi_div_bear > 0:
                    div_str += f"🔍 Diverjans: {setup.multi_div_bear:.0f}/{total_weight} ağırlıklı puan Bearish ✅\n"
                if setup.multi_div_bull > 0:
                    div_str += f"⚠️ Karşı Diverjans: {setup.multi_div_bull:.0f}/{total_weight} Bullish\n"

        # Whale Liquidity & Absorption Profile
        whale_str = ""
        if hasattr(setup, "whale_bull_absorption") and SETTINGS.USE_WHALE_PROFILE:
            abs_parts = []
            if setup.whale_bull_absorption:
                abs_parts.append("🐳 Destek Soğurması (Bull Peak) 🟢")
            if setup.whale_bear_absorption:
                abs_parts.append("🐳 Direnç Soğurması (Bear Peak) 🔴")
            
            delta_part = ""
            if setup.whale_delta_dominance == 1:
                delta_part = "⚡ Baskın Alıcı Deltası 🔥"
            elif setup.whale_delta_dominance == -1:
                delta_part = "⚡ Baskın Satıcı Deltası 💥"
            
            if abs_parts or delta_part:
                whale_str = "🐳 Whale Profile:\n"
                if abs_parts:
                    whale_str += "  " + " / ".join(abs_parts) + "\n"
                if delta_part:
                    whale_str += "  " + delta_part + "\n"

        # PMax (Profit Maximizer)
        pmax_str = ""
        if hasattr(setup, "pmax_trend") and SETTINGS.USE_PMAX:
            if setup.pmax_trend == 1:
                pmax_lbl = "📈 Bullish (MAvg > PMax)"
            elif setup.pmax_trend == -1:
                pmax_lbl = "📉 Bearish (MAvg < PMax)"
            else:
                pmax_lbl = "➡️ Nötr"
            pmax_str = f"📊 PMax ({SETTINGS.PMAX_MA_TYPE}-{SETTINGS.PMAX_MA_LENGTH}): {esc(pmax_lbl)}\n"
            if setup.pmax_cross == 1:
                pmax_str += "  ✅ Yakın Bullish Çapraz (MAvg × PMax)\n"
            elif setup.pmax_cross == -1:
                pmax_str += "  ✅ Yakın Bearish Çapraz (MAvg × PMax)\n"

        # Piyasa Rejimi (#12 genişletilmiş)
        regime_str = ""
        if hasattr(setup, "market_regime") and SETTINGS.USE_MARKET_REGIME:
            regime_icons = {
                "RANGE": "📦 RANGE (Yatay Piyasa) ⚠️",
                "TREND": "📈 TREND (Güçlü Trend)",
                "STRONG_TREND": "🔥 GÜÇLÜ TREND",
                "WEAK_TREND": "📈 ZAYIF TREND",
                "COMPRESSION": "🗜️ SIKIŞMA (Düşük Vol)",
                "EXPANSION": "💥 GENİŞLEME (Yükselen Vol)",
                "MEAN_REVERSION": "🔄 ORTALAMAYA DÖNÜŞ",
                "TRANSITION": "🔄 GEÇİŞ",
            }
            regime_str = f"🏷️ Rejim: {esc(regime_icons.get(setup.market_regime, setup.market_regime))}\n"

        # Confluence özeti (#8 toplam puan ile)
        confluence_parts = []
        if hasattr(setup, "fvg_bull_near") and SETTINGS.USE_FVG:
            if (setup.direction == 1 and setup.fvg_bull_near) or (setup.direction == -1 and setup.fvg_bear_near):
                confluence_parts.append("📊 FVG")
        if hasattr(setup, "ob_bull_near") and SETTINGS.USE_ORDER_BLOCK:
            if (setup.direction == 1 and setup.ob_bull_near) or (setup.direction == -1 and setup.ob_bear_near):
                confluence_parts.append("🏗️ OB")
        if hasattr(setup, "ema_cross") and SETTINGS.USE_EMA_CROSS:
            if setup.ema_cross == setup.direction:
                confluence_parts.append("📈 EMA×")
            if setup.ema_position == setup.direction:
                confluence_parts.append("📍 EMA" + ("↑" if setup.direction == 1 else "↓"))
        if hasattr(setup, "engulfing") and SETTINGS.USE_ENGULFING:
            eng = setup.engulfing
            if (setup.direction == 1 and eng > 0) or (setup.direction == -1 and eng < 0):
                confluence_parts.append("🕯️ Engulf" + ("⚡" if abs(eng) == 2 else ""))
        if hasattr(setup, "lux_liq_sweep") and setup.lux_liq_sweep and getattr(SETTINGS, "USE_LUX_LIQUIDITY", False):
            confluence_parts.append("💧 LuxLiq")
        if hasattr(setup, "bos_signal") and getattr(SETTINGS, "USE_BOS", False):
            if setup.bos_signal == setup.direction:
                confluence_parts.append("🧱 BoS")
        if hasattr(setup, "stacked_ob_fvg") and getattr(SETTINGS, "USE_STACKED_OB_FVG", False):
            if setup.stacked_ob_fvg == setup.direction:
                confluence_parts.append("💎 Stacked")

        confluence_str = ""
        if confluence_parts:
            conf_total = f" ({setup.confluence_score:.0f}p)" if hasattr(setup, "confluence_score") and setup.confluence_score > 0 else ""
            confluence_str = "🔗 Confluence: " + " | ".join(confluence_parts) + conf_total + "\n"
    
        ob_str = ""
        if hasattr(setup, "ob_imbalance_ratio") and setup.ob_imbalance_ratio > 0:
            ob_icon = "✅" if setup.ob_favorable else "❌"
            # Direction-aware label: LONG için Bid baskısı iyi, SHORT için Ask baskısı iyi
            dom_lbl = "Bid\u2191" if setup.direction == 1 else "Ask\u2191"
            ob_str = f"\u2696\ufe0f L2 Imbalance: {setup.ob_imbalance_ratio:.2f}x ({dom_lbl}) {ob_icon}\n"

        # Gösterge çelişkisi uyarısı: ST veya PMax sinyal yönüyle ters ise işaretle
        contra_warnings = []
        if setup.supertrend_trend != 0 and setup.supertrend_trend != setup.direction:
            contra_warnings.append("Supertrend")
        if SETTINGS.USE_PMAX and hasattr(setup, "pmax_trend") and setup.pmax_trend != 0 and setup.pmax_trend != setup.direction:
            contra_warnings.append("PMax")
        contra_str = ""
        if contra_warnings:
            contra_str = f"\u26a0\ufe0f Gösterge Çelişkisi: {' + '.join(contra_warnings)} sinyal yönüyle ters!\n"

    return (
        f"<b>{side} ({esc(setup_type_str)}) | {esc(symbol)}</b>\n"
        f"Skor: <b>{score:.0f}/100</b>{grade_str}\n"
        f"{htf_ct_str}"
        f"{contra_str if setup else ''}"
        f"{ind_str}"
        f"{vp_str}"
        f"{linreg_str}"
        f"{vwap_str}"
        f"{bo_str}"
        f"{tsi_str}"
        f"{div_str}"
        f"{whale_str}"
        f"{pmax_str}"
        f"{regime_str}"
        f"{confluence_str}"
        f"{ob_str}"
        f"Giriş: <b>{fmt_p(plan.entry)}</b>\n"
        f"SL: <b>{fmt_p(plan.sl)}</b>\n"
        f"TP1: <b>{fmt_p(plan.tp1)}</b>  TP2: <b>{fmt_p(plan.tp2)}</b>  TP3: <b>{fmt_p(plan.tp3)}</b>"
    )



def fmt_event(ev):
    sym = esc(ev["symbol"])
    icons = {
        "tp1": "🎯 TP1", "tp2": "🎯🎯 TP2", "tp3": "🏆 TP3",
        "breakeven": "🛡️ Break-Even", "close": "📉 KAPANDI",
        "trailing_activated": "🔄 İzleyen Stop Aktif",
        "sl_escalation": "🔒 SL Yükseltildi",
    }
    
    if ev["type"] == "trailing_activated":
        return f"<b>{icons['trailing_activated']}</b> | {sym} | Fiyat: {fmt_p(ev['price'])}"

    if ev["type"] == "sl_escalation":
        return f"<b>{icons['sl_escalation']}</b> | {sym} | SL → <b>{fmt_p(ev['price'])}</b> ({esc(ev.get('level', ''))} seviyesine)"
        
    if ev["type"] == "close":
        outcome = "WIN ✅" if ev["win"] else "LOSS ❌"
        return f"<b>{icons['close']}</b> | {sym} | {outcome} ({esc(ev['reason'])}) | R: {ev['r_multiple']:+.1f}"
        
    return f"<b>{icons.get(ev['type'], '❓')}</b> | {sym} | Fiyat: {fmt_p(ev['price'])}"


def fmt_events(events):
    """Birden fazla olayı tek bir Telegram mesajında toplar (mesaj kirliliğini önler)."""
    if not events:
        return ""
    if isinstance(events, dict):
        return fmt_event(events)
    return "\n".join(fmt_event(ev) for ev in events)

