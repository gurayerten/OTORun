import time
import threading
import statistics
import copy
import requests
import pandas as pd
from requests.adapters import HTTPAdapter

from config import SETTINGS
from logger import get_logger

_log = get_logger("exchange")


class RateLimiter:
    """Tüm thread'ler arasında paylaşılan, tek noktadan istek hızını sınırlayan kilit.
    Eski botta her thread kendi başına time.sleep yapıyordu; 5 thread x 37 sembol x
    6-7 endpoint kombinasyonunda bu, Binance'ın 429 (rate limit) cevabına çok daha
    kolay çarpıyordu."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


_rl = RateLimiter(SETTINGS.MIN_REQUEST_INTERVAL)
_session = requests.Session()
_pool_size = max(1, int(getattr(SETTINGS, "HTTP_POOL_SIZE", 32)))
_http_adapter = HTTPAdapter(pool_connections=_pool_size, pool_maxsize=_pool_size, pool_block=True)
_session.mount("https://", _http_adapter)
_session.mount("http://", _http_adapter)
_REQUEST_STATS_LOCK = threading.Lock()
_REQUEST_STATS = {}
_MARKET_CACHE_LOCK = threading.Lock()
_MARKET_CACHE = {}


def get_request_stats():
    """Snapshot actual HTTP attempt counts by endpoint path (retries included)."""
    with _REQUEST_STATS_LOCK:
        return dict(_REQUEST_STATS)


def _get_cached(cache_key, fetcher):
    ttl = max(0.0, float(getattr(SETTINGS, "MARKET_DATA_CACHE_TTL_SECONDS", 30.0)))
    if ttl <= 0:
        return fetcher()
    with _MARKET_CACHE_LOCK:
        now = time.monotonic()
        cached = _MARKET_CACHE.get(cache_key)
        if cached and now < cached[0]:
            return copy.deepcopy(cached[1])
        # Concurrent callers share one successful response.
        data = fetcher()
        _MARKET_CACHE[cache_key] = (time.monotonic() + ttl, copy.deepcopy(data))
        return copy.deepcopy(data)


def _get(path, params=None, base_url=None):
    """HTTP GET with exponential backoff retry.
    429 (Rate Limit), 418 (IP Ban uyarısı), 5xx (sunucu hatası) durumlarında
    otomatik yeniden deneme yapar. Binance Retry-After header'ına uyar."""
    url = f"{base_url or SETTINGS.BASE_URL}{path}"
    last_exc = None

    for attempt in range(SETTINGS.MAX_RETRIES + 1):
        _rl.wait()
        try:
            endpoint_key = f"{base_url or 'futures'}{path}"
            with _REQUEST_STATS_LOCK:
                _REQUEST_STATS[endpoint_key] = _REQUEST_STATS.get(endpoint_key, 0) + 1
            r = _session.get(url, params=params, timeout=SETTINGS.REQUEST_TIMEOUT)

            # Başarılı yanıt
            if r.status_code == 200:
                return r.json()

            # Rate limit veya IP ban uyarısı — retry yapılabilir
            if r.status_code in (429, 418):
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    delay = float(retry_after)
                else:
                    delay = SETTINGS.RETRY_BASE_DELAY * (2 ** attempt)
                _log.warning(
                    f"⚠️ Rate limit ({r.status_code}) — {path} | "
                    f"Deneme {attempt + 1}/{SETTINGS.MAX_RETRIES + 1} | "
                    f"{delay:.1f}s bekleniyor..."
                )
                time.sleep(delay)
                last_exc = requests.exceptions.HTTPError(
                    f"{r.status_code} {r.reason}", response=r
                )
                continue

            # Sunucu hatası (5xx) — retry yapılabilir
            if r.status_code >= 500:
                delay = SETTINGS.RETRY_BASE_DELAY * (2 ** attempt)
                _log.warning(
                    f"⚠️ Sunucu hatası ({r.status_code}) — {path} | "
                    f"Deneme {attempt + 1}/{SETTINGS.MAX_RETRIES + 1} | "
                    f"{delay:.1f}s bekleniyor..."
                )
                time.sleep(delay)
                last_exc = requests.exceptions.HTTPError(
                    f"{r.status_code} {r.reason}", response=r
                )
                continue

            # Diğer hatalar (4xx) — retry anlamsız, hemen hata fırlat
            r.raise_for_status()

        except requests.exceptions.ConnectionError as e:
            delay = SETTINGS.RETRY_BASE_DELAY * (2 ** attempt)
            _log.warning(
                f"⚠️ Bağlantı hatası — {path} | "
                f"Deneme {attempt + 1}/{SETTINGS.MAX_RETRIES + 1} | "
                f"{delay:.1f}s bekleniyor... ({e})"
            )
            time.sleep(delay)
            last_exc = e
            continue

        except requests.exceptions.Timeout as e:
            delay = SETTINGS.RETRY_BASE_DELAY * (2 ** attempt)
            _log.warning(
                f"⚠️ Zaman aşımı — {path} | "
                f"Deneme {attempt + 1}/{SETTINGS.MAX_RETRIES + 1} | "
                f"{delay:.1f}s bekleniyor..."
            )
            time.sleep(delay)
            last_exc = e
            continue

    # Tüm denemeler tükendi
    _log.error(f"❌ {SETTINGS.MAX_RETRIES + 1} deneme sonrası başarısız: {path}")
    if last_exc:
        raise last_exc
    raise requests.exceptions.HTTPError(f"Request failed after {SETTINGS.MAX_RETRIES + 1} attempts: {path}")


