"""
Funding Rate Squeeze Radarı — Tüm piyasadaki aşırı funding rate'li coin'leri tespit eder.

Binance /fapi/v1/premiumIndex endpoint'i ile TEK çağrıda tüm coin'lerin funding
rate'ini çeker, en aşırı olanları sıralar ve Telegram'a özet gönderir.

Zenginleştirme katmanları (FUNDING_RADAR_ENRICH aktifse):
  - OI Değişimi:        oi_history.json + anlık OI → değişim yüzdesi ve tasfiye tahmini
  - OI çözülmesi:       yalnızca açıkça tahmin olarak etiketlenen OI azalması proxy'si
  - Vadeli 24s & Kısa:  get_24hr_tickers + kısa vadeli bar momentumu (yeni listeleme ayrımı)
  - Spot / Basis:       Spot 24hr tickers → futures-spot basis farkı & futures-only tespiti
  - CVD & Emilim:       Kline delta yaklaşımı + CVD/Fiyat ayrışması (Absorption) tespiti
  - Teknik Seviyeler:   Supertrend + dinamik fallback'li destek/direnç
  - Zaman Serisi:       radar_history.json → Fiyat, Funding, OI ve CVD çoklu tarama trendi
  - Taksonomi:          4 Resmi Faz (Sıkışma Birikimi, Devam Ediyor, Geç Aşama, Baskı Azalıyor)
  - Piyasa Rejimi:      Yeni listeleme ve çöküş sonrası konsolidasyon ayrımı
  - Funding Cap:        Borsa tavan/taban sınırı (-2.0000% cap) & sabit kalma uyarısı

Saatte 1 kez çalışır (FUNDING_RADAR_INTERVAL_SEC ile ayarlanır).
Mevcut analyze_funding_squeeze (sembol bazlı skorlama) ile ÇAKIŞMAZ.
"""
import html
import json
import os
from datetime import datetime, timezone

import numpy as np

from config import SETTINGS
from exchange import (
    get_all_premium_index,
    get_all_spot_tickers,
    get_24hr_tickers,
    get_klines,
    get_open_interest,
)
from logger import get_logger
from strategy import compute_supertrend

log = get_logger("funding_radar")

RADAR_HISTORY_FILE = "radar_history.json"


# ──────────────────────────────────────────────────────────────────────
#  OI & Radar History — Zaman serisi takibi
# ──────────────────────────────────────────────────────────────────────

