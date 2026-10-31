"""
Hangi pariteler taranacak sorusunun cevabı: hardcoded liste yerine her tarama
döngüsünde Binance'ın kendi verisinden dinamik bir kısa liste çıkarılır.

Üç sinyal kullanılır (CoinMarketCap'teki "trending / gainers / inflow" mantığının
borsa-içi karşılığı, ekstra API key gerektirmeden):

  1. Momentum     -> 24s fiyat değişim yüzdesinin mutlak değeri ("sıra yükselmesi")
  2. Hacim sıçraması -> bugünkü 24s hacim, son N günün ortalamasına oranla ne kadar
                        anormal ("aşırı hacim girişi")
  3. OI değişimi  -> açık pozisyon büyüklüğünün bir önceki tarama turuna göre artışı
                     ("içine gerçek para giren" en güvenilir sinyal — sadece fiyat
                     hareketi değil, pozisyon açılışı/yeni para girişini ölçer)

Aşamalı çalışır ki ağır olan adımlar (OI, günlük mum) sadece ön elemeyi geçen
küçük bir kısa listeye uygulansın, tüm evrene değil.
"""
import json
import os
import time

from config import SETTINGS
from exchange import get_24hr_tickers, get_trading_symbols, get_open_interest, get_daily_klines
from logger import get_logger

log = get_logger("screener")


def _load_oi_history():
    if not os.path.exists(SETTINGS.SCREENER_OI_HISTORY_FILE):
        log.debug("OI history dosyası bulunamadı — ilk çalıştırma, OIΔ% = 0.0 olacak")
        return {}
    try:
        with open(SETTINGS.SCREENER_OI_HISTORY_FILE) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"OI history JSON yükleme hatası: {e}. Boş geçmiş ile başlanıyor.")
        return {}


def _save_oi_history(data):
    path = SETTINGS.SCREENER_OI_HISTORY_FILE
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
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
                    except Exception:
                        pass
    except Exception as e:
        log.warning(f"OI history kaydetme uyarısı ({path}): {e}")


def _minmax_norm(values):
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _stage1_shortlist(valid_symbols):
    """Tek API çağrısıyla tüm evrenin 24s istatistiklerini al, likidite tabanını
    uygula, hacim ve momentumda öne çıkanların BİRLEŞİMİNİ (union) kısa listeye al."""
    tickers = get_24hr_tickers()
    valid_set = set(valid_symbols)

    rows = []
    for t in tickers:
        sym = t.get("symbol")
        if sym not in valid_set:
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
            pct = float(t.get("priceChangePercent", 0))
        except (TypeError, ValueError):
            continue
        if qv < SETTINGS.SCREENER_MIN_QUOTE_VOLUME:
            continue
        rows.append({"symbol": sym, "quote_volume": qv, "price_change_pct": pct})

    if not rows:
        return []

    by_volume = sorted(rows, key=lambda r: r["quote_volume"], reverse=True)
    by_momentum = sorted(rows, key=lambda r: abs(r["price_change_pct"]), reverse=True)

    half = SETTINGS.SCREENER_SHORTLIST_SIZE // 2
    shortlisted = {r["symbol"]: r for r in by_volume[:half]}
    for r in by_momentum[:half]:
        shortlisted.setdefault(r["symbol"], r)

    return list(shortlisted.values())