def get_exchange_info():
    """Binance USDT-M exchangeInfo yanıtını merkezi HTTP katmanından döner."""
    return _get("/fapi/v1/exchangeInfo")


def get_trading_symbols():
    """USDT-M perpetual, şu an TRADING durumunda olan tüm semboller.
    Hardcoded liste yok — exchangeInfo'dan canlı çekilir.
    MAX_SYMBOLS varsa: alfabetik sıralama yerine hacim+momentum
    composite ranking ile en iyi sembolleri seçer."""
    data = get_exchange_info()
    valid_set = set()
    for s in data.get("symbols", []):
        if (
            s.get("quoteAsset") == SETTINGS.QUOTE_ASSET
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s["symbol"] not in SETTINGS.SYMBOL_BLACKLIST
        ):
            valid_set.add(s["symbol"])

    if not SETTINGS.MAX_SYMBOLS:
        return sorted(valid_set)

    # --- Composite ranking: hacim + momentum ---
    try:
        tickers = get_24hr_tickers()
    except Exception as e:
        _log.warning(f"24hr ticker alınamadı, alfabetik sıralama kullanılıyor: {e}")
        return sorted(valid_set)[: SETTINGS.MAX_SYMBOLS]

    rows = []
    for t in tickers:
        sym = t.get("symbol")
        if sym not in valid_set:
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
            pct = abs(float(t.get("priceChangePercent", 0)))
        except (TypeError, ValueError):
            continue
        # Minimum hacim filtresi (screener ile aynı eşik)
        if qv < SETTINGS.SCREENER_MIN_QUOTE_VOLUME:
            continue
        rows.append({"symbol": sym, "quote_volume": qv, "momentum": pct})

    if not rows:
        _log.warning("Composite ranking için yeterli veri yok, alfabetik sıralama")
        return sorted(valid_set)[: SETTINGS.MAX_SYMBOLS]

    # Min-max normalize et
    vols = [r["quote_volume"] for r in rows]
    moms = [r["momentum"] for r in rows]
    v_lo, v_hi = min(vols), max(vols)
    m_lo, m_hi = min(moms), max(moms)
    v_rng = v_hi - v_lo if v_hi - v_lo > 1e-12 else 1.0
    m_rng = m_hi - m_lo if m_hi - m_lo > 1e-12 else 1.0

    for r in rows:
        v_norm = (r["quote_volume"] - v_lo) / v_rng
        m_norm = (r["momentum"] - m_lo) / m_rng
        r["composite"] = (
            SETTINGS.BRUTE_SORT_W_VOLUME * v_norm
            + SETTINGS.BRUTE_SORT_W_MOMENTUM * m_norm
        )

    rows.sort(key=lambda r: r["composite"], reverse=True)
    selected = [r["symbol"] for r in rows[: SETTINGS.MAX_SYMBOLS]]

    _log.info(
        f"📋 Composite ranking: {len(rows)} sembolden {len(selected)} seçildi "
        f"(hacim×{SETTINGS.BRUTE_SORT_W_VOLUME:.0%} + momentum×{SETTINGS.BRUTE_SORT_W_MOMENTUM:.0%})"
    )
    return selected


