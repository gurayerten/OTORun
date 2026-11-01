import os
import warnings
from dataclasses import dataclass, field
from typing import Literal

# Load .env file manually if it exists
if os.path.exists(".env"):
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                # Explicit process environment takes precedence over local .env values.
                os.environ.setdefault(k.strip(), v.strip())
    except (OSError, UnicodeError) as e:
        warnings.warn(f".env dosyası okunamadı: {e}", RuntimeWarning)


@dataclass
class Settings:
    # --- Exchange ---
    BASE_URL: str = "https://fapi.binance.com"
    INTERVAL: str = "15m"    #5m
    KLINES_LIMIT: int = 500
    MAX_WORKERS: int = 16
    REQUEST_TIMEOUT: int = 10
    MIN_REQUEST_INTERVAL: float = 0.08  # global rate-limit guard (~12 req/s)
    HTTP_POOL_SIZE: int = 32
    MARKET_DATA_CACHE_TTL_SECONDS: float = 30.0
    MAX_RETRIES: int = 3                 # 429/5xx hatalarında maks. yeniden deneme
    RETRY_BASE_DELAY: float = 1.0        # Exponential backoff başlangıç süresi (saniye)

    # --- Symbol universe (dinamik, hardcoded liste YOK) ---
    QUOTE_ASSET: str = "USDT"
    SYMBOL_BLACKLIST: tuple = ()
    NON_STANDARD_SYMBOLS: tuple = ("BTCDOMUSDT",)  # endeks kontratı gibi gerçek coin olmayan semboller
    MAX_SYMBOLS: int = 60  # Brute-force mod (USE_SCREENER=False) için tavan
    BRUTE_SORT_W_VOLUME: float = 0.70     # Brute-force modda hacim sıralama ağırlığı
    BRUTE_SORT_W_MOMENTUM: float = 0.30   # Brute-force modda momentum sıralama ağırlığı

    # --- Screener: "doğru parite" seçimi ---
    USE_SCREENER: bool = True
    SCREENER_MIN_QUOTE_VOLUME: float = 5_000_000  # 24s USDT hacim tabanı (likidite filtresi)
    SCREENER_SHORTLIST_SIZE: int = 220    # Aşama 1: hacim+momentum ile ön eleme
    SCREENER_FINAL_COUNT: int = 150       # Aşama 3: nihai taranacak parite sayısı
    SCREENER_ALWAYS_INCLUDE: tuple = ("BTCUSDT", "ETHUSDT")  # her zaman taransın
    SCREENER_VOLUME_BASELINE_DAYS: int = 7  # hacim sıçraması için günlük ortalama penceresi
    SCREENER_OI_HISTORY_FILE: str = "state/oi_history.json"
    SCREENER_W_MOMENTUM: float = 0.35
    SCREENER_W_VOLUME_SURGE: float = 0.35
    SCREENER_W_OI_CHANGE: float = 0.30

    # --- Strateji: likidite süpürmesi ---
    SWING_LENGTH: int = 21
    MAX_SWEEP_DISTANCE_BARS: int = 80
    MIN_SWEEP_SCORE: float = 50.0
    REQUIRE_CHOCH: bool = True
    MINOR_PIVOT_LENGTH: int = 8
    CONFIRM_WINDOW_BARS: int = 13

    # --- ImpulseEngine (Breakout + Volume Spike + BoS Breakout) ---
    # Geçmiş analizde 333 işlemde -138R üretmişti. Yeni sonuçlar ayrı backtestte
    # pozitif beklenti göstermeden canlı sinyallere dahil edilmemeli.
    USE_IMPULSE_ENGINE: bool = False
    # Sweep setup kapıları ImpulseEngine'e otomatik uygulanmaz; Impulse için
    # ayrıca kapatılması gereken setup türleri burada belirtilir.
    DISABLED_IMPULSE_SETUP_TYPES: tuple = ()
    MIN_IMPULSE_SCORE: float = 80.0            # Minimum sinyal skoru (SweepEngine'den biraz yüksek)
    IMPULSE_VOL_SPIKE_MULT: float = 3.0        # Volume spike eşiği (MA'nın kaç katı)
    IMPULSE_VOL_SPIKE_BONUS: float = 15.0      # Volume spike skor bonusu
    IMPULSE_BREAKOUT_BONUS: float = 12.0       # Breakout onayı skor bonusu
    IMPULSE_BOS_BONUS: float = 10.0            # BoS onayı skor bonusu
    IMPULSE_REGIME_EXPANSION_BONUS: float = 8.0   # EXPANSION rejim bonusu
    IMPULSE_REGIME_COMPRESSION_BONUS: float = 5.0 # COMPRESSION → kırılım bonusu
    IMPULSE_EMA_CROSS_BONUS: float = 6.0       # EMA cross onay bonusu
    IMPULSE_ENGULFING_BONUS: float = 5.0       # Engulfing pattern bonusu
    IMPULSE_RANGE_PENALTY: float = 15.0        # RANGE rejim cezası
    IMPULSE_CONTRA_TREND_PENALTY: float = 10.0 # ST+PMax çelişki cezası
    IMPULSE_MIN_BAR_MOVE_PCT: float = 1.5      # Minimum tek-bar fiyat hareketi %
    IMPULSE_LOOKBACK_COMPRESSION: int = 20     # Compression tespiti için geriye bakış
    IMPULSE_COMPRESSION_BB_SQUEEZE: float = 0.6  # BB squeeze oranı eşiği
    IMPULSE_WICK_PENALTY_THRESHOLD: float = 0.6  # Fitil oranı bu üstündeyse ceza
    IMPULSE_WICK_PENALTY: float = 5.0          # Fakeout fitil cezası

    USE_VOLUME_FILTER: bool = True
    VOLUME_MA_LEN: int = 21
    VOLUME_SPIKE_MULT: float = 1.5

    # --- Piyasa Rejimi Tespiti (Range vs Trend) ---
    USE_MARKET_REGIME: bool = True             # Piyasa rejimi tespiti aktif
    REGIME_BB_LENGTH: int = 20                 # Bollinger Band periyodu
    REGIME_BB_MULT: float = 2.0                # Bollinger Band standart sapma çarpanı
    REGIME_BB_SQUEEZE_PERCENTILE: float = 20.0 # BB genişliği bu persentilin altındaysa squeeze
    REGIME_ADX_RANGE_THRESHOLD: float = 20.0   # ADX bu değerin altındaysa range sinyali
    REGIME_ADX_TREND_THRESHOLD: float = 25.0   # ADX bu değerin üstündeyse trend sinyali
    REGIME_RANGE_SCORE_PENALTY: float = 10.0   # Range modunda skor cezası
    REGIME_RANGE_SCORE_THRESHOLD_ADD: float = 10.0  # Range modunda MIN_SWEEP_SCORE'a eklenen miktar

    # --- HTF (Higher Time-Frame) trend filtresi ---
    # Analiz: 1h EMA50'ye karşı açılan işlemler kayıp yoğunlukluydu → açıldı.
    USE_HTF_FILTER: bool = True         # True yapılırsa trendine karşı sinyaller bastırılır
    HTF_INTERVAL: str = "1h"            # Üst zaman dilimi
    HTF_EMA_LENGTH: int = 50            # EMA uzunluğu
    HTF_KLINES_LIMIT: int = 60          # Çekilecek mum sayısı

    # --- Funding Rate & Likidasyon ---
    USE_FUNDING_FILTER: bool = True     # Funding rate squeeze tespiti
    FUNDING_EXTREME_THRESHOLD: float = 0.005   # ±%0.5 aşırı funding
    FUNDING_HIGH_THRESHOLD: float = 0.002      # ±%0.2 yüksek funding
    FUNDING_HISTORY_LIMIT: int = 21            # Funding history bar sayısı (8s aralıklı)
    USE_LIQUIDATION_DATA: bool = False  # forceOrders USER_DATA imza altyapısı eklenene kadar kapalı

    # --- Funding Rate Squeeze Radarı ---
    USE_FUNDING_RADAR: bool = True
    FUNDING_RADAR_MIN_RATE: float = 0.01  # |rate| >= %1 filtre eşiği (Binance ondalık oran: 0.01 = %1; %0.05 = 0.0005)
    FUNDING_RADAR_TOP_N: int = 10             # Her yön için kaç coin listelenir
    FUNDING_RADAR_INTERVAL_SEC: int = 3600    # Gönderim aralığı (saniye) — 1 saat
    FUNDING_RADAR_ENRICH: bool = True         # OI, Liq, Spot Hacim, CVD, Teknik katmanlar
    FUNDING_RADAR_OI_PERIOD: str = "1h"       # OI değişim karşılaştırma periyodu
    FUNDING_RADAR_CVD_BARS: int = 4           # CVD hesaplama bar sayısı (1h mumlardan)
    FUNDING_RADAR_TECH_INTERVAL: str = "1h"   # Teknik analiz zaman dilimi
    FUNDING_RADAR_TECH_BARS: int = 60         # Teknik analiz mum sayısı (Supertrend + S/R)

    # --- Market-Wide Funding Pulse (Piyasa Geneli Nabız) ---
    FUNDING_PULSE_ENABLED: bool = True
    FUNDING_PULSE_INTERVAL_MIN: int = 60
    FUNDING_PULSE_EXTREME_PCT: float = 0.05
    FUNDING_PULSE_TOP_N: int = 5
    FUNDING_PULSE_MIN_QUOTE_VOLUME: float = 0
    FUNDING_PULSE_SEND_ONLY_ON_CHANGE: bool = True
    FUNDING_PULSE_CHANGE_EPS: float = 1e-6

    # --- Futures Volume Dominance (Piyasa Geneli Hacim Hakimiyeti) ---
    FUTURES_VOLUME_DOMINANCE_ENABLED: bool = True
    FUTURES_VOLUME_DOMINANCE_INTERVAL_MIN: int = 60
    FUTURES_VOLUME_DOMINANCE_TOP_N: int = 10
    FUTURES_VOLUME_DOMINANCE_MAJOR_SYMBOLS: tuple = ("BTCUSDT", "ETHUSDT")
    FUTURES_VOLUME_DOMINANCE_MIN_QUOTE_VOLUME: float = 0.0
    FUTURES_VOLUME_DOMINANCE_SEND_ONLY_ON_CHANGE: bool = True
    FUTURES_VOLUME_DOMINANCE_CHANGE_EPS: float = 0.5

    # --- Exchange risk change watch ---
    RISK_WATCH_ENABLED: bool = True
    RISK_WATCH_INTERVAL_MIN: int = 240
    RISK_WATCH_LEVERAGE_ENABLED: bool = False
    RISK_WATCH_NOTIFY_NEW_SYMBOLS: bool = True
    RISK_WATCH_NOTIFY_STATUS_CHANGES: bool = True
    RISK_WATCH_NOTIFY_DELISTED: bool = True
    RISK_WATCH_FIRST_RUN_SILENT: bool = False
    RISK_WATCH_MAX_ITEMS_PER_MSG: int = 15

    # --- Open-interest market breadth ---
    OI_BREADTH_ENABLED: bool = True
    OI_BREADTH_INTERVAL_MIN: int = 30
    OI_BREADTH_WATCHLIST_SIZE: int = 30
    OI_BREADTH_NOISE_PCT: float = 0.03
    OI_BREADTH_TOP_N_HIGHLIGHT: int = 5
    OI_BREADTH_MIN_SYMBOLS: int = 10
    OI_BREADTH_SEND_ONLY_ON_CHANGE: bool = True
    OI_BREADTH_CHANGE_EPS: float = 2.0
    OI_BREADTH_CALL_DELAY_MS: int = 0
    OI_EVENT_NOISE_PCT: float = 0.03

    # --- Market sentiment radar ---
    SENTIMENT_RADAR_ENABLED: bool = True
    SENTIMENT_RADAR_INTERVAL_MIN: int = 60
    SENTIMENT_RADAR_WATCHLIST_SIZE: int = 12
    SENTIMENT_RADAR_PERIOD: str = "15m"
    SENTIMENT_RADAR_USE_GLOBAL_RATIO: bool = True
    SENTIMENT_RADAR_USE_TOP_ACCOUNT_RATIO: bool = True
    SENTIMENT_RADAR_USE_TOP_POSITION_RATIO: bool = True
    SENTIMENT_RADAR_USE_TAKER_RATIO: bool = True
    SENTIMENT_RADAR_DIVERGENCE_THRESHOLD: float = 0.3
    SENTIMENT_RADAR_TOP_N_DIVERGENCE: int = 5
    SENTIMENT_RADAR_SEND_ONLY_ON_CHANGE: bool = True
    SENTIMENT_RADAR_CHANGE_EPS: float = 0.05
    SENTIMENT_RADAR_STALENESS_TOLERANCE_MULT: int = 2

    # --- External broad-market sentiment context (Alternative.me) ---
    CRYPTO_FNG_ENABLED: bool = True
    CRYPTO_FNG_INTERVAL_MIN: int = 360
    CRYPTO_FNG_REQUEST_TIMEOUT: int = 10
    CRYPTO_FNG_MAX_AGE_HOURS: int = 48
    CRYPTO_FNG_ALERT_DELTA: int = 8
    CRYPTO_FNG_FIRST_RUN_NOTIFY: bool = True


    # --- RSI Divergence (opsiyonel skor bileşeni) ---
    USE_RSI_DIVERGENCE: bool = True     # RSI divergence skor bileşeni
    RSI_LENGTH: int = 14                # RSI periyodu
    RSI_DIVERGENCE_LOOKBACK: int = 20   # Divergence için geriye bakış
    RSI_DIVERGENCE_STRIDE: int = 5  # Kaç barda bir RSI divergence güncellenir (1 = her bar)

    # --- ADX & DI Filtre / Skor Ayarları ---
    USE_ADX_FILTER: bool = False
    USE_ADX_SCORE_BONUS: bool = True
    ADX_LENGTH: int = 14
    ADX_THRESHOLD: float = 20.0
    ADX_SCORE_BONUS: float = 5.0
    ADX_FILTER_MODE: Literal["range_only", "trend_direction"] = "trend_direction"  # "range_only" veya "trend_direction"

    # --- Supertrend Filtre / Skor Ayarları ---
    USE_SUPERTREND_FILTER: bool = False
    USE_SUPERTREND_SCORE_BONUS: bool = True
    SUPERTREND_PERIOD: int = 10
    SUPERTREND_MULTIPLIER: float = 3.0
    SUPERTREND_CHANGE_ATR: bool = True
    SUPERTREND_SCORE_BONUS: float = 5.0
    SUPERTREND_CONTRA_PENALTY: float = 5.0  # Supertrend sinyal yönüyle çelişirse ceza

    # --- PMax (Profit Maximizer) ---
    USE_PMAX: bool = True                      # PMax trend bileşeni aktif
    USE_PMAX_SCORE_BONUS: bool = True          # PMax trend uyumu skor bonusu
    USE_PMAX_FILTER: bool = False              # True ise sadece PMax yönüyle uyumlu sinyaller geçer
    PMAX_ATR_LENGTH: int = 10                  # ATR periyodu
    PMAX_ATR_MULTIPLIER: float = 3.0           # ATR çarpanı
    PMAX_MA_TYPE: Literal["SMA", "EMA", "WMA", "TMA", "VAR", "WWMA", "ZLEMA", "TSF"] = "EMA"                  # MA tipi: SMA/EMA/WMA/TMA/VAR/WWMA/ZLEMA/TSF
    PMAX_MA_LENGTH: int = 10                   # MA uzunluğu
    PMAX_CHANGE_ATR: bool = True               # ATR hesaplama yöntemi (True=EMA, False=SMA)
    PMAX_NORMALIZE: bool = False               # ATR normalizasyonu (close'a bölme)
    PMAX_SCORE_BONUS: float = 5.0              # PMax trend uyumu skor bonusu
    PMAX_CROSS_BONUS: float = 3.0              # Son N bar içinde MAvg×PMax çaprazı varsa ek bonus
    PMAX_CROSS_LOOKBACK: int = 5               # Çapraz arama penceresi (bar)
    PMAX_CONTRA_PENALTY: float = 4.0           # PMax sinyal yönüyle çelişirse ceza

    # --- Risk yönetimi ---
    ATR_LEN: int = 14
    # Analiz: 0.25 ATR tamponu 5m gürültüsüne çok yakındı; kayıpların çoğu
    # girişten 5-13 bar sonra SL'yi çarpıyordu → tampon genişletildi.
    SL_BUFFER_ATR: float = 0.45
    TP1_R: float = 1.0
    TP2_R: float = 2.0
    TP3_R: float = 3.0
    USE_BREAK_EVEN: bool = True
    # TP1 sonrası SL girişe değil entry ± BE_LEVEL_R×risk seviyesine çekilir.
    # Analiz: TP1'e değip sonra girişte kesilen (BE) 10+ trade vardı; artık
    # TP1 sonrası çıkışlar en az yarım R kilitler.
    BE_LEVEL_R: float = 0.5
    # Sonuç sınıflandırma eşiği: |r| bu değerin altındaysa BREAKEVEN sayılır.
    # DİKKAT: BE_LEVEL_R kilitli çıkışlar +BE_LEVEL_R ile kapandığı için eşik
    # BE_LEVEL_R'in altındaysa "BE" çıkışları küçük WIN olarak sayılır (win
    # rate'i şişirir). BE çıkışlarını nötr saymak için eşiği BE_LEVEL_R üstüne alın.
    BE_OUTCOME_THRESHOLD: float = 0.3

    # --- Trailing Stop-Loss ---
    USE_TRAILING_STOP: bool = True
    TRAILING_STOP_ACTIVATION_R: float = 1.0    # TP1'e yani 1.0 R kâra ulaşınca aktif olur
    # Analiz: 1.0R trail 5m gürültüsünün içinde kalıyor, kazananları BE'ye
    # çeviriyordu → genişletildi.
    TRAILING_STOP_DISTANCE_R: float = 1.5      # Görülen en yüksek/düşük fiyattan 1.5 R geride takip eder

    # --- Time Stop (maksimum bekleme) ---
    # Analiz: THETA 525 bar (~44 saat) tutulup SL; AIA 497; RE 286 bar.
    # MAX_HOLD_BARS sonunda kâr >= TIME_STOP_MIN_R değilse pozisyon kapatılır.
    MAX_HOLD_BARS: int = 96
    TIME_STOP_MIN_R: float = 0.5

    MIN_SL_DISTANCE_ATR: float = 0.5
    MAX_SL_DISTANCE_PCT: float = 5.0           # SL mesafesi üst sınırı (entry fiyatının %'si)
    MIN_SL_DISTANCE_PCT: float = 0.8           # SL mesafesi alt sınırı (entry fiyatının %'si)

    # --- Breakout Finder ---
    USE_BREAKOUT_FILTER: bool = True       # Breakout yön doğrulaması
    BREAKOUT_PRD: int = 5                  # Pivot periyodu
    BREAKOUT_BO_LEN: int = 200             # Maksimum kırılma mesafesi (bar)
    BREAKOUT_CWIDTH_PCT: float = 3.0       # Eşik genişliği (%)
    BREAKOUT_MINTEST: int = 2              # Minimum test sayısı
    BREAKOUT_SCORE_BONUS: float = 5.0      # Skor bonusu (kırılma aynı yöndeyse)

    # --- Order Book (L2) Imbalance ---
    USE_ORDERBOOK_IMBALANCE: bool = True
    OB_IMBALANCE_DEPTH_PCT: float = 1.0        # Analiz edilecek derinlik (Current price ±%1)
    OB_IMBALANCE_RATIO_THRESHOLD: float = 1.5  # Bids > 1.5x Asks (veya tam tersi) eşiği
    OB_IMBALANCE_SCORE_BONUS: float = 5.0      # Emir defteri işlemi destekliyorsa +5 puan
    OB_IMBALANCE_PENALTY: float = 5.0          # Emir defteri işlemi desteklemiyorsa -5 puan

    # --- Order Book Kalıcılık (Anti-Spoofing) ---
    OB_PERSISTENCE_SNAPSHOTS: int = 3              # Kaç ardışık snapshot alınacak
    OB_PERSISTENCE_INTERVAL_MS: int = 500          # Snapshotlar arası bekleme (ms)
    OB_PERSISTENCE_CV_THRESHOLD: float = 0.30      # CV (varyasyon katsayısı) bu altındaysa "kalıcı"
    OB_SPREAD_DEPTH_LEVELS: int = 10               # Hacim-ağırlıklı spread için ilk N seviye
    OB_SPREAD_TIGHT_BONUS: float = 2.0             # Tight spread bonusu
    OB_DEPTH_SHALLOW_PENALTY: float = 3.0          # Sığ derinlik (yetersiz likidite) cezası
    OB_SPOOFING_DISCOUNT: float = 0.5              # Spoofing riski varsa imbalance skoruna çarpan

    # --- Volume Profile ---
    USE_VOLUME_PROFILE: bool = True        # PoC/VAH/VAL hesapla
    VP_NUM_BARS: int = 100                 # Fiyat seviyesi sayısı
    VP_LOOKBACK: int = 200                 # Geriye bakış (bar)
    VP_VA_PCT: float = 68.0               # Value Area yüzdesi
    VP_POC_SCORE_BONUS: float = 3.0        # Entry PoC'a yakınsa skor bonusu

    # --- Linear Regression Oscillator ---
    USE_LINREG_OSC: bool = True            # Normalize linreg osc. hesapla
    LINREG_LENGTH: int = 20                # Regresyon pencere uzunluğu
    LINREG_UPPER: float = 1.5             # Aşırı alım eşiği (z-score)
    LINREG_LOWER: float = -1.5            # Aşırı satım eşiği (z-score)
    LINREG_PENALTY: float = 5.0           # Aşırı alım/satımda skor cezası

    # --- Dynamic Swing Anchored VWAP ---
    USE_SWING_VWAP: bool = True            # Swing VWAP trend filtresi
    SWING_VWAP_PRD: int = 50              # Swing tespiti için lookback
    SWING_VWAP_APT: float = 20.0          # Adaptive Price Tracking (EWMA ağırlığı)
    SWING_VWAP_SCORE_BONUS: float = 4.0   # VWAP trend uyumu (#8 confluence)
    SWING_VWAP_CONTRA_PENALTY: float = 4.0 # VWAP sinyal çelişki (#8 confluence)

    # --- True Strength Index (TSI) ---
    USE_TSI: bool = True                   # TSI momentum bileşeni
    TSI_LONG_LEN: int = 25                 # Uzun EMA periyodu
    TSI_SHORT_LEN: int = 13                # Kısa EMA periyodu
    TSI_SIGNAL_LEN: int = 13              # Sinyal EMA periyodu
    TSI_SCORE_BONUS: float = 4.0           # TSI yön uyumu bonusu
    TSI_SIGNAL_BONUS: float = 2.0          # TSI sinyal çapraz bonusu

    # --- Multi-Indicator Divergence ---
    USE_MULTI_DIV: bool = True             # Çoklu indikatör diverjans taraması
    MULTI_DIV_PRD: int = 5                 # Pivot periyodu
    MULTI_DIV_LOOKBACK: int = 100          # Geriye bakış (bar)
    MULTI_DIV_MAX_PP: int = 10             # Maksimum pivot noktası sayısı
    MULTI_DIV_PER_INDICATOR: float = 2.0   # Her indikatör başına skor bonusu
    MULTI_DIV_MAX_BONUS: float = 10.0      # Maksimum toplam bonus

    # --- Whale Liquidity and Absorption Profile ---
    USE_WHALE_PROFILE: bool = True         # Whale Profile aktif
    WHALE_LOOKBACK: int = 200              # Profile lookback bar sayısı
    WHALE_BINS: int = 35                   # Dikey fiyat kutusu sayısı
    WHALE_ABSORPTION_SCORE_BONUS: float = 5.0  # Soğurma seviyesine yakınsa skor bonusu
    WHALE_DELTA_SCORE_BONUS: float = 4.0   # Delta üstünlüğü skor bonusu
    WHALE_DELTA_PERCENTILE: float = 95.0   # Delta üstünlüğü persentil eşiği
    WHALE_STRIDE: int = 10          # Kaç barda bir Whale dizileri güncellenir (1 = her bar)

    # --- Fair Value Gap (FVG) ---
    USE_FVG: bool = True                   # FVG tespiti aktif
    FVG_LOOKBACK: int = 50                 # Geriye bakış (bar)
    FVG_PROXIMITY_ATR: float = 0.5         # ATR cinsinden yakınlık eşiği
    FVG_SCORE_BONUS: float = 6.0           # FVG bölgesinde sweep bonusu (#8 confluence)

    # --- Order Block (OB) ---
    USE_ORDER_BLOCK: bool = True           # Order Block tespiti aktif
    OB_LOOKBACK: int = 80                  # Geriye bakış (bar)
    OB_IMPULSE_ATR_MULT: float = 2.0       # İmpuls büyüklüğü (ATR çarpanı)
    OB_PROXIMITY_ATR: float = 0.75         # Yakınlık eşiği (ATR cinsinden)
    OB_SCORE_BONUS: float = 8.0            # OB bölgesinde sweep bonusu (#8 confluence)
    OB_MAX_AGE_BARS: int = 60              # OB geçerlilik süresi

    # --- Stacked OB + FVG ---
    USE_STACKED_OB_FVG: bool = True
    STACKED_OB_FVG_SCORE_BONUS: float = 9.0  # Breaker (#8 confluence)

    # --- Break of Structure (BoS) ---
    USE_BOS: bool = True
    BOS_PIVOT_LENGTH: int = 1
    BOS_MAX_AGE_BARS: int = 50             # Pivot seviyesinin gecerlilik suresi (bar); asilirsa sifirlanir
    BOS_SCORE_BONUS: float = 9.0           # (#8 confluence)

    # --- LuxAlgo Liquidity ---
    USE_LUX_LIQUIDITY: bool = True
    LUX_LIQ_LENGTH: int = 7
    LUX_LIQ_MARGIN: float = 1.45           # 10 / 6.9 ~= 1.449
    LUX_LIQ_SCORE_BONUS: float = 10.0      # Liq Sweep (#8 confluence)

    # --- EMA Momentum Crossover ---
    USE_EMA_CROSS: bool = True             # EMA cross tespiti aktif
    EMA_FAST_LEN: int = 9                  # Hızlı EMA periyodu
    EMA_SLOW_LEN: int = 21                 # Yavaş EMA periyodu
    EMA_CROSS_LOOKBACK: int = 5            # Cross arama penceresi (bar)
    EMA_CROSS_SCORE_BONUS: float = 3.0     # Cross bonusu (#8 confluence)
    EMA_POSITION_BONUS: float = 2.0        # Fiyat doğru EMA tarafındaysa bonus
    EMA_POSITION_CONTRA_PENALTY: float = 2.0  # EMA pozisyonu sinyal yönüyle çelişirse ceza

    # --- Engulfing Mum Deseni ---
    USE_ENGULFING: bool = True             # Engulfing tespiti aktif
    ENGULFING_BODY_MULT: float = 1.5       # Momentum eşiği (ort. body çarpanı)
    ENGULFING_LOOKBACK: int = 20           # Ortalama body hesaplama penceresi
    ENGULFING_SCORE_BONUS: float = 5.0     # Engulfing + momentum bonus
    ENGULFING_BASIC_BONUS: float = 2.0     # Sadece engulfing bonus

    # --- TTM Squeeze (BB vs Keltner) ---
    USE_TTM_SQUEEZE: bool = True             # Squeeze tespiti aktif
    TTM_BB_LENGTH: int = 20                  # Bollinger periyodu
    TTM_BB_MULT: float = 2.0                 # BB std çarpanı
    TTM_KC_MULT: float = 1.5                 # Keltner ATR çarpanı
    TTM_SQUEEZE_SCORE_BONUS: float = 5.0     # Squeeze yönüyle uyumlu ateşleme bonusu
    TTM_SQUEEZE_CONTRA_PENALTY: float = 4.0  # Squeeze momentumu sinyalle çelişirse ceza

    # --- Ichimoku Bulutu ---
    USE_ICHIMOKU: bool = True                # Ichimoku trend filtresi aktif
    ICHIMOKU_TENKAN: int = 9
    ICHIMOKU_KIJUN: int = 26
    ICHIMOKU_SENKOU_B: int = 52
    ICHIMOKU_SCORE_BONUS: float = 5.0        # Fiyat bulutun sinyal yönündeyse bonus
    ICHIMOKU_CONTRA_PENALTY: float = 5.0     # Bulutun tersi yöndeyse ceza

    # --- Gerçek CVD (Taker Buy Volume) ---
    USE_CVD: bool = True                     # Gerçek delta (taker buy) tabanlı CVD
    CVD_LOOKBACK: int = 20                   # CVD eğim penceresi (bar)
    CVD_SCORE_BONUS: float = 4.0             # CVD eğimi sinyal yönüyle uyumluysa bonus
    CVD_CONTRA_PENALTY: float = 6.0          # Çelişki cezası (sinyal yönüne karşı CVD akışı ciddi)

    # --- Chaikin Money Flow (CMF) ---
    USE_CMF: bool = True                     # CMF para akisi filtresi aktif
    CMF_LENGTH: int = 20                     # CMF toplama penceresi (bar)
    CMF_THRESHOLD: float = 0.05              # |CMF| bu esigin ustundeyse anlamli
    CMF_SCORE_BONUS: float = 4.0             # CMF sinyal yonuyle uyumluysa bonus
    CMF_CONTRA_PENALTY: float = 4.0          # CMF sinyalle celisirse ceza

    # --- Stochastic RSI (anti-chase pullback kalitesi) ---
    USE_STOCH_RSI: bool = True               # StochRSI bileseni aktif
    STOCH_RSI_LENGTH: int = 14               # RSI periyodu
    STOCH_RSI_STOCH_LENGTH: int = 14         # Stoch periyodu (RSI'dan bagimsiz)
    STOCH_RSI_SMOOTH: int = 3                # %K duzeltme periyodu
    STOCH_RSI_OVERBOUGHT: float = 80.0       # Asiri alim esigi
    STOCH_RSI_OVERSOLD: float = 20.0         # Asiri satim esigi
    STOCH_RSI_PULLBACK_BONUS: float = 4.0    # Asiri bolgeden donuste sinyal yonune bonus
    STOCH_RSI_CHASE_PENALTY: float = 4.0     # Asiri bolgeye dogru kovalamaya ceza

    # --- Temel Skor Ağırlıkları (score() fonksiyonu içindeki bileşen ağırlıkları) ---
    # Toplam 1.0 olmalı: WICK + RECLAIM + CLOSE_POS + VOLUME + HTF = 1.0
    SCORE_W_WICK: float = 0.35       # Fitil büyüklüğü ağırlığı
    SCORE_W_RECLAIM: float = 0.25    # Seviye geri kazanımı ağırlığı
    SCORE_W_CLOSE_POS: float = 0.20  # Kapanış pozisyonu ağırlığı
    SCORE_W_VOLUME: float = 0.10     # Hacim bileşeni ağırlığı
    SCORE_W_HTF: float = 0.10        # HTF trend uyumu ağırlığı

    # --- Skor Bonus / Ceza Sabitleri ---
    RSI_DIV_BONUS: float = 5.0            # RSI divergence bonus puanı
    FUNDING_SQUEEZE_BONUS: float = 5.0    # Funding squeeze yönüyle uyumlu sinyal bonusu
    FUNDING_EXTREME_PENALTY: float = 3.0  # Aşırı funding karşı yön cezası

    # --- #2 DI Çelişki Cezası ---
    DI_CONTRA_PENALTY: float = 5.0        # ADX eşik üstü + DI sinyale ters yönde → ceza
    DI_CONTRA_PENALTY_STRONG: float = 10.0  # ADX güçlüyse (>= DI_CONTRA_STRONG_ADX) çelişki daha ağır
    DI_CONTRA_STRONG_ADX: float = 25.0    # Güçlü trend ADX eşiği (rejim TREND eşiğiyle uyumlu)

    # --- #3 Çifte Çelişki Cezası ---
    DOUBLE_CONTRA_PENALTY: float = 5.0    # Supertrend + PMax aynı anda sinyale ters → ek ceza

    # --- #4 ADX Kademeli Ceza (eski ADX_LOW_* yerine) ---
    ADX_PENALTY_TIERS: tuple = (
        (15, 15.0),   # ADX < 15 → -15
        (20, 8.0),    # ADX 15-20 → -8
        (25, 2.0),    # ADX 20-25 → -2
    )  # ADX >= 25 → ceza yok

    # --- #5 Karşı Diverjans Kademeli Ceza ---
    CONTRA_DIV_PENALTY_TIERS: tuple = (
        (2, 3.0),     # 1-2 karşı div → -3
        (4, 6.0),     # 3-4 karşı div → -6
        (6, 9.0),     # 5-6 karşı div → -9
        (7, 12.0),    # 7 karşı div → -12
    )
    CONTRA_DIV_MAX_PENALTY: float = 15.0
    # Karşı diverjans baskınlığı: karşı div, uyumlunun RATIO katından fazlaysa
    # ve en az MIN kadar güçlüyse sinyal tamamen reddedilir (hard reject)
    CONTRA_DIV_DOMINANCE_RATIO: float = 2.0
    CONTRA_DIV_DOMINANCE_MIN: float = 15.0

    # --- #6 L2 Imbalance Kademeli ---
    OB_IMBALANCE_TIERS: tuple = (
        (2, 2.0),     # ratio 1-2 → ±2
        (5, 5.0),     # ratio 2-5 → ±5
        (10, 8.0),    # ratio 5-10 → ±8
        (20, 10.0),   # ratio 10-20 → ±10
        (100, 12.0),  # ratio 20+ → ±12
    )
    OB_IMBALANCE_RATIO_CAP: float = 100.0

    # --- #7 Weighted Divergence (indikatör ağırlıkları) ---
    DIVERGENCE_WEIGHTS: dict = field(default_factory=lambda: {
        "macd_hist": 5, "obv": 5, "rsi": 4, "momentum": 4,
        "stoch": 3, "cci": 2, "mfi": 2
    })

    # --- #1 Trend Conflict Engine ---
    CONFLICT_ENGINE_ENABLED: bool = True
    CONFLICT_SCORE_CAPS: dict = field(default_factory=lambda: {
        5: 100, 4: 92, 3: 80, 2: 65
    })  # uyumlu_motor_sayısı → maks skor; <=1 ise reject

    # --- #9 HTF Sert Filtre ---
    HTF_FILTER_MODE: str = "counter_trend_label"   # "disabled" | "counter_trend_label" | "strict_reject"
    HTF_COUNTER_TREND_PENALTY: float = 10.0

    # --- #10 Setup Validator ---
    # 5 bileşen: ST + PMax + HTF + karşı div baskınlığı + CVD.
    # Aktif bileşenlerin en az yarısı sinyale ters ise hard reject.
    SETUP_VALIDATOR_ENABLED: bool = True
    SETUP_VALIDATOR_DIV_RATIO: float = 1.5  # Karşı div, aynı yön divin bu katından güçlüyse baskın sayılır
    SETUP_VALIDATOR_DIV_MIN: float = 15.0   # Karşı div baskınlığı için minimum güç eşiği

    # --- #11 Dinamik TP/SL ---
    DYNAMIC_TP_SL_ENABLED: bool = True
    # Rejim bazlı TP/SL çarpanları: {rejim: (TP1_R, TP2_R, TP3_R, SL_BUFFER_ATR)}
    # Analiz: SL_BUFFER_ATR 0.25→0.45 geçişiyle uyumlu olarak tüm rejimlerin
    # SL tamponu +0.20 ATR genişletildi (5m whipsaw kayıpları için).
    DYNAMIC_TP_SL_MAP: dict = field(default_factory=lambda: {
        "TREND":       (1.0, 2.5, 4.0, 0.45),
        "STRONG_TREND":(1.0, 2.5, 4.0, 0.45),
        "WEAK_TREND":  (1.0, 2.0, 3.0, 0.45),
        "RANGE":       (0.8, 1.5, 2.0, 0.40),
        "COMPRESSION": (0.8, 1.5, 2.0, 0.40),
        "EXPANSION":   (1.0, 2.5, 4.0, 0.50),
        "MEAN_REVERSION": (0.8, 1.5, 2.0, 0.40),
        "TRANSITION":  (1.0, 2.0, 3.0, 0.45),
    })
    DYNAMIC_TP_SL_HIGH_VOL_ATR_MULT: float = 1.5  # ATR > ortalama × bu çarpan ise yüksek vol
    DYNAMIC_TP_SL_HIGH_VOL_SL_BUFFER: float = 0.55

    # --- #8 Confluence Puanları (mevcut *_SCORE_BONUS'ların güncellenmiş tablosu) ---
    # Bu değerler mevcut *_SCORE_BONUS parametrelerinin üstüne override olarak çalışır
    # Güncelleme: BOS=9, OB=8, FVG=6, EMA=3, VWAP=4, PMax=4(bonus), ST=5(bonus)

    # --- #13 Grade Tablosu ---
    GRADE_TABLE: tuple = (
        (90, "A+", 0.75),
        (80, "A",  0.65),
        (70, "B+", 0.55),
        (60, "B",  0.45),
        (50, "C",  0.35),
        (0,  "D",  0.25),
    )  # (min_skor, harf_notu, tahmini_oran)

    # --- #15 Trend Continuation ADX Eşiği & Weak Trend Drift ---
    # ADX bu eşiğin altındayken trend devamı sayılmaz (ADX<20 = trend yok).
    # Bu durumda setup "Weak Trend Drift" etiketlenir ve skora kesinti uygulanır.
    TREND_CONTINUATION_MIN_ADX: float = 20.0
    # Analiz: WTD sinyallerin %48'i oluşturuyordu ve komisyon sonrası negatifti.
    # Ceza 8→12 (kazanan tek WTD olan SUI +4R'yi öldürmemek için 20 değil),
    # asal temizlik WTD_MIN_ADX kapısıyla yapılır (aşağıda).
    WEAK_TREND_DRIFT_PENALTY: float = 12.0   # Düşük ADX'li trend sinyali skor kesintisi

    # ════════════════════════════════════════════════════════════════════
    #  TARAMA ANALİZİ (taramasonuc*.json) SONRASI YENİ KAPILAR
    #  ~51 işlemlik 3 tarama: 3W/26L/31BE ≈ -13R. Aşağıdaki kapılar bu
    #  desenleri hedefler; her biri config'ten kapatılabilir.
    # ════════════════════════════════════════════════════════════════════

    # --- Kapı 1: Rejim kapısı (TRANSITION katli) ---
    # Analiz: 27 TRANSITION işlemi, 0 galibiyet, ≈ -11.5R. TRANSITION'da
    # yalnızca "gerçek" Liq Sweep'e izin: ADX >= eşik + CMF yönle hizalı.
    REGIME_GATE_ENABLED: bool = True
    REGIME_BLOCKED: tuple = ("TRANSITION",)
    REGIME_TRANSITION_MIN_ADX: float = 20.0      # TRANSITION istisnası için minimum ADX
    REGIME_TRANSITION_REQUIRE_SWEEP: bool = True  # lux liq sweep şartı
    REGIME_TRANSITION_REQUIRE_CMF: bool = True    # CMF yönle hizalı şartı

    # --- Kapı 2: Çift taraflı çelişki vetosu ---
    # Analiz: A+ kayıplarının imzası: whale_bull & whale_bear birlikte true
    # ve/veya FVG/OB iki taraf birlikte near (SUI A+, ORCA A+, MANA, DASH).
    # Bunlar pivot gürültüsüdür; >=2 çelişkili çift → hard reject.
    BOTH_SIDES_CONFLICT_ENABLED: bool = True
    BOTH_SIDES_CONFLICT_PENALTY: float = 15.0    # her çelişkili çift için ceza
    BOTH_SIDES_CONFLICT_VETO_COUNT: int = 2      # bu kadar çift varsa hard reject

    # --- Kapı 3: Fade-stretch vetosu (aşırı gerilime karşı giriş) ---
    # Analiz: 28 Ağu 14:00 kümesi (SUI/ARB/WAL/MANA A/A+ SHORT, hepsi SL)
    # tam olarak linreg_osc > +1.0 iken short açtı; Q long'u -1.33'te.
    # Long için osc <= -thr, short için osc >= +thr → veto.
    FADE_STRETCH_VETO_ENABLED: bool = True
    FADE_STRETCH_VETO_THRESHOLD: float = 1.0     # |linreg_osc| eşiği (99 = kapalı)

    # --- Kapı 4: Chase (kovalama) vetosu ---
    # Analiz: kayıp LONG'lar stoch RSI 87-100'de, kayıp SHORT'lar 0-34'de
    # giriyordu. Yalnızca EXPANSION rejiminde kovalamaya izin verilir
    # (2 galibiyet de EXPANSION'da chase ile geldi: SUI, BTW).
    CHASE_VETO_ENABLED: bool = True
    CHASE_ALLOWED_REGIMES: tuple = ("EXPANSION",)

    # --- Kapı 5: Confluence (yapısal) skor tavanı ---
    # Analiz: LUX+BOS+STACKED+OB+FVG bonusları toplam 42 puana kadar
    # çıkıp A+ 92'lik sahte sinyaller üretiyordu (A+ = 5/5 kayıp).
    # Yapısal bonus toplamı tavanla sınırlanır.
    CONFLUENCE_STRUCTURE_CAP: float = 15.0

    # --- Kapı 6: Setup tip kapıları ---
    # SweepEngine BoS Retest: önceki analizde zayıf sonuç → Sweep için kapalı.
    DISABLED_SETUP_TYPES: tuple = ("BoS Retest",)
    # Weak Trend Drift: yalnızca ADX >= 18'de yaşar (kazanan SUI 18.6;
    # kaybedenlerin çoğu ADX 10-16 bandında: NMR 10.5, CLO 15.6, RE 16.0...).
    WTD_MIN_ADX: float = 18.0
    # Trend Continuation: zayıf DI farkı ile açılanlar kaybetti (COW spread 2.7);
    # kazanan BTW spread 44 → DI farkı eşiği.
    TREND_CONTINUATION_MIN_DI_SPREAD: float = 12.0

    # --- Kapı 7: Portföy korelasyon kapısı ---
    # Analiz: 28 Ağu 14:00'da 5 paritede eşzamanlı A/A+ SHORT, 5'i de SL
    # (-5R tek anda). Aynı barda aynı yönde >= CLUSTER_MIN sinyal varsa
    # yalnızca en yüksek skorlı MAX_SAME_DIR_PER_CYCLE tanesi açılır.
    CORRELATION_GATE_ENABLED: bool = True
    CORRELATION_CLUSTER_MIN: int = 3           # aynı yönde bu kadar sinyal = küme
    MAX_SAME_DIR_PER_CYCLE: int = 2            # kümeden en fazla kaç işlem açılır
    CORRELATION_GATE_WINDOW_BARS: int = 3      # tarayıcı: bu kadar bar yakınlık aynı küme

    # --- Seri Hesaplama Performans Parametreleri ---
    # Her K barda bir Volume Profile / Multi-Divergence güncellenir.
    # Backtest hızını doğrudan etkiler. Düşük = daha doğru, yüksek = daha hızlı.
    VP_SERIES_STRIDE: int = 10       # Volume Profile güncelleme sıklığı (bar)
    MULTI_DIV_STRIDE: int = 10       # Multi-divergence güncelleme sıklığı (bar)

    # --- Volume Spike Dedektörü ---
    USE_VOLUME_SPIKE: bool = True
    VOLUME_SPIKE_MULTIPLIER: float = 10.0    # Ortalama hacmin kaç katı = spike
    VOLUME_SPIKE_MIN_CHANGE_PCT: float = 2.0 # Minimum fiyat değişimi %
    VOLUME_SPIKE_LOOKBACK: int = 20          # Ortalama hesaplama penceresi (bar)
    VOLUME_SPIKE_COOLDOWN_BARS: int = 4      # Aynı sembol için tekrar uyarı arası (bar)
    VOLUME_SPIKE_ALERT_FILE: str = "state/volume_spike_alerted.json"

