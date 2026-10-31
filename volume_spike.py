"""
Volume Spike Dedektörü — Ani hacim patlamalarını tespit eder ve Telegram bildirimi gönderir.

Mevcut sinyal akışından (sweep + skorlama) bağımsız çalışır; ancak Whale Alert
ve Squeeze Radar ile aynı analitik derinliğe (Taker akışı, OI analizi,
destek/direnç, teyit/geçersizleşme kriterleri) sahiptir.

Tespit mantığı:
  1. Son N barın hacim ortalamasını hesapla (son bar hariç)
  2. Son kapanmış barın hacmini ortalamaya böl → Spike Çarpanı
  3. Fiyat değişimini hesapla (|ΔFiyat| >= min_pct)
  4. Taker Buy/Sell ayrımı ile hacmin alıcı mı satıcı mı olduğunu belirle (absorpsiyon kontrolü)
  5. Open Interest (OI) değişimi ile pozisyon açılışı / tasfiye ayrımı yap
  6. Son N barın dip/tepe seviyeleriyle destek/direnç ve kırılım durumunu analiz et
  7. Tekrar eden spike'ları zaman serisinde takip et (son 1 saatteki tekrar sayısı)
"""
import html
import json
import os
import threading
import time

import pandas as pd

from config import SETTINGS
from exchange import get_open_interest_history
from logger import get_logger

log = get_logger("volume_spike")

_SPIKE_LOCK = threading.RLock()
_SPIKE_CACHE = {"mtime": None, "data": {}}


def _interval_seconds(interval):
    """'5m', '15m', '1h', '4h', '1d' gibi interval stringlerini saniyeye çevirir."""
    try:
        unit = interval[-1]
        val = int(interval[:-1])
        return val * {"m": 60, "h": 3600, "d": 86400}[unit]
    except Exception:
        return 300  # varsayılan 5 dakika


def _fmt_volume(v):
    """Hacmi okunabilir formata çevir: 1234 → 1.23K, 1234567 → 1.23M"""
    if v is None:
        return "N/A"
    abs_v = abs(v)
    sign = "-" if v < 0 else ""
    if abs_v >= 1_000_000:
        return f"{sign}{abs_v / 1_000_000:.2f}M"
    elif abs_v >= 1_000:
        return f"{sign}{abs_v / 1_000:.2f}K"
    return f"{sign}{abs_v:.2f}"


def _fmt_usd(v):
    """USD tutarını okunabilir formata çevir: 1234567 → $1.23M"""
    if v is None:
        return "N/A"
    return f"${_fmt_volume(v)}"


def _fmt_price(p):
    """Fiyatı coin'in büyüklüğüne göre uyarlanmış hassasiyetle biçimle."""
    if p is None:
        return "N/A"
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 0.1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def _load_spike_alerts():
    """Cooldown ve tekrar takibi için spike alert geçmişini yükle (thread-safe + cache)."""
    path = SETTINGS.VOLUME_SPIKE_ALERT_FILE
    with _SPIKE_LOCK:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            if _SPIKE_CACHE["mtime"] is not None:
                _SPIKE_CACHE["mtime"] = None
                _SPIKE_CACHE["data"] = {}
            return _SPIKE_CACHE["data"]
        if _SPIKE_CACHE["mtime"] == mtime:
            return _SPIKE_CACHE["data"]
        try:
            with open(path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("beklenen biçim dict değil")
        except Exception as e:
            log.warning(f"Spike alert dosyası okunamadı ({path}): {e}")
            return _SPIKE_CACHE["data"]
        _SPIKE_CACHE["mtime"] = mtime
        _SPIKE_CACHE["data"] = data
        return data


def _save_spike_alerts(data):
    """Spike alert geçmişini diske yaz (thread-safe, Windows-safe atomic replace)."""
    path = SETTINGS.VOLUME_SPIKE_ALERT_FILE
    with _SPIKE_LOCK:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, default=str)
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    _SPIKE_CACHE["mtime"] = os.path.getmtime(path)
                    _SPIKE_CACHE["data"] = data
                    return
                except (PermissionError, OSError):
                    if attempt < 4:
                        time.sleep(0.05 * (attempt + 1))
                    else:
                        try:
                            import shutil
                            shutil.copy2(tmp, path)
                            if os.path.exists(tmp):
                                os.remove(tmp)
                            _SPIKE_CACHE["mtime"] = os.path.getmtime(path)
                            _SPIKE_CACHE["data"] = data
                        except Exception:
                            pass
        except Exception as e:
            log.warning(f"Spike alert kaydetme uyarısı ({path}): {e}")


