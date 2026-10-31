"""
Whale Alert — Büyük Emir / Taker Akışı Radarı

Binance Futures "takerlongshortRatio" (5m) verisiyle agresif alıcı/satıcı
dengesini tarar. Ani taker buy/sell sapmaları balina girişini gösterir.

Volume spike'tan farkı: hacmin MİKTARI değil, hacmin YÖNÜ/BASKISI
(alıcı mı satıcı mı agresif) ölçülür.

Tespit mantığı:
  1. Sembolün son N periyotluk taker buy/sell volume oranlarını çek
  2. Son kapanmış periyodun taker buy/sell hacim baskısını hesapla:
       buy_ratio  = buyVol / (buyVol + sellVol)
  3. buy_ratio baseline'a (önceki N periyot MEDYANI) göre
     yeterince saptıysa VEYA hacim spike'ı ≥ 3x ise → tetikle
  4. Yön: son barda baskın agresif taraf (buy_ratio > 0.5 → ALICI, < 0.5 → SATICI).
          Sapma tetiklediyse işareti baskın tarafla aynı olmalı; değilse
          (örn. %70 → %58) baskı sönümleniyor demektir, alarm üretilmez.
  5. buyVol/sellVol coin adedi cinsinden gelir; mark price ile çarpılarak
     USD'ye çevrilir (WHALE_ALERT_MIN_VOLUME USDT eşiği doğru karşılaştırılır).
  6. Open Interest (OI) ile OLASI pozisyon olayı sınıflandırılır. OI değişimi
     KONTRAT ADEDİ (fiyat etkisinden bağımsız) üzerinden ölçülür:
       Taker ALICI + OI↑ → "OLASI LONG AÇILIŞI"
       Taker SATICI + OI↑ → "OLASI SHORT AÇILIŞI"
       Taker ALICI + OI↓ → "OLASI SHORT KAPANIŞI"  (squeeze potansiyeli)
       Taker SATICI + OI↓ → "OLASI LONG KAPANIŞI"
     Bunlar kesin tespit değil, olasılıksal yorumdur (her sözleşmenin karşı
     tarafı vardır). USD-OI fiyat hareketinden etkilenir; gerçek para girişi
     sayılmaz. Agresif taker hacmi açılan pozisyon büyüklüğü değildir.
  7. Kaldıraç & likidasyon: Binance bireysel pozisyon kaldıracını GÖSTERMEZ.
     Bu yüzden mesaj, pozisyon büyüklüğüne göre Binance USDT-M marj
     kademelerini kullanan "yaklaşık senaryo" içerir (uydurma değer yok;
     sadece matematiksel senaryo — açıkça etiketlenir).
  8. Opsiyonel: fiyat etkisi (absorpsiyon), CVD uyumsuzluğu (stealth
     accumulation) ve kalıcılık kontrolü.

Sinyal akışından (sweep + skorlama) tamamen bağımsız çalışır.
Sinyal üretmez, sadece "balina aktivitesi" uyarısı verir.
"""
import html
import json
import math
import os
import threading
import time

import pandas as pd

from config import SETTINGS
from exchange import get_open_interest_history
from exchange import get_all_premium_index, get_premium_index, get_raw_klines, get_taker_long_short_ratio
from logger import get_logger

log = get_logger("whale_alert")



# ---------------------------------------------------------------------------
# Yardımcı: Biçimlendirme
# ---------------------------------------------------------------------------

def _fmt_volume(v):
    """Hacmi okunabilir formata çevir: 1234 → 1.23K, 1234567 → 1.23M"""
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    elif v >= 1_000:
        return f"{v / 1_000:.2f}K"
    return f"{v:.0f}"


def _fmt_usd(v):
    """USD tutarını okunabilir formata çevir: 1234567 → $1.23M"""
    return f"${_fmt_volume(v)}"


def _fmt_price(p):
    """Fiyatı coin'in büyüklüğüne göre uyarlanmış hassasiyetle biçimle."""
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.3f}"
    return f"{p:.6f}"


# ---------------------------------------------------------------------------
# Yardımcı: Kaldıraç / Likidasyon Senaryosu
# ---------------------------------------------------------------------------

def _mmr_for_notional(usd):
    """Pozisyon büyüklüğüne göre yaklaşık Bakım Marjı Oranı (MMR).

    Binance USDT-M BTC/ETH kaldıraç kademelerinin sadeleştirilmiş hâli.
    Gerçek borsa değerleri sembole göre değişir — mesajda zaten
    "yaklaşık senaryo" olarak etiketlenir.

    Returns:
        float: MMR (0.004 = %0.4)
    """
    if usd < 50_000:
        return 0.004
    if usd < 250_000:
        return 0.005
    if usd < 1_000_000:
        return 0.010
    if usd < 10_000_000:
        return 0.025
    if usd < 20_000_000:
        return 0.050
    return 0.10


def _liq_scenarios(price, direction, notional_usd):
    """Kaldıraç kademeleri için yaklaşık likidasyon senaryosu hesaplar.

    Binance gerçek pozisyon kaldıracını göstermediği için bu bir
    MATEMATİKSEL SENARYODUR (izole marj varsayımı):
        mesafe ≈ 1/kaldıraç - MMR(notional)

    Args:
        price:         Giriş (mark) fiyatı
        direction:     1=long (likidasyon aşağıda), -1=short (yukarıda)
        notional_usd:  Pozisyon büyüklüğü (USD) → MMR kademesi

    Returns:
        list[dict]: [{"leverage", "liq_price", "distance_pct", "impossible"}]
    """
    mmr = _mmr_for_notional(notional_usd)
    scenarios = []
    try:
        tiers = tuple(SETTINGS.WHALE_ALERT_LEVERAGE_TIERS)
    except Exception:
        tiers = (10, 25, 50)
    for lev in tiers:
        try:
            lev_f = float(lev)
            dist = (1.0 / lev_f) - mmr
            impossible = dist <= 0
            if impossible:
                dist = 0.0
                liq_price = price  # marj yetersiz — pratikte açılamaz
            elif direction > 0:
                liq_price = price * (1.0 - dist)
            else:
                liq_price = price * (1.0 + dist)
            scenarios.append({
                "leverage": int(lev_f),
                "liq_price": round(liq_price, 6),
                "distance_pct": round(dist * 100.0, 2),
                "impossible": impossible,
            })
        except Exception:
            continue
    return scenarios


# ---------------------------------------------------------------------------
# Yardımcı: Durum (State) Yönetimi
# ---------------------------------------------------------------------------

_ALERTS_LOCK = threading.RLock()
_ALERTS_CACHE = {"mtime": None, "data": {}}