def _load_oi_history():
    path = SETTINGS.SCREENER_OI_HISTORY_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_radar_history():
    path = os.path.join(os.path.dirname(__file__), RADAR_HISTORY_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_radar_history(history):
    path = os.path.join(os.path.dirname(__file__), RADAR_HISTORY_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        log.warning(f"Radar history kaydedilemedi: {e}")


# ──────────────────────────────────────────────────────────────────────
#  CVD — Kline-based delta (ek API çağrısı YOK)
# ──────────────────────────────────────────────────────────────────────

def _calc_cvd_from_df(df):
    """Son N bardan kümülatif volume delta hesapla (USD cinsinden).
    Formül: delta = volume * (close - open) / (high - low) * avg_price
    Coin adedi cinsinden delta'yı ortalama fiyatla çarparak USD'ye çevirir.
    Pozitif → alıcı baskın, negatif → satıcı baskın."""
    if df is None or len(df) < 2:
        return 0.0
    h = df["high"].values
    l = df["low"].values
    o = df["open"].values
    c = df["close"].values
    v = df["volume"].values
    rng = h - l
    rng = np.where(rng <= 0, 1e-8, rng)
    deltas = v * (c - o) / rng
    avg_price = float(c.mean()) if len(c) > 0 else 1.0
    return float(deltas.sum() * avg_price)


# ──────────────────────────────────────────────────────────────────────
#  Destek / Direnç — Asla Boş Kalmayan Pivot + Fallback Mekanizması
# ──────────────────────────────────────────────────────────────────────

def _compute_support_resistance(df, mark_price=None, low_24h=None, high_24h=None, pivot_window=5, max_distance_pct=0.30):
    """Destek ve direnç seviyelerini bulur.
    Önce pivot high/low seviyelerine bakar (fiyattan max %30 uzakta).
    Eğer parabolik yükseliş/çöküş nedeniyle pivot bulunamazsa,
    vadeli 24s dip/tepe veya son kline ekstremumlarını dinamik destek/direnç olarak kullanır.
    """
    if df is None or len(df) < 3:
        return None, None, "", ""

    highs = df["high"].values
    lows = df["low"].values
    close = df["close"].values
    current_price = float(mark_price or close[-1])
    n = len(df)

    supports = []
    resistances = []

    if n >= pivot_window * 2 + 1:
        for i in range(pivot_window, n - pivot_window):
            if all(highs[i] >= highs[i - j] for j in range(1, pivot_window + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, pivot_window + 1)):
                resistances.append(float(highs[i]))

            if all(lows[i] <= lows[i - j] for j in range(1, pivot_window + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, pivot_window + 1)):
                supports.append(float(lows[i]))

    # Destek bul
    support = None
    support_src = ""
    below = [
        s for s in supports
        if s < current_price and (current_price - s) / current_price <= max_distance_pct
    ]
    if below:
        support = max(below)
        support_src = "(Pivot)"
    else:
        if low_24h and 0 < low_24h < current_price:
            support = float(low_24h)
            support_src = "(24s Dip)"
        else:
            recent_low = float(np.min(lows[-min(20, n):]))
            if recent_low < current_price:
                support = recent_low
                support_src = "(Son Dip)"

    # Direnç bul
    resistance = None
    resistance_src = ""
    above = [
        r for r in resistances
        if r > current_price and (r - current_price) / current_price <= max_distance_pct
    ]
    if above:
        resistance = min(above)
        resistance_src = "(Pivot)"
    else:
        if high_24h and high_24h > current_price:
            resistance = float(high_24h)
            resistance_src = "(24s Zirve)"
        else:
            recent_high = float(np.max(highs[-min(20, n):]))
            if recent_high > current_price:
                resistance = recent_high
                resistance_src = "(Son Zirve)"

    return support, resistance, support_src, resistance_src


# ──────────────────────────────────────────────────────────────────────
#  Zaman Serisi, Faz Tespiti & Emilim (Absorption) Analizi
# ──────────────────────────────────────────────────────────────────────

def _update_radar_tracking(coin, squeeze_type, history):
    """Zaman serisi geçmişini günceller ve 4 resmi faza uygun deterministik faz tespiti yapar.
    Fiyat, Funding, OI ve CVD trendlerini birlikte analiz eder."""
    symbol = coin["symbol"]
    current_price = coin["mark_price"]
    current_rate = coin["rate"]
    current_oi = coin.get("current_oi") or 0.0
    current_cvd = coin.get("cvd") or 0.0
    chg_24h = coin.get("price_change_pct_24h") or 0.0
    is_recent_listing = coin.get("is_recent_listing", False)
    recent_trend_pct = coin.get("recent_trend_pct", 0.0)
    recent_bars_count = coin.get("recent_bars_count", 4)

    now_utc = datetime.now(timezone.utc)
    now_ts = now_utc.timestamp()
    now_str = now_utc.strftime("%H:%M UTC")

    prev = history.get(symbol)
    if prev and (now_ts - prev.get("last_updated_ts", 0)) < 43200:
        seen_count = prev.get("seen_count", 1) + 1
        first_price = prev.get("first_price", current_price)
        first_rate = prev.get("first_rate", current_rate)
        first_seen_str = prev.get("first_seen_str", now_str)
        prev_price = prev.get("last_price", current_price)
        prev_cvd = prev.get("last_cvd", current_cvd)

        price_hist = prev.get("price_history", [])
        price_hist.append(current_price)
        price_hist = price_hist[-5:]

        funding_hist = prev.get("funding_history", [])
        funding_hist.append(current_rate)
        funding_hist = funding_hist[-5:]

        oi_hist = prev.get("oi_history", [])
        if current_oi > 0:
            oi_hist.append(current_oi)
            oi_hist = oi_hist[-5:]

        cvd_hist = prev.get("cvd_history", [])
        cvd_hist.append(current_cvd)
        cvd_hist = cvd_hist[-5:]

        price_change_radar = (current_price - first_price) / first_price * 100 if first_price > 0 else 0.0
        price_change_prev_scan = (current_price - prev_price) / prev_price * 100 if prev_price > 0 else 0.0
    else:
        seen_count = 1
        first_price = current_price
        first_rate = current_rate
        first_seen_str = now_str
        prev_price = current_price
        prev_cvd = current_cvd
        price_hist = [current_price]
        funding_hist = [current_rate]
        oi_hist = [current_oi] if current_oi > 0 else []
        cvd_hist = [current_cvd]
        price_change_radar = 0.0
        price_change_prev_scan = 0.0

    # ── 1. Funding Trendi (Zayıflama / Gevşeme vs Şiddetlenme) ──
    # Hem başlangıç değerine (first_rate) hem de geçmiş diziye (funding_hist) göre hesapla
    funding_easing = False
    funding_expanding = False
    funding_change_from_start_pct = 0.0
    if abs(first_rate) > 0:
        funding_change_from_start_pct = (abs(current_rate) - abs(first_rate)) / abs(first_rate) * 100

    recent_rate_chg = 0.0
    if len(funding_hist) >= 2 and abs(funding_hist[0]) > 0:
        recent_rate_chg = (abs(current_rate) - abs(funding_hist[0])) / abs(funding_hist[0]) * 100

    # Eğer negatiflik/pozitiflik büyüklüğü %15'ten fazla azaldıysa gevşiyor
    if funding_change_from_start_pct <= -15.0 or recent_rate_chg <= -15.0:
        funding_easing = True
        funding_dir_label = f"Zayıflıyor (%{abs(funding_change_from_start_pct):.0f} gevşedi)"
    elif funding_change_from_start_pct >= 15.0 or recent_rate_chg >= 15.0:
        funding_expanding = True
        funding_dir_label = f"Şiddetleniyor (+%{funding_change_from_start_pct:.0f} arttı)"
    else:
        funding_dir_label = "Sabit"

    # Funding borsa cap (tavan/taban) kontrolü
    is_funding_capped = abs(current_rate) >= 0.0199
    funding_stuck = len(funding_hist) >= 2 and all(abs(r - current_rate) < 1e-6 for r in funding_hist)

    cap_note = None
    if is_funding_capped:
        cap_side = "TABANINDA" if current_rate < 0 else "TAVANINDA"
        cap_note = f"Funding borsa {cap_side} ({current_rate * 100:+.2f}% CAP) — Gerçek dengesizlik sınırın çok üzerinde!"
    elif funding_stuck and abs(price_change_radar) >= 3.0:
        cap_note = f"Funding {len(funding_hist)} taramadır sabit ({current_rate * 100:+.2f}%), fiyat %{price_change_radar:+.1f} hareket etti!"

    # ── 2. Emilim (Absorption) / CVD-Fiyat Uyumsuzluğu ──
    absorption_note = None
    if seen_count >= 2:
        if current_cvd > 0 and (current_cvd >= prev_cvd or current_cvd > 1_000_000) and price_change_prev_scan < -1.0:
            absorption_note = (
                f"⚡ Emilim (Absorption): Agresif alıcı baskısına rağmen ({_fmt_cvd(current_cvd)} CVD), "
                f"fiyat son taramadan beri %{price_change_prev_scan:.1f} geriledi. Limit satıcılar alımları emiyor."
            )
        elif current_cvd < 0 and price_change_prev_scan > 1.0:
            absorption_note = (
                f"⚡ Pasif Alıcı Emilimi: Satıcı CVD baskısına rağmen ({_fmt_cvd(current_cvd)} CVD), "
                f"fiyat son taramadan beri %{price_change_prev_scan:+.1f} yükseldi. Limit alıcılar satışları emiyor."
            )

    # ── 3. Resmi 4 Faz Taksonomisine Göre Deterministik Faz Tespiti ──
    # Resmi Fazlar:
    # 1. 🟡 SIKIŞMA BİRİKİYOR (Erken Aşama)
    # 2. 🚀 SQUEEZE DEVAM EDİYOR (In Progress)
    # 3. 🛑 GEÇ AŞAMA (Tükenme Riski)
    # 4. 🔵 BASKI AZALIYOR (Funding Gevşiyor)
    regime_note = None

    if is_recent_listing and abs(chg_24h) > 20.0:
        regime_note = f"⚠️ Rejim: Yeni Listeleme (24s {chg_24h:+.1f}% açılış mumundan kalma, son {recent_bars_count}s'de dipte dengelendi)"
    elif chg_24h <= -20.0:
        regime_note = f"⚠️ Rejim: Ağır Çöküş Trendi (24s {chg_24h:+.1f}%)"
    elif chg_24h >= 25.0:
        regime_note = f"⚠️ Rejim: Parabolik Yükseliş Trendi (24s {chg_24h:+.1f}%)"

    if squeeze_type == "short":
        if funding_easing:
            phase = "EASING"
            phase_label = "🔵 BASKI AZALIYOR (Funding Gevşiyor)"
            if is_recent_listing:
                phase_desc = (
                    f"24s'deki %{chg_24h:.1f} düşüş açılış listeleme mumundan kaynaklı; "
                    f"son {recent_bars_count} saatte fiyat dip seviyeden toparlandı ({recent_trend_pct:+.1f}%). "
                    "OI yatay ve likidasyon $0 (aktif kapitülasyon satışları durdu). "
                    f"Funding zayıflıyor ({first_rate*100:+.2f}% ➔ {current_rate*100:+.2f}%), short baskısı sönümleniyor."
                )
            else:
                phase_desc = (
                    f"Funding negatifliği zayıflıyor ({first_rate*100:+.2f}% ➔ {current_rate*100:+.2f}%). "
                    "Short pozisyonlar kapandı veya baskı dağıldı, squeeze potansiyeli güç kaybediyor."
                )
        elif price_change_radar >= 18.0 or (seen_count >= 3 and price_change_radar >= 12.0):
            phase = "LATE"
            phase_label = "🛑 GEÇ AŞAMA (Tükenme / Doygunluk Riski)"
            phase_desc = (
                f"Fiyat ilk tespitten ({first_seen_str}) beri {price_change_radar:+.1f}% primlendi. "
                "Erken alıcılar kâr realizasyonu yapabilir, trene geç binme riski yüksek."
            )
        elif price_change_radar >= 4.5 or (seen_count >= 2 and price_change_radar >= 3.0 and current_rate < -0.01):
            phase = "IN_PROGRESS"
            phase_label = "🚀 SQUEEZE DEVAM EDİYOR (In Progress)"
            phase_desc = (
                f"Squeeze devrede! İlk tespitten ({first_seen_str}) beri fiyat {price_change_radar:+.1f}%. "
                "Short tasfiyeleri/kapanışları alım baskısını besliyor."
            )
        else:
            phase = "POTENTIAL"
            phase_label = "🟡 SIKIŞMA BİRİKİYOR (Erken Aşama Adayı)"
            phase_desc = "Fiyat henüz dar bantta konsolide oluyor, aşırı negatif funding gerilim biriktiriyor."

    else:  # long squeeze
        if funding_easing:
            phase = "EASING"
            phase_label = "🔵 BASKI AZALIYOR (Funding Gevşiyor)"
            phase_desc = (
                f"Pozitif funding zayıflıyor ({first_rate*100:+.2f}% ➔ {current_rate*100:+.2f}%). "
                "Long kalabalığı dağılıyor, aşağı kırılma riski azaldı."
            )
        elif price_change_radar <= -18.0:
            phase = "LATE"
            phase_label = "🛑 GEÇ AŞAMA (Düşüş Doygunluğu)"
            phase_desc = f"Fiyat ilk tespitten beri {price_change_radar:.1f}% çöktü. Satış doygunluğu veya tepki alımı gelebilir."
        elif price_change_radar <= -4.5:
            phase = "IN_PROGRESS"
            phase_label = "🚀 SQUEEZE DEVAM EDİYOR (Long Tasfiyesi)"
            phase_desc = f"Long'lar likide ediliyor, ilk tespitten ({first_seen_str}) beri {price_change_radar:.1f}% çöküş."
        else:
            phase = "POTENTIAL"
            phase_label = "🟡 SIKIŞMA BİRİKİYOR (Long Squeeze Adayı)"
            phase_desc = "Fiyat henüz kırılmadı, aşırı pozitif funding aşağı yönlü gerilim biriktiriyor."

    # ── 4. Matematiksel Olarak Netleştirilmiş Zaman Serisi Özeti ──
    history_summary = None
    if seen_count >= 2:
        p_seq = " ➔ ".join(_fmt_price(p) for p in price_hist[-3:])
        f_seq = " ➔ ".join(f"{r*100:+.2f}%" for r in funding_hist[-3:])
        step_sign = "+" if price_change_prev_scan >= 0 else ""
        total_sign = "+" if price_change_radar >= 0 else ""
        history_summary = {
            "p_line": f"Fiyat: {p_seq} (Son adım: {step_sign}{price_change_prev_scan:.1f}% | İlkten: {total_sign}{price_change_radar:.1f}%)",
            "f_line": f"Funding: {f_seq} ({funding_dir_label})",
            "first_rate_pct": f"{first_rate*100:+.2f}%",
        }

    history[symbol] = {
        "seen_count": seen_count,
        "first_seen_str": first_seen_str,
        "first_price": first_price,
        "first_rate": first_rate,
        "last_price": current_price,
        "prev_price": prev_price,
        "last_rate": current_rate,
        "last_cvd": current_cvd,
        "price_change_radar_pct": price_change_radar,
        "price_change_prev_scan_pct": price_change_prev_scan,
        "price_history": price_hist,
        "funding_history": funding_hist,
        "oi_history": oi_hist,
        "cvd_history": cvd_hist,
        "last_updated_ts": now_ts,
    }

    coin["radar_seen_count"] = seen_count
    coin["radar_first_seen_str"] = first_seen_str
    coin["radar_first_price"] = first_price
    coin["radar_first_rate"] = first_rate
    coin["radar_price_change_pct"] = price_change_radar
    coin["radar_cap_note"] = cap_note
    coin["radar_regime_note"] = regime_note
    coin["radar_absorption_note"] = absorption_note
    coin["radar_phase"] = phase
    coin["radar_phase_label"] = phase_label
    coin["radar_phase_desc"] = phase_desc
    coin["radar_history_summary"] = history_summary


# ──────────────────────────────────────────────────────────────────────
#  Güven Skoru — Squeeze tezini destekleyen 4 kriterin doğrulaması
# ──────────────────────────────────────────────────────────────────────

def _compute_squeeze_confidence(coin, squeeze_type):
    """Squeeze tezini destekleyen/çürüten sinyal sayısına göre güven skoru üretir.
    4 Kriter: CVD (baskı), Supertrend (trend), OI (pozisyon kapanışı), 24s Değişim (momentum).
    """
    score = 0
    warnings = []
    confirmations = []
    criteria_detail = []

    cvd = coin.get("cvd")
    st = coin.get("supertrend")
    oi = coin.get("oi_change_pct")
    chg = coin.get("price_change_pct_24h")

    if squeeze_type == "short":
        # ── 1. CVD ──
        if cvd is None:
            criteria_detail.append("CVD: ? (veri yok)")
        elif cvd > 0:
            if oi is not None and oi < -1.5:
                criteria_detail.append("CVD: ✅ Alıcı (short kapanıyor — organik olabilir)")
                warnings.append("CVD+OI↓ organik kapanma olasılığı")
            else:
                criteria_detail.append("CVD: ✅ Alıcı baskın")
            score += 1
            confirmations.append("CVD alıcı")
        else:
            criteria_detail.append("CVD: ❌ Satıcı baskın")
            warnings.append("CVD satıcı baskın")

        # ── 2. Supertrend ──
        if st is None:
            criteria_detail.append("ST: ? (veri yok)")
        elif st == 1:
            criteria_detail.append("ST: ✅ LONG")
            score += 1
            confirmations.append("ST LONG")
        else:
            criteria_detail.append("ST: ❌ SHORT sinyali")
            warnings.append("ST SHORT sinyali")

        # ── 3. OI Değişimi ──
        if oi is None:
            criteria_detail.append("OI: ? (veri yok)")
        elif oi < -1.5:
            criteria_detail.append(f"OI: ✅ Düşüş {oi:.1f}% (short kapanıyor)")
            score += 1
            confirmations.append("OI azalıyor")
        elif oi > 4.0:
            criteria_detail.append(f"OI: ⚠️ Artış +{oi:.1f}% (short birikimi)")
            warnings.append("OI artıyor (short birikimi)")
        else:
            criteria_detail.append(f"OI: → Nötr {oi:+.1f}%")

        # ── 4. 24s Değişim (Vadeli) ──
        if chg is None:
            criteria_detail.append("24s: ? (veri yok)")
        elif chg > 2.0:
            criteria_detail.append(f"24s: ✅ {chg:+.1f}% pozitif momentum")
            score += 1
            confirmations.append(f"24s +{chg:.1f}% momentum")
        elif chg < -5.0:
            criteria_detail.append(f"24s: ❌ {chg:.1f}% düşüş")
            warnings.append(f"24s {chg:.1f}% düşüş")
        else:
            criteria_detail.append(f"24s: → Nötr {chg:+.1f}%")

    else:  # long squeeze
        # ── 1. CVD ──
        if cvd is None:
            criteria_detail.append("CVD: ? (veri yok)")
        elif cvd < 0:
            if oi is not None and oi < -1.5:
                criteria_detail.append("CVD: ✅ Satıcı (long kapanıyor — organik olabilir)")
                warnings.append("CVD+OI↓ organik kapanma olasılığı")
            else:
                criteria_detail.append("CVD: ✅ Satıcı baskın")
            score += 1
            confirmations.append("CVD satıcı")
        else:
            criteria_detail.append("CVD: ❌ Alıcı baskın")
            warnings.append("CVD alıcı baskın")

        # ── 2. Supertrend ──
        if st is None:
            criteria_detail.append("ST: ? (veri yok)")
        elif st == -1:
            criteria_detail.append("ST: ✅ SHORT")
            score += 1
            confirmations.append("ST SHORT")
        else:
            criteria_detail.append("ST: ❌ LONG sinyali")
            warnings.append("ST LONG sinyali")

        # ── 3. OI Değişimi ──
        if oi is None:
            criteria_detail.append("OI: ? (veri yok)")
        elif oi < -1.5:
            criteria_detail.append(f"OI: ✅ Düşüş {oi:.1f}% (long kapanıyor)")
            score += 1
            confirmations.append("OI azalıyor")
        elif oi > 4.0:
            criteria_detail.append(f"OI: ⚠️ Artış +{oi:.1f}% (long birikimi)")
            warnings.append("OI artıyor (long birikimi)")
        else:
            criteria_detail.append(f"OI: → Nötr {oi:+.1f}%")

        # ── 4. 24s Değişim (Vadeli) ──
        if chg is None:
            criteria_detail.append("24s: ? (veri yok)")
        elif chg < -2.0:
            criteria_detail.append(f"24s: ✅ {chg:.1f}% düşüş momentumu")
            score += 1
            confirmations.append(f"24s {chg:.1f}% düşüş")
        elif chg > 5.0:
            criteria_detail.append(f"24s: ❌ +{chg:.1f}% yükseliş")
            warnings.append(f"24s +{chg:.1f}% yükseliş")
        else:
            criteria_detail.append(f"24s: → Nötr {chg:+.1f}%")

    coin["squeeze_score"] = score
    coin["squeeze_warnings"] = warnings
    coin["squeeze_confirmations"] = confirmations
    coin["squeeze_criteria"] = criteria_detail
    return score, warnings, confirmations


# ──────────────────────────────────────────────────────────────────────
#  Zenginleştirme — Her coin için ek verileri topla
# ──────────────────────────────────────────────────────────────────────

def _enrich_coin(coin, futures_map, spot_map, oi_history):
    """Tek coin için Vadeli 24s, Spot, OI, Likidasyon, CVD ve Teknik verileri toplar."""
    symbol = coin["symbol"]
    mark_price = coin["mark_price"]

    # === 1. Vadeli 24s Verileri ===
    f_info = futures_map.get(symbol)
    if f_info:
        try:
            coin["price_change_pct_24h"] = float(f_info.get("priceChangePercent", 0))
            coin["futures_volume_usd"] = float(f_info.get("quoteVolume", 0))
            coin["high_24h"] = float(f_info.get("highPrice", 0))
            coin["low_24h"] = float(f_info.get("lowPrice", 0))
        except (ValueError, TypeError):
            coin["price_change_pct_24h"] = None
            coin["futures_volume_usd"] = None
            coin["high_24h"] = None
            coin["low_24h"] = None
    else:
        coin["price_change_pct_24h"] = None
        coin["futures_volume_usd"] = None
        coin["high_24h"] = None
        coin["low_24h"] = None

    # === 2. Spot Ticker (Basis ve arbitraj tespiti) ===
    spot_info = spot_map.get(symbol)
    if spot_info:
        coin["is_futures_only"] = False
        coin["spot_volume_usd"] = spot_info.get("quoteVolume", 0)
        try:
            coin["spot_price"] = float(spot_info.get("lastPrice", 0)) or None
        except (ValueError, TypeError):
            coin["spot_price"] = None
    else:
        coin["is_futures_only"] = True
        coin["spot_volume_usd"] = None
        coin["spot_price"] = None

    # === 3. OI Değişimi ===
    try:
        current_oi = get_open_interest(symbol)
        coin["current_oi"] = current_oi
        prev_oi = oi_history.get(symbol)
        if prev_oi and prev_oi > 0:
            coin["oi_change_pct"] = (current_oi - prev_oi) / prev_oi * 100
        else:
            coin["oi_change_pct"] = None
    except Exception:
        coin["current_oi"] = None
        coin["oi_change_pct"] = None

    # === 4. Likidasyon Verisi ===
    # /fapi/v1/forceOrders kullanıcı hesabına ait imzalı USER_DATA endpoint'idir;
    # public piyasa likidasyon akışı değildir. İmzalı katman yokken çağırma.
    if getattr(SETTINGS, "USE_LIQUIDATION_DATA", False):
        log.warning("Funding Radar forceOrders verisini atlıyor: signed USER_DATA altyapısı henüz yok")
    coin["liq_total_usd"] = None
    coin["liq_imbalance"] = None
    coin["liq_top_price"] = None
    coin["liq_top_usd"] = 0.0
    coin["liq_side"] = None

    # === 5. CVD, Kısa Vadeli Bar Momentumu ve Teknik Seviyeler (Kline) ===
    try:
        tech_bars = SETTINGS.FUNDING_RADAR_TECH_BARS
        tech_interval = SETTINGS.FUNDING_RADAR_TECH_INTERVAL
        df = get_klines(symbol, interval=tech_interval, limit=tech_bars)

        if df is not None and len(df) >= 3:
            n_bars = len(df)
            coin["is_recent_listing"] = n_bars < 24  # 24 saatten az geçmişi varsa yeni listelenme

            # Kısa vadeli bar momentumu (son 4 bar veya mevcut bar sayısı)
            bars_back = min(4, n_bars)
            open_past = float(df["open"].iloc[-bars_back])
            close_now = float(df["close"].iloc[-1])
            coin["recent_trend_pct"] = (close_now - open_past) / open_past * 100 if open_past > 0 else 0.0
            coin["recent_bars_count"] = bars_back

            # CVD hesaplama
            cvd_bars = min(SETTINGS.FUNDING_RADAR_CVD_BARS, n_bars)
            cvd_df = df.iloc[-cvd_bars:]
            cvd_value = _calc_cvd_from_df(cvd_df)
            coin["cvd"] = cvd_value
            if cvd_value > 0:
                coin["cvd_label"] = "Alıcı baskın"
            elif cvd_value < 0:
                coin["cvd_label"] = "Satıcı baskın"
            else:
                coin["cvd_label"] = "Nötr"

            # Supertrend (yeterli bar varsa)
            if n_bars >= 10:
                trend, _, _ = compute_supertrend(
                    df,
                    period=min(SETTINGS.SUPERTREND_PERIOD, n_bars - 1),
                    multiplier=SETTINGS.SUPERTREND_MULTIPLIER,
                    change_atr=SETTINGS.SUPERTREND_CHANGE_ATR,
                )
                coin["supertrend"] = int(trend[-1])
            else:
                coin["supertrend"] = None

            sup, res, sup_src, res_src = _compute_support_resistance(
                df,
                mark_price=mark_price,
                low_24h=coin.get("low_24h"),
                high_24h=coin.get("high_24h"),
                max_distance_pct=0.30
            )
            coin["support"] = sup
            coin["resistance"] = res
            coin["support_source"] = sup_src
            coin["resistance_source"] = res_src
        else:
            coin["is_recent_listing"] = False
            coin["recent_trend_pct"] = None
            coin["recent_bars_count"] = 4
            coin["cvd"] = None
            coin["cvd_label"] = None
            coin["supertrend"] = None
            coin["support"] = coin.get("low_24h")
            coin["resistance"] = coin.get("high_24h")
            coin["support_source"] = "(24s Dip)" if coin.get("low_24h") else ""
            coin["resistance_source"] = "(24s Zirve)" if coin.get("high_24h") else ""
    except Exception as e:
        log.debug(f"Kline enrichment hatası ({symbol}): {e}")
        coin["is_recent_listing"] = False
        coin["recent_trend_pct"] = None
        coin["recent_bars_count"] = 4
        coin["cvd"] = None
        coin["cvd_label"] = None
        coin["supertrend"] = None
        coin["support"] = None
        coin["resistance"] = None
        coin["support_source"] = ""
        coin["resistance_source"] = ""

    return coin


# ──────────────────────────────────────────────────────────────────────
#  Ana tarama fonksiyonu
# ──────────────────────────────────────────────────────────────────────

def scan_funding_radar():
    """Tüm piyasayı tarayıp funding rate aşırılıklarını tespit eder."""
    try:
        data = get_all_premium_index()
    except Exception as e:
        log.error(f"premiumIndex API hatası: {e}")
        return None

    if not data or not isinstance(data, list):
        return None

    min_rate = SETTINGS.FUNDING_RADAR_MIN_RATE
    top_n = SETTINGS.FUNDING_RADAR_TOP_N

    # Tüm coin'leri parse et
    coins = []
    for item in data:
        try:
            symbol = item.get("symbol", "")
            rate_str = item.get("lastFundingRate", "0")
            mark_str = item.get("markPrice", "0")

            rate = float(rate_str)
            mark_price = float(mark_str)

            if not symbol.endswith("USDT"):
                continue
            if symbol in SETTINGS.SYMBOL_BLACKLIST:
                continue

            try:
                next_funding_ts = int(item.get("nextFundingTime", 0))
                funding_interval_h = float(item.get("fundingIntervalHours", 8.0))
            except (ValueError, TypeError):
                next_funding_ts = 0
                funding_interval_h = 8.0

            coins.append({
                "symbol": symbol,
                "rate": rate,
                "mark_price": mark_price,
                "next_funding_ts": next_funding_ts,
                "funding_interval_h": funding_interval_h,
            })
        except (ValueError, TypeError):
            continue

    if not coins:
        return None

    extreme_coins = [c for c in coins if abs(c["rate"]) >= min_rate]

    if not extreme_coins:
        log.info("📡 Funding Radar: Aşırı funding'li coin bulunamadı. Piyasa sakin.")
        return None

    negative = sorted([c for c in extreme_coins if c["rate"] < 0], key=lambda x: x["rate"])
    positive = sorted([c for c in extreme_coins if c["rate"] > 0], key=lambda x: x["rate"], reverse=True)

    short_squeeze = negative[:top_n]
    long_squeeze = positive[:top_n]

    total_neg = len(negative)
    total_pos = len(positive)
    if total_neg > total_pos * 1.5:
        sentiment = "BEARISH"
    elif total_pos > total_neg * 1.5:
        sentiment = "BULLISH"
    else:
        sentiment = "NÖTR"

    for c in long_squeeze + short_squeeze:
        c["rate_pct"] = f"{c['rate'] * 100:+.4f}%"

    if SETTINGS.FUNDING_RADAR_ENRICH:
        enrichment_candidates = long_squeeze + short_squeeze
        enrich_count = len(enrichment_candidates)

        futures_map = {}
        try:
            f_tickers = get_24hr_tickers()
            for t in f_tickers:
                sym = t.get("symbol", "")
                if sym:
                    futures_map[sym] = t
        except Exception as e:
            log.warning(f"Futures 24hr ticker verisi çekilemedi: {e}")

        spot_map = {}
        try:
            spot_tickers = get_all_spot_tickers()
            for t in spot_tickers:
                sym = t.get("symbol", "")
                if sym:
                    try:
                        t["quoteVolume"] = float(t.get("quoteVolume", 0))
                    except (ValueError, TypeError):
                        t["quoteVolume"] = 0
                    spot_map[sym] = t
        except Exception as e:
            log.warning(f"Spot ticker verisi çekilemedi: {e}")

        oi_history = _load_oi_history()
        radar_history = _load_radar_history()

        enrich_ok = 0
        for coin in enrichment_candidates:
            try:
                _enrich_coin(coin, futures_map, spot_map, oi_history)
                s_type = "short" if coin in short_squeeze else "long"
                _compute_squeeze_confidence(coin, s_type)
                _update_radar_tracking(coin, s_type, radar_history)
                enrich_ok += 1
            except Exception as e:
                log.debug(f"Enrichment hatası ({coin['symbol']}): {e}")

        _save_radar_history(radar_history)
        log.info(f"📡 Radar Enrichment: {enrich_ok}/{enrich_count} coin zenginleştirildi")

    scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    result = {
        "long_squeeze_candidates": long_squeeze,
        "short_squeeze_candidates": short_squeeze,
        "total_extreme": len(extreme_coins),
        "total_neg": total_neg,
        "total_pos": total_pos,
        "market_sentiment": sentiment,
        "scan_time": scan_time,
    }

    log.info(
        f"📡 Funding Radar: {len(extreme_coins)} aşırı coin "
        f"(🟢 {total_neg} negatif, 🔴 {total_pos} pozitif) | Eğilim: {sentiment}"
    )

    return result


# ──────────────────────────────────────────────────────────────────────
#  Formatlama yardımcıları
# ──────────────────────────────────────────────────────────────────────

def _fmt_price(price):
    if price is None:
        return "—"
    if price >= 1000:
        return f"{price:,.0f}"
    elif price >= 1:
        return f"{price:.2f}"
    elif price >= 0.01:
        return f"{price:.4f}"
    else:
        return f"{price:.7f}"


def _fmt_usd(value):
    if value is None or value == 0:
        return "$0"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    elif value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def _fmt_cvd(value):
    if value is None:
        return "N/A"
    sign = "+" if value >= 0 else ""
    if abs(value) >= 1_000_000:
        return f"{sign}{value / 1_000_000:.1f}M"
    elif abs(value) >= 1_000:
        return f"{sign}{value / 1_000:.0f}K"
    return f"{sign}{value:.0f}"


def _fmt_next_funding(next_funding_ts):
    if not next_funding_ts:
        return "N/A"
    try:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        remaining_ms = next_funding_ts - now_ms
        if remaining_ms <= 0:
            return "yakında"
        remaining_min = int(remaining_ms / 60000)
        h, m = divmod(remaining_min, 60)
        return f"{h}s {m:02d}dk" if h > 0 else f"{m}dk"
    except Exception:
        return "N/A"


def _fmt_price_diff(pct):
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def _fmt_enriched_coin(c, index):
    """Zenginleştirilmiş coin'i taksonomiye uygun faz, doğru matematik ve şeffaf zaman serisiyle formata çevir."""
    rate_pct = c["rate"] * 100
    sym = html.escape(str(c["symbol"]), quote=False)
    lines = []

    # Satır 1: Sembol, funding rate (periyot), fiyat, sonraki funding
    interval_h = c.get("funding_interval_h", 8.0)
    next_f = _fmt_next_funding(c.get("next_funding_ts", 0))
    lines.append(
        f"  <b>{index}. {sym}</b> {rate_pct:+.4f}% ({interval_h:.0f}s periyot) "
        f" 💰 {_fmt_price(c['mark_price'])}  ⏳ {next_f}"
    )

    # Satır 2: Resmi 4 Fazdan Biri & Tarama Sırası
    phase_label = c.get("radar_phase_label", "🟡 SIKIŞMA BİRİKİYOR (Erken Aşama)")
    seen_cnt = c.get("radar_seen_count", 1)
    lines.append(f"     <b>{phase_label}</b> | {seen_cnt}. tarama")

    # Satır 2b: Varsa Piyasa Rejimi (Yeni Listeleme / Çöküş / Parabolik)
    if c.get("radar_regime_note"):
        lines.append(f"     {c['radar_regime_note']}")

    # Satır 3: Zaman Serisi Özeti (Başlangıç Değerleri + Fiyat ve Funding Trendi)
    hs = c.get("radar_history_summary")
    if hs:
        first_time = c.get("radar_first_seen_str", "")
        first_p = _fmt_price(c.get("radar_first_price"))
        first_r = hs.get("first_rate_pct", "")
        lines.append(f"     🕒 Takip ({seen_cnt}. tarama | İlk: {first_time}, 💰{first_p}, ⚡{first_r}):")
        lines.append(f"        └ {hs['p_line']}")
        lines.append(f"        └ {hs['f_line']}")

    # Satır 4: OI + yalnızca varsa yaklaşık OI çözülmesi
    oi_str = "N/A"
    if c.get("oi_change_pct") is not None:
        oi_pct = c["oi_change_pct"]
        oi_arrow = "↑" if oi_pct > 0 else "↓" if oi_pct < 0 else "→"
        oi_str = f"{oi_arrow}{abs(oi_pct):.1f}%"

    if c.get("liq_top_price") is not None and c.get("liq_top_usd", 0) > 0:
        side_str = f" ({c['liq_side']})" if c.get("liq_side") else ""
        liq_str = f"{_fmt_usd(c['liq_top_usd'])} @{_fmt_price(c['liq_top_price'])}{side_str}"
    elif c.get("oi_change_pct") is not None and c["oi_change_pct"] < -1.0 and c.get("current_oi"):
        est_unwind_usd = abs(c["oi_change_pct"] / 100.0) * c["current_oi"] * c["mark_price"]
        liq_str = f"~{_fmt_usd(est_unwind_usd)} (OI tasfiye tahmini)"
    else:
        liq_str = "veri yok (hesap forceOrders erişimi yok)"

    lines.append(f"     📊 OI: {oi_str} | 💥 Liq: {liq_str}")

    # Satır 5: Vadeli 24s Değişim + Kısa Vadeli Bar Momentumu + Basis / Spot + CVD
    chg_24h = c.get("price_change_pct_24h")
    chg_24h_str = ""
    if chg_24h is not None:
        chg_icon = "📈" if chg_24h >= 0 else "📉"
        listing_tag = " (Yeni Liste)" if c.get("is_recent_listing") else ""
        chg_24h_str = f"{chg_icon} 24s: {chg_24h:+.1f}%{listing_tag}"
    else:
        chg_24h_str = "24s: N/A"

    recent_str = ""
    if c.get("recent_trend_pct") is not None and c.get("is_recent_listing"):
        r_pct = c["recent_trend_pct"]
        r_icon = "🟢" if r_pct >= 0 else "🔴"
        r_sign = "+" if r_pct >= 0 else ""
        recent_str = f" | {r_icon} Son {c.get('recent_bars_count', 4)}s: {r_sign}{r_pct:.1f}%"

    basis_str = ""
    if c.get("spot_price") and c.get("mark_price"):
        basis_pct = (c["mark_price"] - c["spot_price"]) / c["spot_price"] * 100
        basis_sign = "+" if basis_pct >= 0 else ""
        basis_str = f" | basis {basis_sign}{basis_pct:.2f}%"
    elif c.get("is_futures_only"):
        basis_str = " | ⚠️ futures-only"

    if c.get("cvd") is not None:
        cvd_str = f"{c['cvd_label']} ({_fmt_cvd(c['cvd'])})"
    else:
        cvd_str = "N/A"

    lines.append(f"     📈 {chg_24h_str}{recent_str}{basis_str} | 🔄 CVD: {cvd_str}")

    # Satır 6: Destek + Direnç + Supertrend (Asla boş kalmaz)
    sup_val = _fmt_price(c.get("support"))
    sup_src = f" {c.get('support_source', '')}".rstrip()
    sup_str = f"{sup_val}{sup_src}" if c.get("support") else "—"

    res_val = _fmt_price(c.get("resistance"))
    res_src = f" {c.get('resistance_source', '')}".rstrip()
    res_str = f"{res_val}{res_src}" if c.get("resistance") else "—"

    if c.get("supertrend") is not None:
        st_str = "🟢 LONG" if c["supertrend"] == 1 else "🔴 SHORT"
    else:
        st_str = "N/A"

    lines.append(f"     🛡️ Destek: {sup_str} | 🎯 Direnç: {res_str} | ST: {st_str}")

    # Satır 7: Güven Skoru + 4 Kriter
    score = c.get("squeeze_score")
    if score is not None:
        score_bar = "🟩" * score + "⬜" * (4 - score)
        score_label = ["Çok Zayıf", "Zayıf", "Orta", "Güçlü", "Çok Güçlü"][score]
        lines.append(f"     🎯 Güven: {score_bar} {score_label} ({score}/4)")

        for criterion in c.get("squeeze_criteria", []):
            lines.append(f"       └ {criterion}")

        # Piyasa Teşhisi (Objektif durum tespiti, asla yanıltıcı iyimser dil yok)
        if c.get("radar_phase_desc"):
            lines.append(f"     💡 Teşhis: {c['radar_phase_desc']}")

        # Emilim (Absorption) / Uyumsuzluk uyarısı
        if c.get("radar_absorption_note"):
            lines.append(f"     {c['radar_absorption_note']}")

        # Borsa Cap uyarısı
        if c.get("radar_cap_note"):
            lines.append(f"     🚨 {c['radar_cap_note']}")

        # Çelişki uyarıları
        warns = c.get("squeeze_warnings", [])
        if warns:
            lines.append(f"     ⚠️ Çelişki: {' | '.join(warns)}")

    return "\n".join(lines)


def fmt_funding_radar(result):
    """Funding radar sonucunu Telegram mesaj formatına çevir."""
    lines = [
        "<b>📡 FUNDING RATE SQUEEZE RADARI</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    lines.append("ℹ️ Short Squeeze = aşırı NEGATİF funding (short'lar kalabalık, yukarı patlama potansiyeli)")
    lines.append("ℹ️ Long Squeeze = aşırı POZİTİF funding (long'lar kalabalık, aşağı çakılma riski)")

    short_candidates = result["short_squeeze_candidates"]
    if short_candidates:
        lines.append("")
        lines.append("<b>🟢 SHORT SQUEEZE RADARI</b> (Negatif Funding)")
        for i, c in enumerate(short_candidates, 1):
            lines.append(_fmt_enriched_coin(c, i))

    long_candidates = result["long_squeeze_candidates"]
    if long_candidates:
        lines.append("")
        lines.append("<b>🔴 LONG SQUEEZE RADARI</b> (Pozitif Funding)")
        for i, c in enumerate(long_candidates, 1):
            lines.append(_fmt_enriched_coin(c, i))

    sentiment_icon = {"BULLISH": "📈", "BEARISH": "📉", "NÖTR": "➡️"}.get(result["market_sentiment"], "➡️")
    total_extreme = result["total_extreme"]
    lines.append("")
    lines.append(
        f"📊 Piyasa: {total_extreme} aşırı funding'li coin "
        f"(🟢{result['total_neg']} / 🔴{result['total_pos']}) | "
        f"Eğilim: {sentiment_icon} {result['market_sentiment']}"
    )

    if total_extreme <= 3:
        lines.append(
            f"⚠️ Uyarı: Sadece {total_extreme} coin'den piyasa eğilimi çıkarmak "
            "istatistiksel olarak zayıf. Daha geniş piyasa verisini kontrol edin."
        )

    lines.append(f"⏰ {result['scan_time']}")
    lines.append("ℹ️ Fazlar: Sıkışma Birikimi ➔ Devam Ediyor ➔ Geç Aşama (Tükenme) ➔ Baskı Azalıyor")

    return "\n".join(lines)