def _to_df(raw):
    if not raw:
        return None
    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tb_base", "tb_quote", "ignore",
        ],
    )
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c])
    df["taker_buy"] = pd.to_numeric(df["tb_base"])  # agresif alici hacmi (gercek delta icin)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df[["open_time", "open", "high", "low", "close", "volume", "taker_buy"]].reset_index(drop=True)


def get_klines(symbol, interval=None, limit=None):
    interval = interval or SETTINGS.INTERVAL
    limit = limit or SETTINGS.KLINES_LIMIT
    raw = _get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    return _to_df(raw)


def get_raw_klines(symbol, interval, limit, **extra_params):
    """Fetch raw kline rows through the shared HTTP layer (pagination/analysis stays with caller)."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    params.update(extra_params)
    return _get("/fapi/v1/klines", params)


def klines_to_dataframe(raw):
    """Convert Binance raw kline rows to the project's canonical DataFrame schema."""
    return _to_df(raw)


def get_orderbook(symbol, limit=500):
    """Binance L2 Emir defterini (Order Book) döndürür."""
    return _get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})


def analyze_orderbook_imbalance(symbol, current_price, direction, depth_pct=None, threshold=None):
    """Belirli bir derinlikteki (örn: %1) emir defteri dengesizliğini analiz eder."""
    depth_pct = depth_pct or SETTINGS.OB_IMBALANCE_DEPTH_PCT
    threshold = threshold or SETTINGS.OB_IMBALANCE_RATIO_THRESHOLD
    
    ob = get_orderbook(symbol, limit=500)
    if not ob:
        return None
        
    bid_vol = 0.0
    ask_vol = 0.0
    
    # Bids (Alım Emirleri - fiyatın altında)
    for b in ob.get("bids", []):
        price, qty = float(b[0]), float(b[1])
        if price >= current_price * (1 - depth_pct / 100.0):
            bid_vol += qty
            
    # Asks (Satış Emirleri - fiyatın üstünde)
    for a in ob.get("asks", []):
        price, qty = float(a[0]), float(a[1])
        if price <= current_price * (1 + depth_pct / 100.0):
            ask_vol += qty
            
    if bid_vol == 0 and ask_vol == 0:
        return None
        
    ratio = bid_vol / ask_vol if ask_vol > 0 else 999.0 if bid_vol > 0 else 1.0
    
    favorable = False
    if direction == 1 and ratio >= threshold:
        favorable = True
    elif direction == -1:
        inv_ratio = ask_vol / bid_vol if bid_vol > 0 else 999.0 if ask_vol > 0 else 1.0
        if inv_ratio >= threshold:
            favorable = True
        ratio = inv_ratio  # Dışarıya hep ilgili yönün oranını dönmek için
        
    return {
        "bid_vol": bid_vol,
        "ask_vol": ask_vol,
        "ratio": ratio,
        "favorable": favorable
    }


