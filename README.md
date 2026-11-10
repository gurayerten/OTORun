# CryptoWatch Sweep Bot

Likidite süpürmesi (stop-hunt) + CHoCH onayı + ATR bazlı risk yönetimine dayanan,
canlı tarama yapan ve pozisyon yaşam döngüsünü (SL/TP/Break-Even) takip eden bir
Telegram botu. Önceki gösterge-ağırlıklandırma tabanlı botun yerine, ekteki Pine
Script indikatöründeki yapısal mantığın Python karşılığıdır.

## ⚠️ Önce bunu yap

Paylaştığın eski kodda Telegram bot token'ı ve chat ID açık metin olarak vardı.
O token artık ifşa olmuş sayılır:

1. Telegram'da **@BotFather** → `/revoke` ile eski token'ı iptal et, yenisini al.
2. Yeni token'ı asla koda yazma — `.env` dosyasına koy (aşağıya bak).

## Kurulum

```bash
pip install -r requirements.txt
Copy-Item .env.example .env
# .env içine TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID'yi yaz
python main.py
```

## Backtest

```bash
python run_backtest.py BTCUSDT ETHUSDT SOLUSDT
python run_backtest.py            # sembol verilmezse dinamik listeden ilk 15'i dener
```

Backtest, `strategy.py` sinyal motorunu ve `risk.py` risk planını kullanır. Sinyal
barı kapandıktan sonra sonraki bar açılışında dolum varsayar; canlı bildirim akışı
ise sinyal barının kapanış fiyatını kullanabilir. Özsermaye eğrisi, stop mesafesine
göre işlem başına varsayılan %1 risk ve bileşik bakiye varsayar (`BACKTEST_RISK_PER_TRADE_PCT`).
Backtest şu an HTF/funding/RSI dış verilerini geçmiş her bar için yeniden kurmaz;
sinyal evreni canlı taramayla birebir değildir. Bu varsayımlar yaklaşık geçmiş
simülasyondur; canlı performans garantisi değildir.

## Dosya yapısı

| Dosya | Görev |
|---|---|
| `config.py` | Tüm parametreler (strateji, risk, exchange, telegram) |
| `exchange.py` | Binance veri çekme + rate limiter + **dinamik** sembol listesi (hardcoded yok) |
| `strategy.py` | Pivot/sweep/CHoCH tespiti ve skor hesaplama (Pine motorunun taşınmış hali) |
| `risk.py` | ATR bazlı SL, R-katlı TP1/TP2/TP3 |
| `state_manager.py` | Açık pozisyon takibi, SL/TP/BE kontrolü, sinyal tekrarını önleme, diske kalıcı kayıt |
| `telegram_notify.py` | Mesaj formatlama ve gönderme |
| `backtester.py` | Canlı kodla aynı mantıkla offline test, gerçek fee/slipaj |
| `main.py` | Sonsuz tarama döngüsü |
| `market_wide_funding_pulse.py` | Piyasa geneli funding değişim raporu |
| `futures_volume_dominance.py` | USDT-M piyasa hacim genişliği ve konsantrasyon özeti |
| `exchange_risk_watch.py` | Sözleşme durumu ve listeleme değişikliği takibi |
| `oi_market_breadth.py` | Hacme göre sınırlı watchlist OI genişliği |
| `market_sentiment_radar.py` | Long/short popülasyon oranları ve taker akışı özeti |
| `crypto_fear_greed.py` | Alternative.me geniş piyasa duyarlılığı (ücretsiz, API anahtarsız) |
| `coinmarketcap_context.py` | CMC toplam piyasa değeri, 24s spot hacim ve BTC/ETH dominansı; anlamlı değişim uyarısı |

## Eski versiyona göre eklenen / düzeltilen noktalar

- **Pozisyon takibi**: Eski bot sinyali Telegram'a atıp unutuyordu. Yeni versiyon
  her sembol için açık pozisyonu diskte tutar, her tarama turunda SL/TP/BE'yi
  kontrol eder ve sonucu (win/loss, R-multiple) bildirir.
- **Sinyal tekrarını önleme**: Aynı bar için aynı sinyal bir daha gönderilmez.
- **Backtest = canlı mantık**: İki ayrı sistem yerine tek strateji kodu.
- **Token güvenliği**: `.env` üzerinden, koda asla yazılmaz.
- **Dinamik sembol listesi**: `exchangeInfo` üzerinden güncel USDT-M perpetual
  semboller; hardcoded liste yok (zaten test amaçlıydı, kaldırıldı).
- **Global rate limiter**: Tüm thread'ler tek bir kilitten geçer, 429 riskini azaltır.
- **Yapısal giriş mantığı**: 100+ ad-hoc ağırlıklı göstergenin toplamı yerine
  (eski kodda bazı bloklar yanlış girintiden dolayı hiç çalışmıyordu — örn. CCI/PSAR
  sinyalleri sadece VWAP mevcutsa hesaplanıyordu) tek, test edilebilir bir kurulum:
  süpürme + reclaim + (opsiyonel) yapı kırılımı.

## Taranacak pariteler nasıl belirleniyor?

Hardcoded liste yerine `screener.py`, her tarama turunda Binance'ın kendi
verisinden dinamik bir kısa liste çıkarır — CoinMarketCap gibi ayrı bir API key
gerektirmeden, zaten kullandığımız Binance Futures API'si üzerinden:

1. **Likidite filtresi** — tek bir `/ticker/24hr` çağrısıyla tüm USDT-M
   paritelerin 24s hacmi alınır, `SCREENER_MIN_QUOTE_VOLUME` altındakiler elenir.
2. **Ön eleme (shortlist)** — hacme göre en yüksek ve 24s fiyat değişimine göre
   en yüksek (mutlak değer) paritelerin BİRLEŞİMİ alınır (`SCREENER_SHORTLIST_SIZE`).
3. **Zenginleştirme** — sadece bu kısa liste için (tüm evren değil):
   - **Hacim sıçraması**: bugünkü hacim / son `SCREENER_VOLUME_BASELINE_DAYS`
     günün ortalaması ("içine aşırı para giren" sinyali)
   - **OI değişimi**: açık pozisyon büyüklüğünün bir önceki tarama turuna göre
     yüzde değişimi (gerçek pozisyon/para girişinin en güvenilir göstergesi,
     diske kalıcı kaydedilir — `state/oi_history.json`)
4. **Kompozit skor** — momentum, hacim sıçraması ve OI değişimi min-max
   normalize edilip ağırlıklı toplanır (`SCREENER_W_*`), en yüksek
   `SCREENER_FINAL_COUNT` parite seçilir. `SCREENER_ALWAYS_INCLUDE` listesindeki
   semboller (örn. BTC/ETH) her zaman dahil edilir.

Kapatmak için `config.py`'de `USE_SCREENER = False` yap — bu durumda eski
brute-force `get_trading_symbols()` (tüm evren, `MAX_SYMBOLS` ile sınırlı) kullanılır.

**Bilinen sınırlama**: OI değişimi sadece Aşama 2'yi geçen kısa listede
hesaplanıyor. Hacmi/fiyatı sakin ama OI'si sessizce artan bir parite (nadir ama
mümkün bir "sessiz birikim" sinyali) bu yüzden kaçırılabilir. İstersen Aşama 1'e
OI'yi de (tüm evren için) dahil edebiliriz — bunun bedeli, ekstra ~N API çağrısı.

CoinMarketCap verisi şu anda `coinmarketcap_context.py` üzerinden yalnızca
global spot piyasa bağlamı olarak kullanılıyor. CMC sıralamaları Binance
vadeli parite evreniyle aynı olmadığı için doğrudan işlem tarayıcısına
karıştırılmıyor.

### CoinMarketCap global piyasa bağlamı

`.env` dosyasına `CMC_API_KEY` eklenirse bot her 6 saatte bir iki CMC isteği
yapar: `global-metrics/quotes/latest` ile toplam piyasa değeri, 24 saatlik spot
hacim ve BTC/ETH dominansını; Basic planda kullanılabilen
`v3/cryptocurrency/listings/latest` ile de ilk 500 piyasa değerli varlığı alır.
İkinci veri Binance USDT perpetual sözleşmeleriyle kesiştirilir; CMC sıralaması,
spot hacmi yüksek hareketliler ilk ölçümde ve yeni/anlamlı değişiklikte Telegram'a
gönderilir. Bu radar tarama sıralamasını veya işlem kararını değiştirmez. CMC
API hatasında mevcut Binance taraması etkilenmez. `CMC_ENABLED`,
`CMC_INTERVAL_MIN`, `CMC_MARKET_CAP_ALERT_PCT`,
`CMC_BTC_DOMINANCE_ALERT_PCT` ve `CMC_DISCOVERY_*` ayarları `config.py`
içinden değiştirilebilir. İstekler altı saatte bir sınırlandırılır ve son durum
diskte tutulur.

## Bilinen sınırlamalar (genel)

- `state/` ve `.env` yerel çalışma verileridir; `.gitignore` ile Git dışında tutulur.
- Binance `forceOrders` kullanıcıya özel imzalı veridir; piyasa geneli tasfiye akışı
  değildir. Signed endpoint desteği eklenene kadar Funding Radar bu veriyi kullanmaz.

- **HTF bias skor bileşeni şu an nötr (0.5) sabit.** Pine'daki gibi üst zaman
  dilimi EMA trendine göre dinamikleştirmek istersen `strategy.py` içinde
  `score()` fonksiyonuna bir `htf_comp` parametresi eklemek gerekir — bilinçli
  olarak basitlik için dışarıda bıraktım.
- **Bu bot emir vermiyor**, sadece sinyal/event bildiriyor. Otomatik işlem için
  Telegram olaylarını bir borsa API'sine (örn. ccxt) bağlayan ayrı bir katman
  gerekir — risk yönetimi tarafı (pozisyon büyüklüğü, kaldıraç, marj kontrolü)
  o noktada eklenmesi gereken kritik bir parça.
- **Sembol başına tek pozisyon.** Aynı sembolde ikinci bir setup oluşsa da
  pozisyon açıkken yok sayılır (Pine'daki `canOpen` mantığıyla aynı).
- `MAX_SYMBOLS=60` varsayılanı rate-limit güvenliği için; tüm USDT-M
  sembollerini taramak istiyorsan `config.py`'de `None` yap ama
  `MIN_REQUEST_INTERVAL`'i de gözden geçir.