def check_volume_spike(symbol, df, mark_price_map=None):
    """Bir sembol için hacim patlaması kontrolü yapar.
    
    Args:
        symbol: Sembol adı (örn: "GRASSUSDT")
        df: Kapanmış mumları içeren DataFrame (closed_df)
        mark_price_map: Opsiyonel anlık mark price sözlüğü
        
    Returns:
        dict: Spike tespit edildiyse zenginleştirilmiş bilgi dict'i, yoksa None
    """
    if df is None or len(df) < SETTINGS.VOLUME_SPIKE_LOOKBACK + 2:
        return None

    lookback = SETTINGS.VOLUME_SPIKE_LOOKBACK
    interval = getattr(SETTINGS, "INTERVAL", "5m")
    period_s = _interval_seconds(interval)
    period_mins = max(1, period_s // 60)
    lookback_mins = lookback * period_mins

    # Son kapanmış bar
    last = df.iloc[-1]
    spike_vol = float(last["volume"])
    open_price = float(last["open"])
    close_price = float(last["close"])
    high_price = float(last["high"])
    low_price = float(last["low"])
    bar_time = last.get("open_time", "")

    # Önceki N barın hacim ortalaması (son bar hariç)
    prev_vols = df["volume"].iloc[-(lookback + 1):-1].values
    avg_vol = float(prev_vols.mean())

    if avg_vol <= 0:
        return None

    multiplier = spike_vol / avg_vol
    change_pct = ((close_price - open_price) / open_price) * 100.0 if open_price > 0 else 0.0

    # Eşik kontrolü
    if multiplier < SETTINGS.VOLUME_SPIKE_MULTIPLIER:
        return None
    if abs(change_pct) < SETTINGS.VOLUME_SPIKE_MIN_CHANGE_PCT:
        return None

    # Cooldown kontrolü & Tekrar Takibi
    now_ts = time.time()
    alerts = _load_spike_alerts()
    prev_entry = alerts.get(symbol, {})

    if prev_entry:
        # Aynı bar_time'da zaten uyarı gönderilmişse tekrarlama
        if str(prev_entry.get("bar_time", "")) == str(bar_time):
            return None
        last_bar_time = prev_entry.get("bar_time")
        cooldown_sec = SETTINGS.VOLUME_SPIKE_COOLDOWN_BARS * period_s
        try:
            last_ts = pd.Timestamp(last_bar_time)
            curr_ts = pd.Timestamp(bar_time)
            if (curr_ts - last_ts).total_seconds() < cooldown_sec:
                return None
        except Exception:
            pass

    # Tekrar Takibi: Son 1 saat (3600 sn) içindeki spike'lar
    raw_hist = prev_entry.get("history_1h", []) if isinstance(prev_entry, dict) else []
    history_1h = [t for t in raw_hist if (now_ts - t) <= 3600]
    alert_count_1h = len(history_1h) + 1
    last_alert_ago_min = int((now_ts - history_1h[-1]) / 60) if history_1h else None
    history_1h.append(now_ts)

    # State güncelle
    alerts[symbol] = {
        "bar_index": len(df) - 1,
        "bar_time": str(bar_time),
        "multiplier": multiplier,
        "alerted_at": now_ts,
        "history_1h": history_1h,
        "alert_count_1h": alert_count_1h,
    }
    _save_spike_alerts(alerts)

    # Fiyat ve USD hesaplamaları
    mark_price = (mark_price_map.get(symbol) if mark_price_map else None) or close_price
    total_usd = spike_vol * mark_price
    avg_usd = avg_vol * mark_price
    direction = 1 if change_pct > 0 else -1

    # 1. Taker Buy / Sell Ayrımı (Hacim Kimin?)
    has_taker = False
    taker_buy = None
    taker_sell = None
    buy_ratio = None
    buy_usd = None
    sell_usd = None
    aggressor_side = "N/A"
    absorption_note = ""

    if "taker_buy" in last and last["taker_buy"] is not None:
        try:
            tb = float(last["taker_buy"])
            if tb >= 0:
                has_taker = True
                taker_buy = tb
                taker_sell = max(0.0, spike_vol - taker_buy)
                buy_ratio = taker_buy / spike_vol if spike_vol > 0 else 0.5
                buy_usd = taker_buy * mark_price
                sell_usd = taker_sell * mark_price

                if buy_ratio >= 0.58:
                    aggressor_side = "BUYER"  # Alıcı Baskın
                elif buy_ratio <= 0.42:
                    aggressor_side = "SELLER"  # Satıcı Baskın
                else:
                    aggressor_side = "BALANCED"  # Dengeli

                # Absorpsiyon Tespiti
                if change_pct > 1.0 and buy_ratio < 0.44:
                    absorption_note = "💎 Absorpsiyon: Fiyat yükseldi ancak satıcı piyasa emirleri baskındı (pasif alıcı emdi)."
                elif change_pct < -1.0 and buy_ratio > 0.56:
                    absorption_note = "💎 Absorpsiyon: Fiyat düştü ancak alıcı piyasa emirleri baskındı (pasif satıcı emdi)."
        except Exception as e:
            log.debug(f"Taker buy ayrıştırma hatası ({symbol}): {e}")

    # 2. Open Interest (OI) Analizi
    has_oi = False
    oi_pct = None
    oi_diff_usd = None
    oi_event = "N/A"
    oi_note = ""
    oi_noise_pct = float(getattr(SETTINGS, "OI_EVENT_NOISE_PCT", 0.03))

    try:
        raw_oi = get_open_interest_history(symbol, period=interval, limit=2)
        if raw_oi and len(raw_oi) >= 2:
            has_oi = True
            oi_prev_c = float(raw_oi[-2].get("sumOpenInterest", 0.0))
            oi_last_c = float(raw_oi[-1].get("sumOpenInterest", 0.0))
            oi_prev_usd = float(raw_oi[-2].get("sumOpenInterestValue", 0.0))
            oi_last_usd = float(raw_oi[-1].get("sumOpenInterestValue", 0.0))

            if oi_prev_c > 0:
                oi_pct = ((oi_last_c - oi_prev_c) / oi_prev_c) * 100.0
            oi_diff_usd = oi_last_usd - oi_prev_usd

            if oi_pct is not None:
                if abs(oi_pct) < oi_noise_pct:
                    oi_event = "NOISE"
                    oi_note = f"OI değişimi gürültü filtresi (±%{oi_noise_pct:g}) altında (%{oi_pct:+.3f}); pozisyon etkisi nötr."
                elif oi_pct >= oi_noise_pct:
                    if change_pct > 0:
                        oi_event = "LONG_BUILDUP"
                        oi_note = f"Fiyat↑ + OI↑ (+%{abs(oi_pct):.2f}) → Taze long girişi; trend güçlenerek devam edebilir."
                    else:
                        oi_event = "SHORT_BUILDUP"
                        oi_note = f"Fiyat↓ + OI↑ (+%{abs(oi_pct):.2f}) → Taze short girişi; satış baskısı güçleniyor."
                else:
                    if change_pct > 0:
                        oi_event = "SHORT_SQUEEZE"
                        oi_note = f"Fiyat↑ + OI↓ (-%{abs(oi_pct):.2f}) → Short tasfiyesi/kapanışı; yükseliş tükenme riski taşır."
                    else:
                        oi_event = "LONG_LIQUIDATION"
                        oi_note = f"Fiyat↓ + OI↓ (-%{abs(oi_pct):.2f}) → Long kapitülasyonu/tasfiyesi; panik satışları baskın."
    except Exception as e:
        log.debug(f"OI analizi hatası ({symbol}): {e}")

    # 3. Destek ve Direnç Seviyeleri (Son lookback mum, son bar hariç)
    sup_level = float(df["low"].iloc[-(lookback + 1):-1].min())
    res_level = float(df["high"].iloc[-(lookback + 1):-1].max())

    breakout_type = "INSIDE"
    if close_price > res_level:
        breakout_type = "BREAKOUT"  # Direnç kırılımı
    elif close_price < sup_level:
        breakout_type = "BREAKDOWN"  # Destek kırılımı

    return {
        "symbol": symbol,
        "multiplier": multiplier,
        "avg_volume": avg_vol,
        "spike_volume": spike_vol,
        "change_pct": change_pct,
        "open_price": open_price,
        "close_price": close_price,
        "high_price": high_price,
        "low_price": low_price,
        "mark_price": mark_price,
        "bar_time": str(bar_time),
        "direction": direction,
        "total_usd": total_usd,
        "avg_usd": avg_usd,
        "lookback": lookback,
        "lookback_mins": lookback_mins,
        "interval": interval,
        # Taker akış
        "has_taker": has_taker,
        "taker_buy": taker_buy,
        "taker_sell": taker_sell,
        "buy_ratio": buy_ratio,
        "buy_usd": buy_usd,
        "sell_usd": sell_usd,
        "aggressor_side": aggressor_side,
        "absorption_note": absorption_note,
        # OI
        "has_oi": has_oi,
        "oi_pct": oi_pct,
        "oi_diff_usd": oi_diff_usd,
        "oi_event": oi_event,
        "oi_note": oi_note,
        # Seviyeler
        "support_level": sup_level,
        "resistance_level": res_level,
        "breakout_type": breakout_type,
        # Tekrar takibi
        "alert_count_1h": alert_count_1h,
        "last_alert_ago_min": last_alert_ago_min,
    }


def fmt_volume_spike(info):
    """Hacim patlaması bildirimini zenginleştirilmiş Telegram formatına çevir."""
    symbol = info["symbol"]
    multiplier = info["multiplier"]
    avg_vol = info["avg_volume"]
    spike_vol = info["spike_volume"]
    change_pct = info["change_pct"]
    open_price = info["open_price"]
    close_price = info["close_price"]
    high_price = info.get("high_price", close_price)
    low_price = info.get("low_price", open_price)
    bar_time = info["bar_time"]
    direction = info.get("direction", 1 if change_pct > 0 else -1)
    interval = info.get("interval", getattr(SETTINGS, "INTERVAL", "5m"))

    total_usd = info.get("total_usd")
    avg_usd = info.get("avg_usd")
    lookback = info.get("lookback", 20)
    lookback_mins = info.get("lookback_mins", 100)

    # 1. Yön ikonu (Düzeltildi: Emoji ve metin artık tam uyumlu)
    dir_icon = "🟢" if change_pct > 0 else "🔴"
    trend_icon = "📈" if change_pct > 0 else "📉"
    dir_label = "Yükseliş" if change_pct > 0 else "Düşüş"

    # 2. Büyüklük etiketi (Skala şeffaf)
    if multiplier >= 50:
        size_label = "🔥🔥🔥 MEGA"
    elif multiplier >= 20:
        size_label = "🔥🔥 BÜYÜK"
    elif multiplier >= 10:
        size_label = "🔥 ANORMAL"
    else:
        size_label = "⚡ STANDART"

    # 3. Taker Akış Ayrımı
    has_taker = info.get("has_taker", False)
    taker_str = ""
    if has_taker:
        buy_usd = info.get("buy_usd")
        sell_usd = info.get("sell_usd")
        buy_ratio = info.get("buy_ratio", 0.5)
        agg_side = info.get("aggressor_side", "N/A")
        if agg_side == "BUYER":
            agg_badge = f"🟢 Alıcı Baskın (Taker Alış: %{buy_ratio * 100:.1f})"
        elif agg_side == "SELLER":
            agg_badge = f"🔴 Satıcı Baskın (Taker Satış: %{(1.0 - buy_ratio) * 100:.1f})"
        else:
            agg_badge = f"⚖️ Dengeli Akış (Alış: %{buy_ratio * 100:.1f} / Satış: %{(1.0 - buy_ratio) * 100:.1f})"

        taker_str = (
            f"\n🔄 Taker Akışı: {agg_badge}\n"
            f"   ↳ Hacim Dağılımı: {_fmt_usd(buy_usd)} Alıcı / {_fmt_usd(sell_usd)} Satıcı"
        )
        absorption_note = info.get("absorption_note")
        if absorption_note:
            taker_str += f"\n   ↳ {absorption_note}"

    # 4. Open Interest (OI)
    has_oi = info.get("has_oi", False)
    oi_pct = info.get("oi_pct")
    oi_diff_usd = info.get("oi_diff_usd")
    oi_note = info.get("oi_note", "")
    oi_str = ""
    if has_oi and oi_pct is not None:
        c_sign = "+" if oi_pct >= 0 else "-"
        usd_part = f" ≈ {c_sign}{_fmt_usd(abs(oi_diff_usd))}" if oi_diff_usd is not None else ""
        oi_str = f"\n🔍 OI Değişimi: {c_sign}%{abs(oi_pct):.2f}{usd_part} [Gürültü filtresi: ±%{getattr(SETTINGS, 'OI_EVENT_NOISE_PCT', 0.03):g}]\n   ↳ {oi_note}"

    # 5. Destek & Direnç Seviyeleri
    sup_level = info.get("support_level")
    res_level = info.get("resistance_level")
    breakout_type = info.get("breakout_type", "INSIDE")

    if breakout_type == "BREAKOUT":
        structure_note = f"🚀 DİRENÇ KIRILIMI — Fiyat son {lookback} periyodun tavanını (${_fmt_price(res_level)}) yukarı kırdı!"
    elif breakout_type == "BREAKDOWN":
        structure_note = f"⚠️ DESTEK KIRILIMI — Fiyat son {lookback} periyodun tabanını (${_fmt_price(sup_level)}) aşağı kırdı!"
    else:
        structure_note = f"Range Hareketi (Destek: ${_fmt_price(sup_level)} | Direnç: ${_fmt_price(res_level)})"

    # 6. Tekrar Takibi (Son 1 saat)
    alert_count_1h = info.get("alert_count_1h", 1)
    last_alert_ago_min = info.get("last_alert_ago_min")
    if alert_count_1h > 1 and last_alert_ago_min is not None:
        repeat_str = f"🕒 Tekrar Takibi: Bu coin için son 1 saatte {alert_count_1h}. hacim patlaması ({last_alert_ago_min} dk önce de görüldü)"
    else:
        repeat_str = "🕒 Tekrar Takibi: Son 1 saatteki ilk hacim patlaması"

    # 7. Teyit ve Geçersizleşme Kriterleri
    if change_pct > 0:
        if breakout_type == "BREAKOUT":
            confirm_text = f"Kırılan direncin (${_fmt_price(res_level)}) üzerinde saatlik tutunma + alım akışının sürmesi"
            invalidate_text = f"Fiyatın tekrar ${_fmt_price(res_level)} altına sarkması veya spike dibinin (${_fmt_price(low_price)}) kırılması (sahte kırılım)"
        else:
            confirm_text = f"Direnç seviyesine (${_fmt_price(res_level)}) doğru momentumun sürmesi + alıcı üstünlüğü"
            invalidate_text = f"Spike mumunun dip seviyesinin (${_fmt_price(low_price)}) altına sarkılması"
    else:
        if breakout_type == "BREAKDOWN":
            confirm_text = f"Kırılan desteğin (${_fmt_price(sup_level)}) altında kalıcılık + satış akışının sürmesi"
            invalidate_text = f"Fiyatın tekrar ${_fmt_price(sup_level)} üzerine çıkması veya spike tepesinin (${_fmt_price(high_price)}) aşılması (ayı tuzağı)"
        else:
            confirm_text = f"Destek seviyesine (${_fmt_price(sup_level)}) doğru satış baskısının sürmesi"
            invalidate_text = f"Spike mumunun tepe seviyesinin (${_fmt_price(high_price)}) aşılması"

    # 8. USD ve Coin Hacim Satırı
    usd_line = ""
    if total_usd is not None and avg_usd is not None:
        usd_line = f"💵 Hacim Tutarı: ~{_fmt_usd(total_usd)} (Normal periyot ortalama: ~{_fmt_usd(avg_usd)})\n"

    return (
        f"<b>🚨 HACİM PATLAMASI {dir_icon} | {html.escape(str(symbol), quote=False)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{usd_line}"
        f"📊 Coin Adedi: {_fmt_volume(spike_vol)} (Normal ort: {_fmt_volume(avg_vol)} | son {lookback} periyot / {lookback_mins}dk bazında → {multiplier:.1f}x)\n"
        f"{trend_icon} Fiyat Hareketi: {dir_icon} {dir_label} ({change_pct:+.2f}%) [{_fmt_price(open_price)} → {_fmt_price(close_price)}]\n"
        f"🛡️ Seviyeler (Son {lookback} Mum): Taban: ${_fmt_price(sup_level)} | Tavan: ${_fmt_price(res_level)}\n"
        f"   ↳ {structure_note}"
        f"{taker_str}"
        f"{oi_str}\n"
        f"🏷️ Büyüklük: {size_label} ({multiplier:.1f}x) [Skala: 5x+ Standart | 10x+ Anormal | 20x+ Büyük | 50x+ Mega]\n"
        f"⏰ {bar_time} ({interval})\n"
        f"{repeat_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Teyit: {confirm_text}\n"
        f"❌ Geçersizleşme: {invalidate_text}"
    )