def _compute_weighted_spread(bids, asks, n_levels=10):
    """İlk N seviyenin hacim-ağırlıklı ortalama spread'ini hesaplar.
    Döner: weighted_spread (float) — düşük = tight spread (iyi)."""
    if not bids or not asks:
        return float('inf')
    top_bids = bids[:n_levels]
    top_asks = asks[:n_levels]
    best_bid = float(top_bids[0][0])
    best_ask = float(top_asks[0][0])
    if best_bid <= 0 or best_ask <= 0:
        return float('inf')
    # Basit spread: (best_ask - best_bid) / mid_price
    mid = (best_bid + best_ask) / 2.0
    raw_spread = (best_ask - best_bid) / mid
    # Hacim-ağırlıklı: ilk N seviyedeki toplam hacim ne kadar yoğun
    bid_depth = sum(float(b[1]) * float(b[0]) for b in top_bids)  # USDT cinsinden
    ask_depth = sum(float(a[1]) * float(a[0]) for a in top_asks)
    # Düşük depth = geniş etkin spread → ceza
    depth_factor = 1.0 + (1.0 / max(bid_depth + ask_depth, 1.0)) * 1e6
    return raw_spread * depth_factor


def analyze_orderbook_persistent(symbol, current_price, direction,
                                  depth_pct=None, threshold=None,
                                  snapshots=None, interval_ms=None):
    """Anti-spoofing order book analizi: birden fazla ardışık snapshot alarak
    emrin kalıcılığını, spread kalitesini ve likidite derinliğini ölçer.

    Döner:
        dict veya None. Dict içeriği:
        - bid_vol, ask_vol, ratio, favorable: mevcut imbalance bilgisi
        - persistent: bool — CV < eşik ise True (kalıcı, güvenilir)
        - cv: float — varyasyon katsayısı (düşük = kalıcı)
        - spread_quality: 'tight' | 'normal' | 'wide'
        - depth_quality: 'deep' | 'normal' | 'shallow'
        - effective_multiplier: float — kalıcılık + spread + derinliğe göre skor çarpanı
    """
    depth_pct = depth_pct or SETTINGS.OB_IMBALANCE_DEPTH_PCT
    threshold = threshold or SETTINGS.OB_IMBALANCE_RATIO_THRESHOLD
    snapshots = snapshots or SETTINGS.OB_PERSISTENCE_SNAPSHOTS
    interval_ms = interval_ms or SETTINGS.OB_PERSISTENCE_INTERVAL_MS
    interval_s = interval_ms / 1000.0

    ratios = []
    bid_vols = []
    ask_vols = []
    spreads = []
    depths_usd = []

    for snap_i in range(snapshots):
        ob = get_orderbook(symbol, limit=500)
        if not ob:
            if snap_i == 0:
                return None  # İlk snapshot bile alınamadıysa bırak
            continue

        bid_vol = 0.0
        ask_vol = 0.0
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])

        for b in bids:
            price, qty = float(b[0]), float(b[1])
            if price >= current_price * (1 - depth_pct / 100.0):
                bid_vol += qty
        for a in asks:
            price, qty = float(a[0]), float(a[1])
            if price <= current_price * (1 + depth_pct / 100.0):
                ask_vol += qty

        if bid_vol == 0 and ask_vol == 0:
            if snap_i == 0:
                return None
            continue

        if direction == 1:
            ratio = bid_vol / ask_vol if ask_vol > 0 else 999.0 if bid_vol > 0 else 1.0
        else:
            ratio = ask_vol / bid_vol if bid_vol > 0 else 999.0 if ask_vol > 0 else 1.0

        ratios.append(ratio)
        bid_vols.append(bid_vol)
        ask_vols.append(ask_vol)

        # Spread kalitesi
        ws = _compute_weighted_spread(bids, asks, SETTINGS.OB_SPREAD_DEPTH_LEVELS)
        spreads.append(ws)

        # Derinlik (USDT cinsinden toplam likidite)
        total_depth = (bid_vol + ask_vol) * current_price
        depths_usd.append(total_depth)

        # Son snapshot değilse bekle
        if snap_i < snapshots - 1:
            time.sleep(interval_s)

    if not ratios:
        return None

    # --- Kalıcılık: CV (Coefficient of Variation) ---
    mean_ratio = statistics.mean(ratios)
    if len(ratios) >= 2 and mean_ratio > 0:
        std_ratio = statistics.stdev(ratios)
        cv = std_ratio / mean_ratio
    else:
        cv = 0.0  # Tek snapshot — kalıcı say

    persistent = cv < SETTINGS.OB_PERSISTENCE_CV_THRESHOLD

    # --- Spread kalitesi ---
    avg_spread = statistics.mean(spreads) if spreads else float('inf')
    if avg_spread < 0.0005:       # %0.05'ten dar
        spread_quality = "tight"
    elif avg_spread < 0.002:      # %0.2'den dar
        spread_quality = "normal"
    else:
        spread_quality = "wide"

    # --- Derinlik kalitesi ---
    avg_depth = statistics.mean(depths_usd) if depths_usd else 0.0
    if avg_depth > 500_000:       # 500K USDT üzeri
        depth_quality = "deep"
    elif avg_depth > 100_000:     # 100K USDT üzeri
        depth_quality = "normal"
    else:
        depth_quality = "shallow"

    # --- Efektif çarpan hesapla ---
    effective_multiplier = 1.0
    if not persistent:
        effective_multiplier *= SETTINGS.OB_SPOOFING_DISCOUNT
        _log.debug(f"{symbol} OB spoofing riski: CV={cv:.3f} > {SETTINGS.OB_PERSISTENCE_CV_THRESHOLD}, "
                   f"çarpan → {effective_multiplier:.2f}")

    favorable = mean_ratio >= threshold

    return {
        "bid_vol": statistics.mean(bid_vols),
        "ask_vol": statistics.mean(ask_vols),
        "ratio": mean_ratio,
        "favorable": favorable,
        "persistent": persistent,
        "cv": cv,
        "spread_quality": spread_quality,
        "depth_quality": depth_quality,
        "avg_depth_usd": avg_depth,
        "effective_multiplier": effective_multiplier,
        "snapshots_taken": len(ratios),
    }