def _stage2_enrich(shortlist):
    """Kısa liste için tek tek (ama küçük listede) hacim sıçraması ve OI
    değişimini hesapla."""
    oi_history = _load_oi_history()
    new_oi_history = {}
    enriched = []
    oi_errors = 0
    vol_errors = 0

    for row in shortlist:
        sym = row["symbol"]

        # Hacim sıçraması: bugünkü hacim / son N günün ortalaması
        volume_surge = 1.0
        try:
            daily = get_daily_klines(sym, SETTINGS.SCREENER_VOLUME_BASELINE_DAYS + 1)
            if daily is not None and len(daily) >= 2:
                baseline = daily["volume"].iloc[:-1].mean()  # bugünü hariç tut
                today_vol = daily["volume"].iloc[-1]
                if baseline > 0:
                    volume_surge = today_vol / baseline
        except Exception as e:
            vol_errors += 1
            log.warning(f"Hacim sıçraması hesaplanamadı: {sym} — {e}")

        # OI değişimi: bir önceki tarama turuna göre
        oi_change_pct = 0.0
        try:
            current_oi = get_open_interest(sym)
            new_oi_history[sym] = current_oi
            prev_oi = oi_history.get(sym)
            if prev_oi:
                oi_change_pct = (current_oi - prev_oi) / prev_oi * 100
            else:
                log.debug(f"OI önceki veri yok (ilk tur): {sym}, mevcut OI={current_oi:.2f}")
        except Exception as e:
            oi_errors += 1
            log.warning(f"OI çekilemedi: {sym} — {e}")

        row["volume_surge"] = volume_surge
        row["oi_change_pct"] = oi_change_pct
        enriched.append(row)

    if oi_errors:
        log.warning(f"⚠️  {oi_errors}/{len(shortlist)} sembolde OI çekilemedi")
    if vol_errors:
        log.warning(f"⚠️  {vol_errors}/{len(shortlist)} sembolde hacim sıçraması hesaplanamadı")

    _save_oi_history(new_oi_history)
    return enriched


def _stage3_rank(enriched):
    if not enriched:
        return []
    momentum = [abs(r["price_change_pct"]) for r in enriched]
    vol_surge = [r["volume_surge"] for r in enriched]
    oi_change = [r["oi_change_pct"] for r in enriched]

    m_norm = _minmax_norm(momentum)
    v_norm = _minmax_norm(vol_surge)
    o_norm = _minmax_norm(oi_change)

    for r, m, v, o in zip(enriched, m_norm, v_norm, o_norm):
        r["composite_score"] = (
            SETTINGS.SCREENER_W_MOMENTUM * m
            + SETTINGS.SCREENER_W_VOLUME_SURGE * v
            + SETTINGS.SCREENER_W_OI_CHANGE * o
        )

    return sorted(enriched, key=lambda r: r["composite_score"], reverse=True)


def screen_symbols(verbose=True):
    """Ana giriş noktası: taranacak parite listesini döner."""
    universe = get_trading_symbols_unbounded()
    shortlist = _stage1_shortlist(universe)
    enriched = _stage2_enrich(shortlist)
    ranked = _stage3_rank(enriched)

    top = ranked[: SETTINGS.SCREENER_FINAL_COUNT]
    symbols = [r["symbol"] for r in top]

    for always in SETTINGS.SCREENER_ALWAYS_INCLUDE:
        if always in universe and always not in symbols:
            symbols.append(always)

    if verbose:
        log.info(f"🔎 Screener: {len(universe)} evrenden {len(shortlist)} ön-eleme, "
                 f"{len(symbols)} parite seçildi")
        for r in top[:10]:
            # Fiyat trendi yön ikonu
            trend = "📈" if r["price_change_pct"] > 0 else "📉" if r["price_change_pct"] < 0 else "➡️"
            oi_str = f"OIΔ%={r['oi_change_pct']:+.1f}" if r["oi_change_pct"] != 0.0 else "OIΔ%=bekleniyor"
            log.info(f"   {trend} {r['symbol']:<12} skor={r['composite_score']:.2f}  "
                     f"Δ%={r['price_change_pct']:+.1f}  hacim_x={r['volume_surge']:.2f}  "
                     f"{oi_str}")

    return symbols


def get_trading_symbols_unbounded():
    """exchange.get_trading_symbols() MAX_SYMBOLS ile kısıtlı; screener tüm
    evreni görmek ister, o yüzden burada sınırı geçici olarak kaldırıyoruz."""
    original = SETTINGS.MAX_SYMBOLS
    SETTINGS.MAX_SYMBOLS = None
    try:
        return get_trading_symbols()
    finally:
        SETTINGS.MAX_SYMBOLS = original