# --- Whale Alert (Taker Akışı Radarı) ---
    USE_WHALE_ALERT: bool = True
    WHALE_ALERT_PERIOD: str = "5m"             # takerlongshortRatio periyodu
    WHALE_ALERT_LOOKBACK: int = 20             # Baseline hesaplama penceresi (periyot)
    WHALE_ALERT_DEVIATION: float = 0.15        # Min. buy-payı sapması (0.15 = ±15 puan)
    WHALE_ALERT_MIN_VOLUME: float = 500_000    # Min. tek taraf taker hacmi (USDT) — buyVol*markPrice ile karşılaştırılır
    WHALE_ALERT_MIN_BUY_RATIO: float = 0.62    # buy_ratio ≥ bu değer veya ≤ (1-bu değer) olmalı
    WHALE_ALERT_COOLDOWN_BARS: int = 8         # Aynı sembol için tekrar uyarısı arası (periyot)
    WHALE_ALERT_MAX_PER_CYCLE: int = 5         # Tur başına maks. bildirim sayısı
    WHALE_ALERT_FILE: str = "state/whale_alerted.json"
    # --- Gerçek pozisyon olayı (Open Interest) + likidasyon senaryosu ---
    WHALE_ALERT_OI_ENABLED: bool = True        # OI analizi: LONG/SHORT AÇILDI-KAPANIYOR sınıflandırması
    WHALE_ALERT_ASSUMED_LEVERAGE: int = 25     # Başlık likidasyon senaryosundaki kaldıraç (Binance gerçek kaldıracı göstermez)
    WHALE_ALERT_LEVERAGE_TIERS: tuple = (10, 25, 50)  # Senaryo tablosundaki kaldıraç kademeleri

    # --- Whale Alert v2: çift skorlama, veri kalitesi ve çelişki takibi ---
    WHALE_ALERT_CONFLICT_WINDOW_BARS: int = 3  # Karşı yön uyarısı bu kadar bar içinde geldiyse → CONFLICTED
    WHALE_ALERT_HISTORY_SIZE: int = 8          # Sembol başına tutulan uyarı geçmişi (flow_state için)
    WHALE_ALERT_MIN_SCORE: float = 40.0        # Minimum alarm skoru
    WHALE_ALERT_LOW_LOOKBACK: int = 12         # Yeni dip/tepe referans penceresi (bar)
    WHALE_ALERT_PRICE_FLAT_PCT: float = 0.0015 # "Fiyat yatay" eşiği (son bar hareketi < %0.15)
    WHALE_ALERT_ABSORPTION_WINDOW: int = 3     # Absorpsiyon: son N kapanmış bar taranır (çoklu pencere şartı)
    WHALE_ALERT_ABSORPTION_MIN_BARS: int = 2   # ...bu barlardan en az N tanesi "yüksek hacim + yatay fiyat" göstermeli
    WHALE_ALERT_ABSORPTION_VOL_SPIKE: float = 2.0   # Absorpsiyon bar hacmi ≥ referans ortalamanın bu katı
    WHALE_ALERT_ABSORPTION_MAX_IMPACT: float = 0.002 # Absorpsiyon bar fiyat hareketi ≤ %0.2 (yatay)
    WHALE_ALERT_SPIKE_ZSCORE: float = 3.0      # Hacim z-skoru ek tetikleyicisi (dağılım bazlı, hacim×3'ten bağımsız)
    WHALE_ALERT_NET_FLOW_MIN_SHARE: float = 0.25  # "Net akış belirgin" eşiği (|buy-sell| / toplam, 0-1)
    WHALE_ALERT_SPREAD_MAX_PCT: float = 0.10   # Tight spread eşiği (fiyatın yüzdesi; likidite skor bileşeni)
    WHALE_ALERT_SCORE_W: dict = field(default_factory=lambda: {
        "volume_anomaly": 0.30, "deviation": 0.30, "persistence": 0.15,
        "mtf": 0.15, "liquidity": 0.10,
    })  # Akış gücü ağırlıkları (toplam 1.0) — geçmişe göre kalibre edilmeden işlem sinyali olarak kullanılmaz
    WHALE_ALERT_DIR_W: dict = field(default_factory=lambda: {
        "oi": 0.30, "price": 0.30, "cvd": 0.20, "structure": 0.20,
    })  # Yön güveni ağırlıkları (toplam 1.0; eksik veri bileşenlerinin ağırlığı paylaştırılır)
    WHALE_ALERT_SCORE_FLOW_SHARE: float = 0.6  # whale_score = %60 akış gücü + %40 yön güveni

    # --- Backtest ---
    TAKER_FEE: float = 0.0004
    SLIPPAGE: float = 0.0005
    BACKTEST_DAYS: int = 30
    BACKTEST_INITIAL_BALANCE: float = 1000.0
    BACKTEST_RISK_PER_TRADE_PCT: float = 1.0

    # --- Telegram (ASLA koda yazma, .env'den okunur) ---
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # --- Döngü / durum dosyaları ---
    SCAN_INTERVAL_SECONDS: int = 900 #15 dakikada 1 tarama ama önceki hali 300  
    STATE_FILE: str = "state/positions.json"
    ALERTED_FILE: str = "state/alerted.json"
    TRADE_HISTORY_FILE: str = "state/trade_history.json"
    STATS_FILE: str = "state/stats.json"
    SIGNAL_LOG_FILE: str = "state/signal_log.json"


SETTINGS = Settings()