def get_24hr_tickers():
    """TÜM semboller için 24s istatistiklerini TEK istekte döner: hacim, fiyat
    değişimi vb. Screener'ın 1. aşaması bu tek çağrı üzerine kurulu — binlerce
    sembolü tek tek sorgulamak yerine."""
    return _get_cached("futures:/fapi/v1/ticker/24hr", lambda: _get("/fapi/v1/ticker/24hr"))


def get_open_interest(symbol):
    data = _get("/fapi/v1/openInterest", {"symbol": symbol})
    return float(data.get("openInterest", 0)) if data else 0.0


def get_open_interest_history(symbol, period, limit=2):
    """Returns historical OI points for one symbol through the shared HTTP layer."""
    return _get("/futures/data/openInterestHist", {
        "symbol": symbol,
        "period": period,
        "limit": limit,
    })


def get_top_tickers_by_volume(n, exclude=()):
    """Bir ticker çağrısıyla en yüksek quote-volume'lu USDT ticker satırlarını seçer."""
    excluded = set(exclude or ())
    excluded.update(getattr(SETTINGS, "SYMBOL_BLACKLIST", ()))
    excluded.update(getattr(SETTINGS, "NON_STANDARD_SYMBOLS", ()))
    candidates = []
    tickers = get_24hr_tickers()
    if not isinstance(tickers, list):
        raise ValueError("24hr ticker yanıtı liste değil")
    for ticker in tickers:
        if not isinstance(ticker, dict):
            continue
        symbol = ticker.get("symbol")
        if not symbol or not symbol.endswith("USDT") or symbol in excluded:
            continue
        try:
            volume = float(ticker.get("quoteVolume", 0))
        except (TypeError, ValueError) as e:
            _log.debug(f"Top-volume ticker parse hatası ({symbol}): {e}")
            continue
        if volume > 0:
            candidates.append((volume, ticker))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [ticker for _, ticker in candidates[:max(0, int(n))]]