def _load_whale_alerts():
    """Cooldown takibi için whale alert geçmişini yükle.

    Dosya mtime'ı değişmediyse bellekteki kopya döner (sembol başına disk
    okuması yapılmaz). Dosya bozuksa uyarı verilir ve bellekteki son sağlam
    durum kullanılır — sessizce boş dict'e düşülmez (cooldown kaybolmasın).
    Dosya kullanıcı tarafından silindiyse durum sıfırlanır.

    Not: Kilit yalnız aynı süreç içindeki thread'leri korur; iki ayrı
    Python süreci aynı dosyayı yazıyorsa birbirinin güncellemesini ezebilir.
    """
    path = SETTINGS.WHALE_ALERT_FILE
    with _ALERTS_LOCK:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            if _ALERTS_CACHE["mtime"] is not None:
                # Dosya daha önce vardı, şimdi yok → bilinçli silinmiş say
                _ALERTS_CACHE["mtime"] = None
                _ALERTS_CACHE["data"] = {}
            return _ALERTS_CACHE["data"]
        if _ALERTS_CACHE["mtime"] == mtime:
            return _ALERTS_CACHE["data"]
        try:
            with open(path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("beklenen biçim dict değil")
        except Exception as e:
            log.warning(f"Whale alert dosyası okunamadı ({path}): {e} — bellekteki durum kullanılıyor")
            return _ALERTS_CACHE["data"]
        _ALERTS_CACHE["mtime"] = mtime
        _ALERTS_CACHE["data"] = data
        return data


def _save_whale_alerts(data):
    """Whale alert geçmişini diske yaz (Windows-safe).

    Bellekteki durum HER ZAMAN güncellenir; disk yazımı başarısız olsa bile
    aynı süreç içinde cooldown çalışmaya devam eder.
    """
    path = SETTINGS.WHALE_ALERT_FILE
    with _ALERTS_LOCK:
        _ALERTS_CACHE["data"] = data
        snapshot = dict(data)
        tmp = path + ".tmp"
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(snapshot, f, default=str)
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except (PermissionError, OSError):
                    if attempt < 4:
                        time.sleep(0.05 * (attempt + 1))
                    else:
                        try:
                            import shutil
                            shutil.copy2(tmp, path)
                            if os.path.exists(tmp):
                                os.remove(tmp)
                        except Exception:
                            pass
            try:
                _ALERTS_CACHE["mtime"] = os.path.getmtime(path)
            except OSError:
                pass
        except Exception as e:
            log.warning(f"Whale alert kaydetme uyarısı ({path}): {e}")


def _append_alert_history(info):
    """Her alarmı JSONL geçmişine ekler (sonradan geriye dönük test/kalibrasyon için).

    Skor ağırlıklarının gerçek isabetini ölçmenin tek yolu, alarm anındaki
    tüm bileşenleri ve sonraki fiyat hareketini birlikte görebilmektir.
    Varsayılan dosya: WHALE_ALERT_FILE'ın yanında *_history.jsonl.
    Kapatmak için SETTINGS.WHALE_ALERT_HISTORY_ENABLED = False.
    """
    if not getattr(SETTINGS, "WHALE_ALERT_HISTORY_ENABLED", True):
        return
    try:
        path = getattr(SETTINGS, "WHALE_ALERT_HISTORY_FILE", None)
        if not path:
            root, _ = os.path.splitext(SETTINGS.WHALE_ALERT_FILE)
            path = root + "_history.jsonl"
        rec = {k: v for k, v in info.items() if k != "liq_scenarios"}
        rec["logged_at"] = time.time()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with _ALERTS_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        log.debug(f"Whale alert geçmiş logu yazılamadı: {e}")


# ---------------------------------------------------------------------------
# Yardımcı: Veri Çekme
# ---------------------------------------------------------------------------

def _fetch_taker_ratios(symbol, interval, limit):
    """Binance takerlongshortRatio endpoint'inden ham veri çeker.

    Returns:
        list[dict] | None: [{"buyVol","sellVol","buySellRatio","timestamp"}, ...]
    """
    try:
        data = get_taker_long_short_ratio(symbol, interval, limit)
        if isinstance(data, list) and data:
            return data
    except Exception as e:
        log.debug(f"Taker ratio çekme hatası ({symbol}): {e}")
    return None


def load_mark_price_map():
    """Tüm semboller için mark price haritasını TEK bir API çağrısıyla çeker.

    main.py tarafından tur başında bir kez çağrılır; sonuç check_whale_alert'e
    mark_price_map parametresiyle geçirilir. Bu sayede 150 sembol için
    150 ayrı istek yerine tek bir istek yeterli olur.

    Returns:
        dict[str, float]: {"BTCUSDT": 65432.1, ...} veya {} (hata durumunda)
    """
    try:
        data = get_all_premium_index()
        if isinstance(data, list):
            return {x["symbol"]: float(x.get("markPrice", 0)) for x in data}
    except Exception as e:
        log.warning(f"Toplu mark price çekme hatası: {e}")
    return {}


def _fetch_mark_price_single(symbol):
    """Tek sembol için mark price fallback (map kullanılamadığında)."""
    try:
        data = get_premium_index(symbol)
        if isinstance(data, dict):
            return float(data.get("markPrice", 0.0))
    except Exception as e:
        log.debug(f"Mark price çekme hatası ({symbol}): {e}")
    return 0.0


def _classify_position_event(direction, oi_pct, oi_notional_usd,
                             oi_aligned=True, price_neutral=True):
    """Taker yönü + OI değişiminden OLASI pozisyon olayını ve veri-uyum güvenini döndürür.

    Binance pozisyon bazında veri yayınlamadığı için bu, iki ölçümün
    (agresif taker akışı + net OI değişimi) kesişiminden üretilen olasılıksal
    yorumdur. Vadeli işlemde her sözleşmenin karşı tarafı vardır; agresif
    alıcı yeni long açıyor da olabilir, short kapatıyor da olabilir.

        ALICI baskı + OI↑ → probable_long_opened
        SATICI baskı + OI↑ → probable_short_opened
        ALICI baskı + OI↓ → probable_short_closed  (squeeze riski)
        SATICI baskı + OI↓ → probable_long_closed
        OI yok / gürültü   → "flow"

    OI değişimi KONTRAT ADEDİ üzerinden ölçülür (fiyat etkisinden bağımsız).
    USD-OI fiyat hareketiyle de değiştiği için yalnızca kontrat verisi yoksa
    yedek olarak kullanılır (price_neutral=False) ve güven en fazla LOW olur.

    Güven, sembol büyüklüğüne bağımlı olmasın diye hem OI'nin kendi
    yüzdesine hem de mutlak notional karşılığına bakar. Eşikler
    SETTINGS'ten ayarlanabilir (yoksa varsayılanlar kullanılır).

    Args:
        direction:        1 / -1 (agresif taker yönü)
        oi_pct:           OI değişimi, yüzde (None = veri yok)
        oi_notional_usd:  Δkontrat × mark price (fiyat etkisinden arındırılmış)
        oi_aligned:       OI zaman damgası taker barıyla uyumlu mu?
        price_neutral:    oi_pct kontrat bazlı mı (True) yoksa USD bazlı mı (False)

    Returns:
        tuple[str, str, int]: (event_key, "HIGH"|"MEDIUM"|"LOW", güven skoru 0-100)
    """
    if oi_pct is None:
        return "flow", "LOW", 20

    noise = float(getattr(SETTINGS, "OI_EVENT_NOISE_PCT", 0.03))
    if abs(oi_pct) < noise:
        return "flow", "LOW", 20  # OI değişimi gürültü seviyesinde → olay etiketi yok

    pct_hi = float(getattr(SETTINGS, "WHALE_ALERT_OI_PCT_HIGH", 0.50))
    pct_med = float(getattr(SETTINGS, "WHALE_ALERT_OI_PCT_MED", 0.15))
    usd_hi = float(getattr(SETTINGS, "WHALE_ALERT_OI_USD_HIGH", 250_000))
    usd_med = float(getattr(SETTINGS, "WHALE_ALERT_OI_USD_MED", 50_000))

    pct_abs = abs(oi_pct)
    usd_abs = abs(oi_notional_usd or 0.0)

    if pct_abs >= pct_hi and usd_abs >= usd_hi:
        level = 2
    elif pct_abs >= pct_med and usd_abs >= usd_med:
        level = 1
    else:
        level = 0

    if not oi_aligned and level > 0:
        level -= 1  # zaman damgaları uyuşmuyorsa bir kademe düşür
    if not price_neutral:
        level = 0   # USD-OI fiyat etkisi taşır → yüksek güven verilmez

    conf, conf_score = {2: ("HIGH", 80), 1: ("MEDIUM", 55), 0: ("LOW", 30)}[level]

    oi_rising = oi_pct > 0
    if direction > 0:
        key = "probable_long_opened" if oi_rising else "probable_short_closed"
    else:
        key = "probable_short_opened" if oi_rising else "probable_long_closed"

    return key, conf, conf_score


# ---------------------------------------------------------------------------
# Yardımcı: Periyot → Saniye
# ---------------------------------------------------------------------------

def _period_seconds(period):
    """'5m', '15m', '1h' gibi periyot stringlerini saniyeye çevirir."""
    try:
        unit = period[-1]
        val = int(period[:-1])
        return val * {"m": 60, "h": 3600, "d": 86400}[unit]
    except Exception:
        return 300  # varsayılan 5 dakika


# ---------------------------------------------------------------------------
# Ana Fonksiyon
# ---------------------------------------------------------------------------

def _calc_deviation(raw_data):
    """Ham taker ratio listesinden (buyVol/sellVol) sapma değeri hesaplar.

    Multi-timeframe onayında tekrar kullanılmak üzere ayrı fonksiyon.

    Returns:
        float | None: deviation (>0 alıcı, <0 satıcı) veya veri yetersizse None
    """
    lookback = SETTINGS.WHALE_ALERT_LOOKBACK
    if not raw_data or len(raw_data) < lookback + 1:
        return None
    try:
        bvols = [float(r["buyVol"]) for r in raw_data]
        svols = [float(r["sellVol"]) for r in raw_data]
    except (KeyError, ValueError, TypeError):
        return None

    def _br(b, s):
        t = b + s
        return b / t if t > 0 else 0.5

    last_br = _br(bvols[-1], svols[-1])
    prev_ratios = [_br(bvols[i], svols[i]) for i in range(len(bvols) - 1 - lookback, len(bvols) - 1)]
    if not prev_ratios:
        return None
    baseline = float(pd.Series(prev_ratios).median())
    return last_br - baseline


# ---------------------------------------------------------------------------
# Yardımcı: Sembol Başına Sinyal Durumu (Signal State)
# ---------------------------------------------------------------------------

_SIGNAL_STATE_CACHE: dict = {}  # in-memory
_SIGNAL_STATE_WINDOW_SEC = 1800


def _resolve_symbol_state(symbol, direction, dev_15m):
    """Sembol başına sinyal durumunu takip eder; çelişen sinyalleri CONFLICTED işaretler.

    Ters yönde alarm gelince `flip_ts` kaydedilir. Bu andan itibaren
    _SIGNAL_STATE_WINDOW_SEC (30 dk) dolana kadar sembol CONFLICTED kalır —
    ardışık ikinci alarm aynı yöndeyse bile durum hemen temizlenmez.
    Son alarmdan bu yana 30 dk geçtiyse tüm geçmiş sıfırlanır.

    Cache alanları: bullish (son yön), ts (son alarm), flip_ts (son ters yön geçişi).

    Returns:
        str: "BULLISH_FLOW" | "BEARISH_FLOW" | "CONFLICTED" | "NEUTRAL"
    """
    now = time.time()

    # Cache birikimi: 2000 kayıt üstünde 1 saatten eski kayıtlar temizlenir.
    if len(_SIGNAL_STATE_CACHE) > 2000:
        for _k, _v in list(_SIGNAL_STATE_CACHE.items()):
            if now - _v.get("ts", 0) > 3600:
                del _SIGNAL_STATE_CACHE[_k]

    prev = _SIGNAL_STATE_CACHE.get(symbol)
    new_bullish = direction > 0

    if prev and now - prev.get("ts", 0) > _SIGNAL_STATE_WINDOW_SEC:
        prev = None  # 30 dakikadan eskiyse geçmişi sıfırla

    flip_ts = None
    if prev:
        flip_ts = prev.get("flip_ts")
        if new_bullish != prev.get("bullish", new_bullish):
            flip_ts = now  # ters yön geçişi

    if flip_ts and now - flip_ts < _SIGNAL_STATE_WINDOW_SEC:
        state = "CONFLICTED"
    else:
        flip_ts = None
        state = "BULLISH_FLOW" if new_bullish else "BEARISH_FLOW"

    _SIGNAL_STATE_CACHE[symbol] = {
        "state": state,
        "bullish": new_bullish,
        "ts": now,
        "flip_ts": flip_ts,
    }
    return state


# ---------------------------------------------------------------------------
# Yardımcı: Whale Score (İki Boyutlu Puanlama Sistemi)
# ---------------------------------------------------------------------------

def _calc_whale_score(
    buy_ratio, vol_spike, dominant_usd,
    oi_confidence_score, tf_state, quality_label, signal_state,
):
    """Whale sinyalinin toplam gücünü iki boyutlu olarak skorlar.

    İki ayrı boyut hesaplanır — bu sayede yüksek hacimli ama yönü belirsiz
    olaylar da değerli alarm olarak kalır; zorla long/short etiketi almaz.

    Akış gücü üç BAĞIMSIZ bileşenden oluşur:
      1. Taker payı uç değeri   → yön dengesizliğinin şiddeti
      2. Hacim spike'ı          → sembolün kendi geçmişine göre anomali
      3. Mutlak USD büyüklük    → dolar bazında anlamlı mı? (MIN_VOLUME'a göre log ölçek)
    (Eski sürümde 1. ve 3. bileşen matematiksel olarak aynı büyüklüktü:
     |buy-sell|/(buy+sell) = |2*buy_ratio-1|. Bu yüzden gerçekte tek bir
     bilgi iki kez ödüllendiriliyordu; 3. bileşen artık mutlak büyüklüğü ölçer.)

    tf_state: "CONFIRMED" → 30 puan; "UNAVAILABLE"/"OFF" → 0 puan
    (eksik veri olumlu kanıt sayılmaz).

    Ağırlıklar başlangıç taslağıdır; geçmiş veriyle kalibre edilmemiştir.
    Kompozit puan bir başarı olasılığı DEĞİLDİR. Kalibrasyon için alarm
    geçmişi _append_alert_history ile JSONL'e kaydedilir.

    Returns:
        dict: flow_strength, direction_conf, composite, label, breakdown
    """
    breakdown = {}

    # --- A. Akış Gücü (flow_strength) ---
    # 1. Taker Buy Payı uç değeri (0.5→0 puan, 1.0→100 puan)
    taker_extreme = max(buy_ratio, 1.0 - buy_ratio)
    taker_score = min(100, int((taker_extreme - 0.5) * 200))
    breakdown["taker_extreme"] = taker_score

    # 2. Hacim spike anomalisi (1x→0, 3x→40, 6x→100)
    spike_score = min(100, max(0, int((vol_spike - 1.0) * 20)))
    breakdown["vol_spike"] = spike_score

    # 3. Mutlak büyüklük: baskın taraf USD hacmi / asgari hacim eşiği
    #    (1x→0, 10x→100; log ölçek)
    try:
        min_vol = float(getattr(SETTINGS, "WHALE_ALERT_MIN_VOLUME", 0) or 0)
    except (TypeError, ValueError):
        min_vol = 0.0
    if min_vol > 0 and dominant_usd > min_vol:
        size_score = min(100, int(math.log10(dominant_usd / min_vol) * 100))
    else:
        size_score = 0
    breakdown["size_usd"] = size_score

    flow_strength = int(taker_score * 0.40 + spike_score * 0.35 + size_score * 0.25)

    # --- B. Yön Güveni (direction_conf) ---
    # 1. OI veri-uyum skoru
    oi_s = min(100, oi_confidence_score)
    breakdown["oi_conf"] = oi_s

    # 2. 15m teyidi — yalnızca gerçekten teyit edildiyse puan
    tf_score = 30 if tf_state == "CONFIRMED" else 0
    breakdown["tf_score"] = tf_score

    # 3. Kalite etiketi
    quality_score = {
        "absorption": 25, "stealth": 20, "persistent": 15,
        "absorption_weak": 10, "spike": 5,
    }.get(quality_label, 0)
    breakdown["quality"] = quality_score

    # 4. Signal state (CONFLICTED → ceza)
    state_bonus = {
        "BULLISH_FLOW": 15, "BEARISH_FLOW": 15,
        "BULLISH_FLOW_ABSORBED": 15, "BEARISH_FLOW_ABSORBED": 15,
        "BULLISH_FLOW_ABSORB_WEAK": 10, "BEARISH_FLOW_ABSORB_WEAK": 10,
        "CONFLICTED": 0, "NEUTRAL": 5,
    }.get(signal_state, 5)
    state_penalty = -20 if signal_state == "CONFLICTED" else 0
    breakdown["state"] = state_bonus

    direction_conf = max(0, int(
        oi_s * 0.40
        + tf_score * 0.30
        + quality_score * 0.20
        + state_bonus * 0.10
    ) + state_penalty)

    # --- Kompozit (akış gücü biraz daha belirleyici) ---
    composite = min(100, int(flow_strength * 0.55 + direction_conf * 0.45))

    if composite >= 80:
        label = "ÇOK GÜÇLÜ"
    elif composite >= 60:
        label = "GÜÇLÜ"
    elif composite >= 40:
        label = "İZLEMEYE DEĞER"
    else:
        label = "DÜŞÜK"

    return {
        "flow_strength": flow_strength,
        "direction_conf": direction_conf,
        "composite": composite,
        "label": label,
        "breakdown": breakdown,
    }


def check_whale_alert(symbol, mark_price_map=None, multi_tf_confirm=True):
    """Bir sembol için agresif taker akışı (balina) sapması kontrolü yapar.

    Akış sırası (ucuzdan pahalıya; aday elendikçe sonraki API istekleri yapılmaz):
      taker verisi → tetikleyici → USD filtresi → yön → cooldown
      → 15m teyidi → kline (absorpsiyon/stealth) → OI → skor

    Önemli kurallar:
      - buyVol/sellVol coin cinsinden gelir → mark price ile USD'ye çevrilir
      - YÖN = son barda baskın agresif taraf (buy_ratio > 0.5 → ALICI).
        Hacim spike'ı tek başına tetiklediyse yön yine buy_ratio'dan gelir.
        Sapma tetiklediyse sapmanın işareti baskın tarafla aynı olmalı;
        değilse (örn. %70 → %58: alıcılar hâlâ baskın ama zayıflıyor) bu bir
        balina girişi değil baskı sönümlenmesidir → alarm üretilmez.
      - Spike tetikleyicisi: ort. bazlı ≥3x VE son bar, geçmiş pencerenin
        üst yüzdelik dilimlerinde (WHALE_ALERT_SPIKE_MIN_RANK, vars. 0.90).
      - Kline'da yalnızca KAPANMIŞ mum kullanılır (son eleman açık mum olabilir).
      - 15m verisi yoksa teyit puanı verilmez (tf_state="UNAVAILABLE").
      - OI kontrat adedi (fiyat etkisinden bağımsız) üzerinden yorumlanır.
      - est_position_usd = baskın taraf agresif taker hacmi; AÇILAN POZİSYON
        BÜYÜKLÜĞÜ DEĞİLDİR (alan adı geriye uyumluluk için korundu;
        aynı değer aggressive_taker_usd olarak da döner).

    Args:
        symbol:          Sembol adı (örn: "BTCUSDT")
        mark_price_map:  {symbol: price} haritası (main.py'den toplu çekilir).
        multi_tf_confirm: True ise 15m'de de aynı yön zorunlu.

    Returns:
        dict | None: Saptama varsa bilgi dict'i, yoksa None. Başlıca alanlar:
            symbol, direction (1/-1), aggressor_side ("BUY"/"SELL"),
            pressure_trend ("STRENGTHENING"|"FLAT_OR_FADING"),
            buy_ratio, baseline_ratio, deviation, buy/sell_volume(_usd),
            vol_spike (ort.), vol_spike_med (medyan), vol_rank,
            persistent, absorption, absorption_strength ("strong"|"weak"|""),
            absorbed_by ("passive_sellers"|"passive_buyers"|""),
            stealth_accumulation, stealth_distribution, quality_label,
            tf_state ("CONFIRMED"|"UNAVAILABLE"|"OFF"), tf_confirmed (bool),
            position_event, oi_confidence, oi_change_contracts_pct,
            oi_notional_usd (Δkontrat×fiyat), oi_change_usd (USD-OI Δ, fiyat etkili),
            oi_aligned, data_flags (eksik/uyumsuz veri listesi),
            bar_diag (zaman hizalama teşhisi), signal_state, whale_score, ...
    """
    lookback = SETTINGS.WHALE_ALERT_LOOKBACK
    interval = SETTINGS.WHALE_ALERT_PERIOD
    period_s = _period_seconds(interval)

    raw = _fetch_taker_ratios(symbol, interval, lookback + 1)
    if not raw or len(raw) < lookback + 1:
        return None

    try:
        buy_vols = [float(r["buyVol"]) for r in raw]
        sell_vols = [float(r["sellVol"]) for r in raw]
        bar_time = str(raw[-1].get("timestamp", ""))
        bar_ts_ms = int(raw[-1].get("timestamp", 0) or 0)
    except (KeyError, ValueError, TypeError):
        return None

    # --- 1. USD Dönüşümü ---
    # buyVol/sellVol base asset (coin adedi) cinsinden gelir, USD değil.
    if mark_price_map:
        mark_price = mark_price_map.get(symbol, 0.0)
    else:
        mark_price = _fetch_mark_price_single(symbol)
    if mark_price <= 0:
        log.debug(f"Mark price alınamadı ({symbol}), whale alert atlandı.")
        return None

    last_buy, last_sell = buy_vols[-1], sell_vols[-1]
    buy_usd = last_buy * mark_price
    sell_usd = last_sell * mark_price

    # --- 2. Taker Buy Payı Hesabı ---
    def _buy_ratio(b, s):
        total = b + s
        return b / total if total > 0 else 0.5

    buy_ratio = _buy_ratio(last_buy, last_sell)

    # Baseline: son periyot HARİÇ önceki N periyodun MEDYANI
    prev_ratios = [
        _buy_ratio(buy_vols[i], sell_vols[i])
        for i in range(len(buy_vols) - 1 - lookback, len(buy_vols) - 1)
    ]
    baseline = float(pd.Series(prev_ratios).median()) if prev_ratios else 0.5

    deviation = buy_ratio - baseline  # baseline'a göre GÖRELİ değişim

    # --- 3. Hacim Spike Hesabı ---
    prev_totals = [
        buy_vols[i] + sell_vols[i]
        for i in range(len(buy_vols) - 1 - lookback, len(buy_vols) - 1)
    ]
    avg_total = sum(prev_totals) / len(prev_totals) if prev_totals else 0
    med_total = float(pd.Series(prev_totals).median()) if prev_totals else 0.0
    current_total = last_buy + last_sell
    vol_spike = (current_total / avg_total) if avg_total > 0 else 0.0
    vol_spike_med = (current_total / med_total) if med_total > 0 else 0.0
    # Son barın geçmiş penceredeki yüzdelik konumu (0-1): düşük hacimli
    # sembollerde küçük akışın sahte 3x üretmesini süzer.
    vol_rank = (
        sum(1 for t in prev_totals if t < current_total) / len(prev_totals)
        if prev_totals else 0.0
    )

    # --- 4. Tetikleyici: Sapma VEYA (Hacim Spike ≥ 3x ve üst yüzdelik) ---
    deviation_ok = abs(deviation) >= SETTINGS.WHALE_ALERT_DEVIATION
    spike_min_rank = float(getattr(SETTINGS, "WHALE_ALERT_SPIKE_MIN_RANK", 0.90))
    spike_ok = vol_spike >= 3.0 and vol_rank >= spike_min_rank
    if not (deviation_ok or spike_ok):
        return None

    # --- 5. USD Hacim Filtresi ---
    if max(buy_usd, sell_usd) < SETTINGS.WHALE_ALERT_MIN_VOLUME:
        return None

    # --- 6. Buy Ratio Yönlülük Filtresi ---
    min_ratio = SETTINGS.WHALE_ALERT_MIN_BUY_RATIO
    if buy_ratio < min_ratio and buy_ratio > (1.0 - min_ratio):
        return None

    # --- 6b. Yön: baskın agresif taraf (mutlak), sapma ise yalnız doğrulama ---
    if abs(buy_ratio - 0.5) < 1e-9:
        return None
    aggressor = 1 if buy_ratio > 0.5 else -1
    if deviation_ok and deviation != 0 and (1 if deviation > 0 else -1) != aggressor:
        log.debug(
            f"Yön çelişkisi ({symbol}): buy_ratio={buy_ratio:.3f} baseline={baseline:.3f} "
            f"→ baskın taraf ile sapma ters; baskı sönümleniyor, alarm yok."
        )
        return None
    direction = aggressor
    pressure_trend = "STRENGTHENING" if deviation * direction > 0 else "FLAT_OR_FADING"

    # --- 7. Cooldown Kontrolü ---
    with _ALERTS_LOCK:
        alerts = _load_whale_alerts()
        prev_alert = alerts.get(symbol)

    if prev_alert:
        if str(prev_alert.get("bar_time", "")) == bar_time:
            return None
        cooldown_sec = SETTINGS.WHALE_ALERT_COOLDOWN_BARS * period_s
        try:
            prev_ms = int(prev_alert.get("bar_time_ms", 0))
            if prev_ms <= 0 or bar_ts_ms <= 0:
                raise ValueError("bar zaman damgası eksik")
            elapsed = (bar_ts_ms - prev_ms) / 1000.0
        except Exception as e:
            # Cooldown'u atlamak yerine duvar saatine (alerted_at) düş
            log.warning(f"Cooldown zaman ayrıştırma hatası ({symbol}): {e} — alerted_at ile kontrol")
            try:
                elapsed = time.time() - float(prev_alert.get("alerted_at", 0) or 0)
            except (TypeError, ValueError):
                elapsed = 0.0  # okunamıyorsa güvenli taraf: alarmı ertele
        if elapsed < cooldown_sec:
            return None

    # --- 8. Multi-Timeframe Onayı (5m + 15m) ---
    # Kline/OI isteklerinden ÖNCE çalışır: reddedilen adaylar için boşuna
    # API isteği yapılmaz. Veri yoksa teyit puanı VERİLMEZ (UNAVAILABLE).
    tf_state = "OFF"
    dev_15m = None
    if multi_tf_confirm:
        tf_state = "UNAVAILABLE"
        try:
            raw_15m = _fetch_taker_ratios(symbol, "15m", lookback + 1)
            dev_15m = _calc_deviation(raw_15m)
            if dev_15m is not None:
                if (direction > 0) != (dev_15m > 0):
                    log.debug(
                        f"MTF ret ({symbol}): yön={direction:+d} 15m dev={dev_15m:+.3f} yön uyuşmuyor"
                    )
                    return None
                tf_state = "CONFIRMED"
        except Exception as e:
            log.debug(f"15m taker ratio hatası ({symbol}): {e}")

    # --- 9. Kalıcılık Kontrolü (ÖNCEKİ 3 periyot, alarm barı hariç) ---
    # Bilinçli tasarım: alarm barından önce de aynı yönde birikim var mıydı?
    persistent = False
    if len(buy_vols) >= 4:
        recent_3_ratios = [
            _buy_ratio(buy_vols[i], sell_vols[i])
            for i in range(-4, -1)
        ]
        recent_3_avg = sum(recent_3_ratios) / len(recent_3_ratios)
        if direction > 0:
            persistent = recent_3_avg > (baseline + 0.05)
        else:
            persistent = recent_3_avg < (baseline - 0.05)

    # --- 10. Kline Analizi: Absorpsiyon + Stealth (yalnızca KAPANMIŞ mumlar) ---
    # Absorpsiyon: agresif akış var ama fiyat o yönde ilerlemiyor
    #   (alım emilirse pasif satıcı, satış emilirse pasif alıcı var).
    # Stealth: çok barlı net CVD aynı yönde, mutlak USD anlamlı, fiyat yatay.
    absorption = False
    absorption_strength = ""
    absorbed_by = ""
    score_boost = 0
    stealth_accumulation = False
    stealth_distribution = False
    kline_ok = False
    price_move_pct = None
    bar_diag = {"taker_ts_ms": bar_ts_ms}
    sup_level = None
    res_level = None
    avg_usd_baseline = avg_total * mark_price if avg_total > 0 else 0.0
    med_usd_baseline = med_total * mark_price if med_total > 0 else 0.0
    try:
        klines = get_raw_klines(symbol, interval, 8)
        now_ms = int(time.time() * 1000)
        if isinstance(klines, list) and klines:
            # k[6] = mumun kapanış zamanı; son eleman çoğunlukla AÇIK mumdur
            closed = [k for k in klines if int(k[6]) < now_ms]
            forming = [k for k in klines if int(k[6]) >= now_ms]
            if closed:
                k_lows = [float(k[3]) for k in closed]
                k_highs = [float(k[2]) for k in closed]
                sup_level = min(k_lows) if k_lows else None
                res_level = max(k_highs) if k_highs else None
                last_k = closed[-1]
                k_open = float(last_k[1])
                k_high = float(last_k[2])
                k_low = float(last_k[3])
                k_close = float(last_k[4])
                k_vol = float(last_k[5])
                if k_open > 0:
                    kline_ok = True
                    rng = k_high - k_low
                    close_loc = (k_close - k_low) / rng if rng > 0 else 0.5
                    # Akış yönündeki fiyat ilerlemesi (negatif = fiyat ters gitti)
                    move_in_dir = direction * (k_close - k_open) / k_open
                    price_move_pct = round((k_close - k_open) / k_open * 100.0, 3)

                    # Zaman hizalama teşhisi (sinyali değiştirmez, loglanır):
                    # taker toplam hacmi hangi mumla örtüşüyor? ≈1.0 → o mumla aynı bar.
                    bar_diag.update({
                        "taker_age_s": round(now_ms / 1000.0 - bar_ts_ms / 1000.0, 1) if bar_ts_ms else None,
                        "kline_open_ms": int(last_k[0]),
                        "taker_vs_closed_kline_vol": round(current_total / k_vol, 3) if k_vol > 0 else None,
                    })
                    if forming:
                        f_vol = float(forming[-1][5])
                        bar_diag["taker_vs_forming_kline_vol"] = (
                            round(current_total / f_vol, 3) if f_vol > 0 else None
                        )
                    log.debug(f"Bar hizalama ({symbol}): {bar_diag}")

                    # --- Absorpsiyon (çok koşullu; hacim + fiyat ÇEKİRDEK koşul) ---
                    cond_volume = vol_spike >= 2.0
                    cond_price = move_in_dir < 0.002  # fiyat akış yönünde ilerlemedi
                    dominant_usd = buy_usd if direction > 0 else sell_usd
                    avg_usd_baseline = avg_total * mark_price if avg_total > 0 else 0
                    cond_flow = (dominant_usd / avg_usd_baseline) > 1.5 if avg_usd_baseline > 0 else False
                    # Kapanış konumu akışa karşı mı? (alımda alt yarı, satışta üst yarı)
                    if rng > 0:
                        cond_reject = (close_loc <= 0.4) if direction > 0 else (close_loc >= 0.6)
                    else:
                        cond_reject = False

                    hits = sum([cond_volume, cond_price, cond_flow, cond_reject])
                    core = cond_volume and cond_price
                    if core and hits >= 3:
                        absorption, absorption_strength = True, "strong"
                        score_boost = 10 + (hits - 3) * 5  # 10 veya 15
                    elif core and hits == 2:
                        absorption, absorption_strength = True, "weak"
                        score_boost = 5
                    if absorption:
                        absorbed_by = "passive_sellers" if direction > 0 else "passive_buyers"

                    # --- Stealth: çok barlı CVD + yatay fiyat + mutlak büyüklük ---
                    win = 6
                    if len(closed) >= win and len(buy_vols) >= win:
                        w_open = float(closed[-win][1])
                        w_close = float(closed[-1][4])
                        if w_open > 0:
                            w_move = (w_close - w_open) / w_open
                            wb, ws = buy_vols[-win:], sell_vols[-win:]
                            w_total = sum(wb) + sum(ws)
                            w_cvd = sum(wb) - sum(ws)
                            cvd_ratio = w_cvd / w_total if w_total > 0 else 0.0
                            pos_bars = sum(1 for b, s in zip(wb, ws) if b > s)
                            neg_bars = sum(1 for b, s in zip(wb, ws) if s > b)
                            cvd_usd = abs(w_cvd) * mark_price
                            max_move = float(getattr(SETTINGS, "WHALE_ALERT_STEALTH_MAX_MOVE", 0.004))
                            min_usd = float(getattr(SETTINGS, "WHALE_ALERT_MIN_VOLUME", 0) or 0)
                            if abs(w_move) < max_move and cvd_usd >= min_usd:
                                if direction > 0 and cvd_ratio >= 0.04 and pos_bars >= 4:
                                    stealth_accumulation = True
                                elif direction < 0 and cvd_ratio <= -0.04 and neg_bars >= 4:
                                    stealth_distribution = True
    except Exception as e:
        log.debug(f"Kline/absorpsiyon hesaplama hatası ({symbol}): {e}")

    # --- 11. Open Interest Analizi ---
    # Birincil ölçüm KONTRAT ADEDİ değişimidir (fiyattan bağımsız).
    # USD-OI (sumOpenInterestValue) fiyat hareketiyle de değişir; yalnızca
    # bilgi olarak tutulur / kontrat verisi yoksa yedek olarak kullanılır.
    oi_enabled = getattr(SETTINGS, "WHALE_ALERT_OI_ENABLED", True)
    oi_change_usd = None
    oi_change_pct = None
    oi_last_usd = None
    oi_change_contracts = None
    oi_change_contracts_pct = None
    oi_notional_usd = None
    oi_usd_vs_contracts_mismatch = False
    oi_aligned = True
    if oi_enabled:
        try:
            raw_oi = get_open_interest_history(symbol, interval, limit=2)
            if raw_oi and len(raw_oi) >= 2:
                oi_prev = float(raw_oi[-2].get("sumOpenInterestValue", 0.0))
                oi_last_usd = float(raw_oi[-1].get("sumOpenInterestValue", 0.0))
                if oi_prev > 0:
                    oi_change_usd = oi_last_usd - oi_prev
                    oi_change_pct = (oi_change_usd / oi_prev) * 100.0
                oi_prev_c = float(raw_oi[-2].get("sumOpenInterest", 0.0))
                oi_last_c = float(raw_oi[-1].get("sumOpenInterest", 0.0))
                if oi_prev_c > 0:
                    oi_change_contracts = oi_last_c - oi_prev_c
                    oi_change_contracts_pct = (oi_change_contracts / oi_prev_c) * 100.0
                    oi_notional_usd = oi_change_contracts * mark_price  # fiyat etkisinden arındırılmış
                    if oi_change_usd is not None and abs(oi_change_usd) > 50_000:
                        oi_usd_vs_contracts_mismatch = (
                            (oi_change_usd > 0) != (oi_change_contracts > 0)
                        )
                # Zaman uyumu: OI barı taker barından 1 periyottan fazla kaymışsa güven düşer
                try:
                    oi_ts = int(raw_oi[-1].get("timestamp", 0) or 0)
                    if oi_ts and bar_ts_ms:
                        oi_aligned = abs(oi_ts - bar_ts_ms) <= period_s * 1000
                except (TypeError, ValueError):
                    pass
        except Exception as e:
            log.debug(f"OI hesaplama hatası ({symbol}): {e}")

    if oi_change_contracts_pct is not None:
        position_event, oi_confidence, oi_confidence_score = _classify_position_event(
            direction, oi_change_contracts_pct, oi_notional_usd,
            oi_aligned=oi_aligned, price_neutral=True,
        )
    else:
        # Kontrat verisi yok → USD-OI yedeği (fiyat etkili; güven LOW'a sabitlenir)
        position_event, oi_confidence, oi_confidence_score = _classify_position_event(
            direction, oi_change_pct, oi_change_usd,
            oi_aligned=oi_aligned, price_neutral=False,
        )

    # Baskın taraftaki agresif taker hacmi. AÇILAN POZİSYON BÜYÜKLÜĞÜ DEĞİLDİR.
    est_position_usd = buy_usd if direction > 0 else sell_usd

    # Likidasyon senaryosu: gerçek pozisyon büyüklüğü bilinmediği için marj
    # kademesi, agresif taker hacmi VARSAYIMIYLA seçilir (OI değişimi pozisyon
    # notional'ı değildir, kullanılmaz). Senaryo ikincil bilgidir.
    liq_notional_usd = est_position_usd
    liq_scenarios = _liq_scenarios(mark_price, direction, liq_notional_usd)
    assumed_lev = getattr(SETTINGS, "WHALE_ALERT_ASSUMED_LEVERAGE", 25)
    headline_scenario = next(
        (s for s in liq_scenarios if s["leverage"] == assumed_lev),
        liq_scenarios[0] if liq_scenarios else None,
    )
    if headline_scenario is None or headline_scenario["impossible"]:
        feasible = [s for s in liq_scenarios if not s["impossible"]]
        if feasible:
            headline_scenario = max(feasible, key=lambda s: s["leverage"])
    if headline_scenario is not None and not headline_scenario["impossible"]:
        liq_price = headline_scenario["liq_price"]
        liq_distance_pct = headline_scenario["distance_pct"]
    else:
        liq_price = None
        liq_distance_pct = None

    # --- Kalite etiketi (öncelik: güçlü absorpsiyon > stealth > kalıcı > zayıf absorpsiyon > spike) ---
    if absorption and absorption_strength == "strong":
        quality_label = "absorption"
    elif stealth_accumulation or stealth_distribution:
        quality_label = "stealth"
    elif persistent:
        quality_label = "persistent"
    elif absorption:
        quality_label = "absorption_weak"
    elif vol_spike >= 3.0:
        quality_label = "spike"
    else:
        quality_label = ""

    # --- Sembol Başına Sinyal Durumu ---
    signal_state = _resolve_symbol_state(symbol, direction, dev_15m)
    if signal_state != "CONFLICTED":
        if quality_label == "absorption":
            signal_state = "BEARISH_FLOW_ABSORBED" if direction < 0 else "BULLISH_FLOW_ABSORBED"
        elif quality_label == "absorption_weak":
            signal_state = "BEARISH_FLOW_ABSORB_WEAK" if direction < 0 else "BULLISH_FLOW_ABSORB_WEAK"

    # --- Whale Score ---
    whale_score = _calc_whale_score(
        buy_ratio=buy_ratio,
        vol_spike=vol_spike,
        dominant_usd=est_position_usd,
        oi_confidence_score=oi_confidence_score,
        tf_state=tf_state,
        quality_label=quality_label,
        signal_state=signal_state,
    )

    # --- Minimum skor kapısı (opsiyonel; WHALE_ALERT_MIN_SCORE, vars. 0 = kapalı) ---
    # Cooldown tüketilmeden önce uygulanır: düşük skorlu aday, sonraki güçlü
    # sinyali cooldown ile engellemez.
    min_score = float(getattr(SETTINGS, "WHALE_ALERT_MIN_SCORE", 0) or 0)
    if whale_score["composite"] < min_score:
        log.debug(f"Skor kapısı ({symbol}): {whale_score['composite']} < {min_score:g}, alarm yok.")
        return None

    # --- Veri kalitesi bayrakları (kullanıcıya gösterilir) ---
    data_flags = []
    if multi_tf_confirm and tf_state == "UNAVAILABLE":
        data_flags.append("15m verisi yok")
    if not kline_ok:
        data_flags.append("kline yok (absorpsiyon/stealth ölçülemedi)")
    if oi_enabled and oi_change_contracts_pct is None:
        data_flags.append("OI kontrat verisi yok" if oi_change_pct is not None else "OI verisi yok")
    if oi_enabled and not oi_aligned:
        data_flags.append("OI zaman damgası taker barıyla uyuşmuyor")

    # --- 12. State Güncelle & Tekrar Takibi ---
    with _ALERTS_LOCK:
        alerts = _load_whale_alerts()
        now_ts = time.time()
        prev_entry = alerts.get(symbol, {})
        raw_hist = prev_entry.get("history_1h", []) if isinstance(prev_entry, dict) else []
        # Son 1 saat (3600 sn) içindeki alarmlar
        history_1h = [t for t in raw_hist if (now_ts - t) <= 3600]
        alert_count_1h = len(history_1h) + 1
        last_alert_ago_min = int((now_ts - history_1h[-1]) / 60) if history_1h else None
        history_1h.append(now_ts)

        alerts[symbol] = {
            "bar_time": bar_time,
            "bar_time_ms": bar_ts_ms,
            "buy_ratio": buy_ratio,
            "deviation": deviation,
            "alerted_at": now_ts,
            "history_1h": history_1h,
            "alert_count_1h": alert_count_1h,
            "direction": direction,
        }
        _save_whale_alerts(alerts)

    # buy_sell_ratio: sonsuzluk koruması → 999.0 (fmt'de "N/A" gösterilir)
    bsr = (last_buy / last_sell) if last_sell > 0 else 999.0

    info = {
        "symbol": symbol,
        "direction": direction,
        "aggressor_side": "BUY" if direction > 0 else "SELL",
        "pressure_trend": pressure_trend,
        "buy_ratio": buy_ratio,
        "baseline_ratio": baseline,
        "deviation": deviation,
        "buy_volume": last_buy,
        "sell_volume": last_sell,
        "buy_volume_usd": buy_usd,
        "sell_volume_usd": sell_usd,
        "buy_sell_ratio": bsr,
        "bar_time": bar_time,
        "vol_spike": vol_spike,
        "vol_spike_med": round(vol_spike_med, 2),
        "vol_rank": round(vol_rank, 3),
        "avg_usd_baseline": avg_usd_baseline,
        "med_usd_baseline": med_usd_baseline,
        "lookback_bars": lookback,
        "lookback_mins": (lookback * period_s) // 60,
        "support_level": sup_level,
        "resistance_level": res_level,
        "alert_count_1h": alert_count_1h,
        "last_alert_ago_min": last_alert_ago_min,
        "persistent": persistent,
        "stealth_accumulation": stealth_accumulation,
        "stealth_distribution": stealth_distribution,
        "absorption": absorption,
        "absorption_strength": absorption_strength,
        "absorbed_by": absorbed_by,
        "price_move_pct": price_move_pct,
        "quality_label": quality_label,
        "score_boost": score_boost,
        "mark_price": mark_price,
        "tf_state": tf_state,
        "tf_confirmed": tf_state == "CONFIRMED",
        "deviation_15m": round(dev_15m, 4) if dev_15m is not None else None,
        # --- Pozisyon olayı (olasılıksal) ---
        "position_event": position_event,
        "est_position_usd": round(est_position_usd, 2),        # geriye uyumluluk
        "aggressive_taker_usd": round(est_position_usd, 2),    # doğru ad
        "oi_change_usd": round(oi_change_usd, 2) if oi_change_usd is not None else None,
        "oi_change_pct": round(oi_change_pct, 3) if oi_change_pct is not None else None,
        "oi_last_usd": round(oi_last_usd, 2) if oi_last_usd is not None else None,
        "oi_change_contracts": round(oi_change_contracts, 4) if oi_change_contracts is not None else None,
        "oi_change_contracts_pct": round(oi_change_contracts_pct, 3) if oi_change_contracts_pct is not None else None,
        "oi_notional_usd": round(oi_notional_usd, 2) if oi_notional_usd is not None else None,
        "oi_usd_vs_contracts_mismatch": oi_usd_vs_contracts_mismatch,
        "oi_aligned": oi_aligned,
        # --- Varsayımsal kaldıraç / likidasyon senaryosu ---
        "assumed_leverage": assumed_lev,
        "liq_notional_usd": round(liq_notional_usd, 2),
        "liq_scenarios": liq_scenarios,
        "liq_price": liq_price,
        "liq_distance_pct": liq_distance_pct,
        # --- Güven / durum / skor ---
        "oi_confidence": oi_confidence,
        "oi_confidence_score": oi_confidence_score,
        "signal_state": signal_state,
        "whale_score": whale_score,
        "data_flags": data_flags,
        "bar_diag": bar_diag,
    }
    _append_alert_history(info)
    return info


# ---------------------------------------------------------------------------
# Formatlama
# ---------------------------------------------------------------------------

def fmt_whale_alert(info, rank=None):
    """Whale alert bildirimini Telegram formatına çevir.

    Mesajda ÖLÇÜM (taker hacmi, OI değişimi) ile YORUM (olası pozisyon olayı)
    ayrı satırlarda gösterilir. Agresif taker hacmi açılan pozisyon
    büyüklüğü değildir; likidasyon senaryosu gerçek pozisyon verisi değil,
    varsayımsal bir hesaptır.

    Args:
        info: check_whale_alert()'in döndürdüğü dict
        rank: Global sapma sırası (#1, #2, ...). None ise başlıkta gösterilmez.
    """
    symbol = info["symbol"]
    direction = info["direction"]
    buy_ratio = info["buy_ratio"]
    baseline = info["baseline_ratio"]
    deviation = info["deviation"]
    buy_vol = info["buy_volume"]
    sell_vol = info["sell_volume"]
    buy_usd = info.get("buy_volume_usd", 0.0)
    sell_usd = info.get("sell_volume_usd", 0.0)
    bsr = info["buy_sell_ratio"]
    vol_spike = info.get("vol_spike", 0.0)
    vol_spike_med = info.get("vol_spike_med", 0.0)
    persistent = info.get("persistent", False)
    quality_label = info.get("quality_label", "")
    tf_state = info.get(
        "tf_state", "CONFIRMED" if info.get("tf_confirmed", True) else "UNAVAILABLE"
    )
    mark_price = info.get("mark_price", 0.0)
    signal_state = info.get("signal_state", "NEUTRAL")
    whale_score = info.get("whale_score") or {}
    oi_usd_vs_contracts_mismatch = info.get("oi_usd_vs_contracts_mismatch", False)
    pressure_trend = info.get("pressure_trend", "STRENGTHENING")
    data_flags = info.get("data_flags") or []

    # Destek & Direnç seviyeleri
    sup_level = info.get("support_level")
    res_level = info.get("resistance_level")

    # Tekrar takibi (son 1 saat)
    alert_count_1h = info.get("alert_count_1h", 1)
    last_alert_ago_min = info.get("last_alert_ago_min")

    # Göreli hacim bağlamı
    avg_usd_baseline = info.get("avg_usd_baseline", 0.0)
    lookback_bars = info.get("lookback_bars", 20)
    lookback_mins = info.get("lookback_mins", 100)

    # --- Pozisyon olayı (olasılıksal) ---
    position_event = info.get("position_event", "flow")
    taker_usd = info.get("aggressive_taker_usd", info.get("est_position_usd", 0.0))
    oi_change_usd = info.get("oi_change_usd")
    oi_c_pct = info.get("oi_change_contracts_pct")
    oi_notional = info.get("oi_notional_usd")

    # --- Kaldıraç / likidasyon senaryosu ---
    assumed_lev = info.get("assumed_leverage", 25)
    liq_scenarios = info.get("liq_scenarios") or []
    liq_notional = info.get("liq_notional_usd", taker_usd)

    # Absorpsiyon durumu
    is_absorbed = quality_label in ("absorption", "absorption_weak")

    if is_absorbed:
        dir_icon = "💎🟢" if direction > 0 else "💎🔴"
        dir_label = "Agresif ALICI (⚠️ Pasif Satıcı Tarafından Emildi)" if direction > 0 else "Agresif SATICI (⚠️ Pasif Alıcı Tarafından Emildi)"
    else:
        dir_icon = "🐋🟢" if direction > 0 else "🐋🔴"
        dir_label = "Agresif ALICI (Long Baskı)" if direction > 0 else "Agresif SATICI (Short Baskı)"
    liq_side = "aşağıda" if direction > 0 else "yukarıda"

    oi_confidence = info.get("oi_confidence", "LOW")
    conf_badge = {
        "HIGH": "🟢 Yüksek veri uyumu",
        "MEDIUM": "🟡 Orta veri uyumu",
        "LOW": "🔴 Düşük veri uyumu",
    }.get(oi_confidence, "")

    noise = float(getattr(SETTINGS, "OI_EVENT_NOISE_PCT", 0.03))
    _event_map = {
        "probable_long_opened":  ("🟢 OLASI LONG AÇILIŞI",
                                   f"Alım + OI artışı (+%{abs(oi_c_pct):.2f}) — taze long girişiyle uyumlu (olasılıksal; kesin pozisyon değildir)."),
        "probable_short_opened": ("🔴 OLASI SHORT AÇILIŞI",
                                   f"Satış + OI artışı (+%{abs(oi_c_pct):.2f}) — taze short girişiyle uyumlu (olasılıksal; kesin pozisyon değildir)."),
        "probable_short_closed": ("🟠 OLASI SHORT KAPANIŞI",
                                   f"Alım + OI azalışı (-%{abs(oi_c_pct):.2f}) — short kâr alma/kapanışıyla uyumlu (squeeze riski)."),
        "probable_long_closed":  ("🟠 OLASI LONG KAPANIŞI",
                                   f"Satış + OI azalışı (-%{abs(oi_c_pct):.2f}) — long tasfiyesi/kapanışıyla uyumlu (satış baskısı gevşeyebilir)."),
        "flow": ("🌊 Agresif ALICI AKIŞI" if direction > 0 else "🌊 Agresif SATICI AKIŞI",
                 f"Hacmin yönü agresif şekilde {'alıcıya' if direction > 0 else 'satıcıya'} kaydı; "
                 + ("OI verisi yok, pozisyon olayı belirlenemedi."
                    if oi_c_pct is None else
                    f"OI değişimi gürültü filtresi (±%{noise:.2f}) altında (%{oi_c_pct:+.3f}); net pozisyon yönü çıkarılamadı.")),
    }

    if is_absorbed:
        if quality_label == "absorption":
            event_label = "💎 ABSORPSİYON — SATICI EMİLDİ" if direction < 0 else "💎 ABSORPSİYON — ALICI EMİLDİ"
            event_note = (
                f"Satış hacmine ({_fmt_usd(taker_usd)}) rağmen fiyat dip kıramadı; pasif alıcılar emdi. Ayı tezi zayıflamış olabilir."
                if direction < 0 else
                f"Alış hacmine ({_fmt_usd(taker_usd)}) rağmen fiyat tepe aşamadı; pasif satıcılar emdi. Boğa tezi zayıflamış olabilir."
            )
        else:
            event_label = "🔹 ZAYIF ABSORPSİYON"
            event_note = "Hacim artışına rağmen fiyat yönünde ilerleme sınırlı kaldı; pasif emir direnci olabilir."
    else:
        event_label, event_note = _event_map.get(position_event, _event_map["flow"])

    # Hacim dilimi etiketi (algı uyumsuzluğunu önlemek için tarafsız ve açıklayıcı)
    dev_pct = abs(deviation) * 100
    dev_tier = 3 if dev_pct >= 30 else 2 if dev_pct >= 20 else 1 if dev_pct >= 10 else 0
    usd_score = (whale_score.get("breakdown") or {}).get("size_usd")
    if usd_score is None:
        usd_tier = dev_tier
    else:
        usd_tier = 3 if usd_score >= 70 else 2 if usd_score >= 40 else 1 if usd_score >= 15 else 0
    tier_idx = min(dev_tier, usd_tier)
    size_label = ["⚡ Standart", "🔹 Orta Hacim", "🔶 Yüksek Hacim", "💎 Çok Yüksek Hacim"][tier_idx]

    rank_str = f"#{rank} " if rank else ""

    # buy_sell_ratio: 999 sonsuzluk sentinel → "N/A"
    bsr_str = f"{bsr:.2f}" if bsr < 999 else "N/A"

    # Kalite etiketi
    if direction > 0:
        absorb_text = "💎 ABSORPSİYON — agresif alım fiyatı ilerletemedi (pasif satıcı emdi)"
        absorb_weak_text = "🔹 ZAYIF ABSORPSİYON — alım hacmi var, fiyat ilerlemiyor (pasif satıcı olabilir)"
        stealth_text = "🕵️ STEALTH BİRİKİM — çok barlı net alım (CVD↑), fiyat yatay"
    else:
        absorb_text = "💎 ABSORPSİYON — agresif satış fiyatı düşüremedi (pasif alıcı emdi)"
        absorb_weak_text = "🔹 ZAYIF ABSORPSİYON — satış hacmi var, fiyat düşmüyor (pasif alıcı olabilir)"
        stealth_text = "🕵️ STEALTH DAĞITIM — çok barlı net satış (CVD↓), fiyat yatay"
    _quality_map = {
        "absorption": absorb_text,
        "absorption_weak": absorb_weak_text,
        "stealth": stealth_text,
        "persistent": "📌 KALICI BASKI — Son 3 periyottur (15 dk) taker akışı kesintisiz aynı yönde sürüyor",
        "spike": f"💥 HACİM SPİKE — {vol_spike:.1f}x ort. / {vol_spike_med:.1f}x medyan",
    }
    quality_lines = []
    if quality_label in _quality_map:
        quality_lines.append(f"   {_quality_map[quality_label]}")
    secondary = []
    if quality_label != "spike" and vol_spike >= 3.0:
        secondary.append(f"💥 Spike: {vol_spike:.1f}x")
    if quality_label != "persistent" and persistent:
        secondary.append("📌 Kalıcı (15 dk)")
    if secondary:
        quality_lines.append("   " + "  |  ".join(secondary))
    quality_str = ("\n" + "\n".join(quality_lines)) if quality_lines else ""

    # Open Interest satırı
    oi_str = ""
    if oi_c_pct is not None:
        c_sign = "+" if oi_c_pct >= 0 else "-"
        c_note = "açık pozisyon artışı" if oi_c_pct > 0 else "açık pozisyon azalışı"
        notional_part = ""
        if oi_notional:
            notional_part = f" ≈ {c_sign}{_fmt_usd(abs(oi_notional))}"
        oi_str = f"\n🔍 OI (kontrat): {c_sign}%{abs(oi_c_pct):.2f}{notional_part} — {c_note} [Gürültü filtresi: ±%{noise:.2f}]"
        if oi_change_usd:
            u_sign = "+" if oi_change_usd >= 0 else "-"
            oi_str += f"\n   ↳ OI değeri (USD, fiyat etkili): {u_sign}{_fmt_usd(abs(oi_change_usd))}"
    elif oi_change_usd:
        u_sign = "+" if oi_change_usd >= 0 else "-"
        oi_str = (
            f"\n🔍 OI değeri (USD): {u_sign}{_fmt_usd(abs(oi_change_usd))} "
            f"— kontrat verisi yok, fiyat etkisi içerebilir"
        )

    # Kaldıraç / likidasyon senaryosu satırları
    lev_str = ""
    if liq_scenarios:
        headline = next((s for s in liq_scenarios if s["leverage"] == assumed_lev), None)
        if headline is None or headline.get("impossible"):
            feasible = [s for s in liq_scenarios if not s.get("impossible")]
            if feasible:
                headline = max(feasible, key=lambda s: s["leverage"])
            elif headline is None:
                headline = liq_scenarios[0]
        if headline.get("impossible"):
            headline_line = (
                f"   {headline['leverage']}x → bu büyüklükte izole marj yetersiz (cross gerekir)"
            )
        else:
            headline_line = (
                f"   {headline['leverage']}x → Varsayımsal risk bölgesi: %{headline['distance_pct']:.1f} "
                f"{liq_side} (~${_fmt_price(headline['liq_price'])})"
            )
        others = []
        for s in liq_scenarios:
            if s["leverage"] == headline["leverage"]:
                continue
            if s.get("impossible"):
                others.append(f"{s['leverage']}x: marj yetersiz")
            else:
                others.append(f"{s['leverage']}x: %{s['distance_pct']:.1f} (~${_fmt_price(s['liq_price'])})")
        others_str = f"\n   {'  |  '.join(others)}" if others else ""
        lev_str = (
            f"\n⚙️ Varsayımsal senaryo (notional ≈ {_fmt_usd(liq_notional)} taker hacmi varsayımı; "
            f"gerçek pozisyon bilgisi yok):"
            f"\n{headline_line}{others_str}"
        )

    # MTF teyit rozeti
    tf_str = {
        "CONFIRMED": "✅ MTF Teyidi: 5m ve 15m zaman dilimlerinde akış aynı yönde",
        "UNAVAILABLE": "⚠️ MTF Teyidi: 15m verisi yok (yalnız 5m değerlendirildi)",
        "OFF": "ℹ️ MTF: Çoklu zaman dilimi kapalı",
    }.get(tf_state, tf_state)

    # Whale Score gösterimi & Formül Şeffaflığı
    ws_composite = whale_score.get("composite", 0)
    ws_label = whale_score.get("label", "")
    ws_flow = whale_score.get("flow_strength", 0)
    ws_dir = whale_score.get("direction_conf", 0)
    ws_bd = whale_score.get("breakdown") or {}
    score_str = (
        f"\n🎯 Whale Score: {ws_composite}/100 — {ws_label} [Formül: %55 Akış ({ws_flow}) + %45 Yön ({ws_dir})]\n"
        f"   ↳ Detay: Akış [Uç Payı: {ws_bd.get('taker_extreme', 0)} | Spike: {ws_bd.get('vol_spike', 0)} | USD: {ws_bd.get('size_usd', 0)}] "
        f"| Yön [OI: {ws_bd.get('oi_conf', 0)} | MTF: {ws_bd.get('tf_score', 0)}]"
        if whale_score else ""
    )

    # Signal state gösterimi
    _state_icons = {
        "BULLISH_FLOW": "📈 BULLISH_FLOW (Alıcı Baskın)",
        "BEARISH_FLOW": "📉 BEARISH_FLOW (Satıcı Baskın)",
        "BULLISH_FLOW_ABSORBED": "💎 ABSORPSİYON (Alıcı Emildi — Boğa Tezi Zayıf / Pasif Satıcı Üstün)",
        "BEARISH_FLOW_ABSORBED": "💎 ABSORPSİYON (Satıcı Emildi — Ayı Tezi Zayıf / Pasif Alıcı Üstün)",
        "BULLISH_FLOW_ABSORB_WEAK": "🔹 ZAYIF ABSORPSİYON (Alış Fiyatı Yükseltemedi)",
        "BEARISH_FLOW_ABSORB_WEAK": "🔹 ZAYIF ABSORPSİYON (Satış Fiyatı Düşüremedi)",
        "CONFLICTED": "⚠️ ÇELİŞEN — son 30 dk içinde ters yönde alarm alındı",
        "NEUTRAL": "➖ NEUTRAL",
    }
    state_display = _state_icons.get(signal_state, signal_state)

    # Sapma satırı
    trend_note = "" if pressure_trend == "STRENGTHENING" else "  [hacim kaynaklı tetik; baskı baseline'a göre artmadı]"

    oi_mismatch_str = (
        "\n⚠️ OI değeri (USD) fiyat hareketinden etkilenmiş; yorum kontrat adedine göre yapıldı"
        if oi_usd_vs_contracts_mismatch else ""
    )
    flags_str = (
        "\n⚠️ Eksik/uyumsuz veri: " + "; ".join(data_flags) if data_flags else ""
    )

    # Seviyeler satırı
    levels_str = ""
    if sup_level is not None and res_level is not None:
        levels_str = f"\n🛡️ Seviyeler (Son 8 Mum): Taban/Destek: ${_fmt_price(sup_level)} | Tavan/Direnç: ${_fmt_price(res_level)}"

    # Tekrar takibi satırı
    if alert_count_1h > 1 and last_alert_ago_min is not None:
        repeat_str = f"\n🕒 Tekrar Takibi: Bu coin için son 1 saatte {alert_count_1h}. uyarı ({last_alert_ago_min} dk önce de akış kaydedildi)"
    else:
        repeat_str = f"\n🕒 Tekrar Takibi: Son 1 saatteki ilk balina akışı"

    # Göreli hacim satırı
    vol_context = ""
    if avg_usd_baseline > 0:
        vol_context = f" (5dk periyot ortalaması: {_fmt_usd(avg_usd_baseline)} | {vol_spike:.1f}x spike — son {lookback_bars} periyot / {lookback_mins}dk referansı)"

    # Teyit ve Geçersizleşme (Absorpsiyon çelişkisi giderildi)
    if is_absorbed:
        sup_str = f"${_fmt_price(sup_level)}" if sup_level is not None else "destek tabanı"
        res_str = f"${_fmt_price(res_level)}" if res_level is not None else "direnç tavanı"
        if direction < 0:
            confirm_text = f"Fiyatın emilim tabanından ({sup_str}) yukarı tepki vermesi + alıcıların piyasa emrine geçmesi"
            invalidate_text = f"Satıcıların pasif alıcıyı delip taban seviyesini ({sup_str}) aşağı kırması"
        else:
            confirm_text = f"Fiyatın emilim tavanından ({res_str}) aşağı dönmesi + satıcıların piyasa emrine geçmesi"
            invalidate_text = f"Alıcıların pasif satıcıyı delip tavan seviyesini ({res_str}) yukarı kırması"
    else:
        _confirm_map = {
            "probable_long_opened":  (
                "Fiyatın yükselişini sürdürmesi + alım akışının devamı",
                "Satış baskısının güçlenmesi veya OI düşüşü",
            ),
            "probable_short_opened": (
                "Fiyatın düşüşünü sürdürmesi + satış akışının devamı",
                "Alım baskısının güçlenmesi veya OI düşüşü",
            ),
            "probable_short_closed": (
                "Fiyatın yükselişini sürdürmesi + alım akışının devamı",
                "Alım akışına rağmen fiyatın tavanı aşamaması veya satış gelmesi",
            ),
            "probable_long_closed":  (
                "Fiyatın son dip seviyesini kırması + satış akışının devamı",
                "Satış akışına rağmen fiyatın tabanı kıramayıp yukarı tepki vermesi",
            ),
            "flow": (
                "Alım akışının güçlenerek sürmesi" if direction > 0 else "Satış akışının güçlenerek sürmesi",
                "Akışın tükenmesi veya karşı yönde baskı başlaması",
            ),
        }
        confirm_text, invalidate_text = _confirm_map.get(position_event, _confirm_map["flow"])

    return (
        f"<b>🐋 BALİNA UYARISI {dir_icon} | {rank_str}{html.escape(str(symbol), quote=False)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{event_label} [{conf_badge}]\n"
        f"💵 Agresif taker hacmi: {_fmt_usd(taker_usd)}{vol_context}\n"
        f"   ↳ ℹ️ Pozisyon büyüklüğü değildir; 5 dakikalık tek yönlü agresif piyasa emri toplamıdır.\n"
        f"📈 Fiyat: ${_fmt_price(mark_price)}"
        f"{levels_str}"
        f"{oi_str}"
        f"{lev_str}\n"
        f"🏷️ Hacim Dilimi: {size_label} [Dolar büyüklük kademesidir; işlem kalitesini Whale Score belirler]{quality_str}"
        f"{score_str}\n"
        f"⚡ Sinyal: {state_display}\n"
        f"⚖️ Taker Buy Payı: %{buy_ratio * 100:.1f} (Ort: %{baseline * 100:.1f} → {deviation * 100:+.1f} puan sapma){trend_note}\n"
        f"📊 Taker Buy/Sell: {_fmt_usd(buy_usd)} / {_fmt_usd(sell_usd)} (oran {bsr_str})\n"
        f"   ↳ Coin: {_fmt_volume(buy_vol)} / {_fmt_volume(sell_vol)}\n"
        f"🧭 Yön: {dir_icon} {dir_label}\n"
        f"⏰ {SETTINGS.WHALE_ALERT_PERIOD} periyodu | {tf_str}"
        f"{repeat_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👁️ {event_note}"
        f"{oi_mismatch_str}"
        f"{flags_str}\n"
        f"✅ Teyit: {confirm_text}\n"
        f"❌ Geçersizleşme: {invalidate_text}"
    )