def get_top_symbols_by_volume(n, exclude=()):
    """Bir ticker çağrısıyla en yüksek quote-volume'lu USDT sembollerini seçer."""
    return [ticker["symbol"] for ticker in get_top_tickers_by_volume(n, exclude=exclude)]


def get_global_long_short_ratio(symbol, period):
    return _get("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": period, "limit": 1})


def get_top_long_short_account_ratio(symbol, period):
    return _get("/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": period, "limit": 1})


def get_top_long_short_position_ratio(symbol, period):
    return _get("/futures/data/topLongShortPositionRatio", {"symbol": symbol, "period": period, "limit": 1})


def get_taker_long_short_ratio(symbol, period, limit=1):
    return _get("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": period, "limit": limit})


def get_premium_index(symbol=None):
    if not symbol:
        return get_all_premium_index()
    params = {"symbol": symbol} if symbol else None
    return _get("/fapi/v1/premiumIndex", params)


def get_daily_klines(symbol, days):
    raw = _get("/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": days})
    return _to_df(raw)


def get_klines_paginated(symbol, interval, days):
    """Backtest için 1500 bar limitinin ötesine sayfalama ile gider."""
    bars_per_day = {"15m": 96, "1h": 24, "4h": 6}.get(interval, 96)
    needed = days * bars_per_day
    out = []
    end_time = None
    while len(out) < needed:
        params = {"symbol": symbol, "interval": interval, "limit": 1000}
        if end_time:
            params["endTime"] = end_time
        raw = _get("/fapi/v1/klines", params)
        if not raw:
            break
        out = raw + out
        end_time = raw[0][0] - 1
        if len(raw) < 1000:
            break
    if not out:
        return None
    return _to_df(out[-needed:])


def get_htf_klines(symbol):
    """HTF trend filtresi için üst zaman dilimi mumlarını çeker."""
    return get_klines(symbol, interval=SETTINGS.HTF_INTERVAL, limit=SETTINGS.HTF_KLINES_LIMIT)


def get_all_premium_index():
    """Tüm USDT-M kontratların funding rate + mark price bilgisini TEK çağrıda döner.
    Funding Radar modülü tarafından kullanılır."""
    return _get_cached("futures:/fapi/v1/premiumIndex", lambda: _get("/fapi/v1/premiumIndex"))


def get_all_spot_tickers():
    """Tüm spot piyasanın 24s istatistiklerini TEK çağrıda döner.
    Funding Radar tarafından spot hacim verisi için kullanılır.
    NOT: Spot API farklı base URL kullanır (api.binance.com)."""
    try:
        return _get("/api/v3/ticker/24hr", base_url="https://api.binance.com")
    except Exception as e:
        _log.warning(f"Spot ticker verisi alınamadı: {e}")
        return []


def get_funding_rate(symbol):
    """Son funding rate'i döner."""
    data = _get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
    if data and len(data) > 0:
        return float(data[0].get("fundingRate", 0))
    return 0.0


def get_funding_history(symbol):
    """Funding rate history'si — squeeze tespiti için.
    8 saatte bir güncellenir, limit=21 son ~7 günü kapsar."""
    data = _get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": SETTINGS.FUNDING_HISTORY_LIMIT})
    if not data or not isinstance(data, list):
        return []
    rates = []
    for x in data:
        try:
            rates.append(float(x.get("fundingRate", 0)))
        except (ValueError, TypeError):
            continue
    return rates


def analyze_funding_squeeze(symbol):
    """Funding rate analizi — squeeze riski ve trend.
    Dönen dict: funding_rate, avg_funding, squeeze_risk, funding_trend"""
    result = {
        "funding_rate": 0.0,
        "avg_funding": 0.0,
        "squeeze_risk": None,       # 'LONG_SQUEEZE' | 'SHORT_SQUEEZE' | None
        "funding_trend": "NEUTRAL", # 'BULLISH' | 'BEARISH' | 'NEUTRAL'
        "extreme_funding": False,
    }
    try:
        rates = get_funding_history(symbol)
        if len(rates) < 3:
            return result

        current = rates[-1]  # En son funding (API artan zamana göre döner)
        result["funding_rate"] = current
        result["avg_funding"] = sum(rates) / len(rates)
        result["extreme_funding"] = abs(current) > SETTINGS.FUNDING_EXTREME_THRESHOLD

        # Funding trend: en yenisi en eskisiyle karşılaştırılır
        if current > rates[0]:
            result["funding_trend"] = "BULLISH"
        elif current < rates[0]:
            result["funding_trend"] = "BEARISH"

        # Squeeze riski: artan funding + aşırı seviye
        increasing = len(rates) >= 3 and rates[-1] > rates[-2] > rates[-3]
        if increasing:
            if current > SETTINGS.FUNDING_HIGH_THRESHOLD:
                result["squeeze_risk"] = "LONG_SQUEEZE"   # Aşırı long → short squeeze potansiyeli
            elif current < -SETTINGS.FUNDING_HIGH_THRESHOLD:
                result["squeeze_risk"] = "SHORT_SQUEEZE"  # Aşırı short → long squeeze potansiyeli
    except Exception as e:
        _log.warning(f"Funding squeeze analizi yapılamadı ({symbol}): {e}")
    return result


def get_liquidation_data(symbol, limit=50):
    """Binance forceOrders API'den gerçek likidasyon verilerini çeker.
    Long/short likidasyon dengesizliği ve yoğun likidasyon cluster'larını döner."""
    result = {
        "long_liq_usd": 0.0,
        "short_liq_usd": 0.0,
        "total_liq_usd": 0.0,
        "liq_imbalance": 0.0,      # -1..+1 (pozitif = long ağırlıklı)
        "significant": False,       # 1M USD üstü
        "top_clusters": {},         # fiyat → USD
    }
    try:
        data = _get("/fapi/v1/forceOrders", {"symbol": symbol, "limit": limit})
        if not data or not isinstance(data, list):
            return result

        clusters = {}
        for order in data:
            if not isinstance(order, dict):
                continue
            side = order.get("side", "")
            try:
                qty = float(order.get("origQty", 0))
                price = float(order.get("price", 0))
            except (ValueError, TypeError) as e:
                _log.debug(f"Force order satırı parse edilemedi ({symbol}): {e}")
                continue
            usd_val = qty * price
            if side == "SELL":   # Long likidasyon
                result["long_liq_usd"] += usd_val
            elif side == "BUY":  # Short likidasyon
                result["short_liq_usd"] += usd_val

            price_level = round(price, 2)
            clusters[price_level] = clusters.get(price_level, 0) + usd_val

        result["total_liq_usd"] = result["long_liq_usd"] + result["short_liq_usd"]
        if result["total_liq_usd"] > 0:
            result["liq_imbalance"] = (result["long_liq_usd"] - result["short_liq_usd"]) / result["total_liq_usd"]
        result["significant"] = result["total_liq_usd"] > 1_000_000
        # En büyük 5 cluster
        result["top_clusters"] = dict(sorted(clusters.items(), key=lambda x: x[1], reverse=True)[:5])
    except Exception as e:
        _log.warning(f"Force order verisi alınamadı ({symbol}); endpoint kullanıcı hesabına ait olabilir: {e}")
    return result

