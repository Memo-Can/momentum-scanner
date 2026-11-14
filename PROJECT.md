# BIST Momentum Scanner

Borsa İstanbul (BIST) hisseleri için canlı momentum/sinyal tarama uygulaması. Web arayüzü, terminal arayüzü ve Android mobil uygulamasından oluşuyor.

- **Canlı site:** https://momentumscanner.com.tr
- **Repo:** https://github.com/Memo-Can/momentum-scanner (branch: `dev`)

## Ne yapıyor

Her 5 dakikada bir Yahoo Finance'ten BIST hisse verisi çekilip; EMA(9/21), MACD(12,26,9), RSI(14), Bollinger Bantları(20,2σ), ADX(14) ve endekse göre göreceli güç (RS) hesaplanıp **tek bir Konfluens Skoru**'na (-6..+6 → GÜÇLÜ AL/AL/NÖTR/SAT/GÜÇLÜ SAT) birleştiriliyor. Amaç: 6 ayrı göstergeye tek tek bakmak yerine tek bakışta net bir yön görmek.

## Özellikler

- **Üç sekme:** BIST 30 / BIST 100 (resmi endeks üyeliği) / BIST 300 (günlük en çok kazandırandan en aza, dinamik — resmi endeks değil). Varsayılan sekme BIST 100.
- **Piyasa göstergesi şeridi:** Dolar, Euro, Sterlin, BIST100 endeksi, Bitcoin, Gram Altın, Gram Gümüş, Petrol — 300 saniyede bir güncellenir, sekmelerin sağında.
- **Hisse grafiği:** 3 aylık mum grafiği + açılır/kapanır EMA/Bollinger/MACD/RSI katmanları. Büyütülmüş görünüm için popup.
- **Skor Geçmişi:** Günlük/Saatlik/5 Dakikalık sekmeleri — her zaman son 1 yılı kapsar (geçmişe dönük backfill + canlı log birleştirilerek).
- **Arama, sütun sıralama, kolon başlığı tooltip'leri.**
- **Web bildirimleri:** Bir hisse GÜÇLÜ AL/SAT'a geçtiğinde tarayıcı bildirimi + ses (sadece gerçek geçişte, spam yok).
- **Telegram bildirimleri:** Bir hisse GÜÇLÜ AL/SAT'a geçtiğinde Telegram'a anlık mesaj (2026-10'da mobil push'un yerini aldı).
- **Mobil uygulama (Android):** Web sitesini saran bir WebView (push bildirimi artık Telegram üzerinden, bkz. yukarı).
- **Sinyal/skor geçmişi (SQLite):** Zamanla "Skor gerçekten işe yarıyor mu" diye kendi verinle değerlendirebilmen için.
- **Sanal Al-Sat (paper trading):** 1.000.000 TL sanal bakiye, GÜÇLÜ AL'a yeni geçen hisseleri otomatik "satın alır", swing stop/hedef ya da GÜÇLÜ SAT'a dönüş ya da süre aşımıyla "satar" — Skor'un gerçek performansını ölçmek için (gerçek para değil).

## Mimari (özet — ayrıntı için `CLAUDE.md`)

```
backend/
  bist/                  → BIST tarayıcısının tüm dosyaları (hepsi bist_ önekli)
    bist_scanner.py        → DB/skor geçmişi/sinyal bildirimi (iş katmanı)
    bist_web.py            → Flask web arayüzü (AKTİF deploy edilen)
    bist_terminal.py       → terminal arayüzü
    bist_backfill_daily.py → geçmişe dönük günlük skor backfill (elle, tek seferlik)
    bist_market_data.py    → Yahoo Finance veri çekme katmanı
    bist_paper_trading.py  → Sanal Al-Sat mantığı
    bist_indicators.py     → saf indikator matematiği (EMA/RSI/MACD/ADX/vb.)
  notification.py        → Telegram bildirim gönderimi
  settings.py            → Ayarlar ekranının iş katmanı
  admin.py               → Yönetici Paneli'nin iş katmanı
  data/                  → momentum_scanner.db (SQLite) — "veritabanı"
frontend/
  templates/index.html   → tek sayfalık web UI
  static/                → favicon + bildirim sesleri
mobile/                  → Expo/React Native Android uygulaması
```

## Kullanılan Teknolojiler

### Backend — Python 3

| Paket | Ne işe yarıyor |
|---|---|
| **Flask** (`flask>=3.1,<4.0`) | Web framework — HTTP route'ları (`/`, `/api/*`), Jinja2 şablon motoru (server-side HTML render), çerez-imzalı `session` (login) burada geliyor. |
| **gunicorn** (`gunicorn>=26.2,<27.0`) | Production WSGI sunucusu — VPS'te Flask'ın kendi geliştirme sunucusu yerine bunu çalıştırıyoruz (`-w 1 --worker-class gthread --threads 4`, tek process + çok thread'li, bkz. `CLAUDE.md`). |
| **yfinance** (`yfinance>=1.7,<2.0`) | Yahoo Finance'in resmi olmayan API'sine istemci — hisse fiyat/geçmiş veri/temel bilgi çekimi. Projenin tek dış veri kaynağı. |
| **pandas** (`pandas>=3.0,<4.0`) | Zaman serisi işleme — kapanış fiyatlarından EMA/MACD/RSI/Bollinger/ADX gibi indikatörlerin hesaplanması, `rolling()`/`pct_change()` gibi fonksiyonlarla. |
| **sqlite3** (Python stdlib, pip bağımlılığı değil) | Dosya tabanlı veritabanı — skor geçmişi, sinyal logu, Sanal Al-Sat tabloları, kullanıcı ayarları. WAL (Write-Ahead Log) modunda, çoklu okuyucu + tek yazarın birbirini bloklamadan çalışması için. |
| **werkzeug** (Flask'ın bağımlılığı, ayrı pip paketi değil) | `ProxyFix` middleware (nginx arkasında gerçek istemci IP'sini görmek için) ve `generate_password_hash`/`check_password_hash` (admin şifresi) buradan geliyor. |
| **urllib** (stdlib) | Telegram Bot API'ye mesaj göndermek için — ekstra bir HTTP kütüphanesi (örn. `requests`) kullanılmadı, stdlib yeterli görüldü. |

### Frontend — sunucu taraflı render + sade JavaScript

| Teknoloji | Ne işe yarıyor |
|---|---|
| **Jinja2** (Flask ile birlikte gelir) | HTML şablon motoru — `{% if %}`/`{% include %}` ile sunucu tarafında koşullu/parçalı render (örn. login olmayana bazı sütun/panellerin HTML'de hiç gönderilmemesi). Build adımı/derleme YOK. |
| **Vanilla JavaScript (ES6+)** | Hiçbir framework (React/Vue/Angular) veya bundler (Webpack/Vite) kullanılmıyor — tüm etkileşim (tablo sıralama/arama/sayfalama, grafik, Sanal Al-Sat, Ayarlar popup'ları) HTML şablonlarının içine gömülü `<script>` blokları ile yazılıyor. Tercih nedeni: proje küçük/orta ölçekli, bir SPA'nın karmaşıklığına gerek görülmedi (bkz. `CLAUDE.md`'deki ilgili karar notu). |
| **CSS3 (düz, framework'süz)** | Tüm görsel tasarım tek bir `style.css` dosyasında — Bootstrap/Tailwind gibi bir CSS framework'ü yok, CSS custom properties (`:root` değişkenleri) ile karanlık tema renkleri yönetiliyor. |
| **lightweight-charts** (TradingView, v4.1.3) | Tek harici JS kütüphanesi — mum grafiği + EMA/Bollinger/MACD/RSI çizimi, crosshair senkronizasyonu. Projenin kendi `static/` klasöründen servis ediliyor (CDN'den değil, bkz. `CLAUDE.md`). |

### Mobil uygulama — TypeScript

| Teknoloji | Ne işe yarıyor |
|---|---|
| **React Native** (0.86) + **React** (19.2) | Android için native köprü — ama bu projede native ekranlar YOK, tek bir `WebView` bileşeni web sitesini sarmalıyor. |
| **Expo** (SDK 57, managed workflow) | React Native uygulamasını derleme/build/dağıtım altyapısı (EAS Build) — native Android/iOS proje dosyalarıyla elle uğraşmadan APK/AAB üretimi. |
| **react-native-webview** | Web sitesini (`momentumscanner.com.tr`) uygulama içinde gösteren WebView bileşeni + Android geri tuşu davranışı (WebView geçmişinde gezinme). |
| **TypeScript** | Tip güvenliği — `App.tsx` ve yapılandırma dosyaları (`app.config.js`) TS ile yazılı, `tsc --noEmit` ile derleme hatası kontrolü yapılıyor. |

## Altyapı

- **VPS:** DigitalOcean (77.245.146.216), nginx reverse proxy + Let's Encrypt SSL, Flask `systemd` servisi (`web-bist.service`).
- **Domain:** `momentumscanner.com.tr` (hosting.com.tr üzerinden alındı).
- **Veri kaynağı:** Yahoo Finance (`yfinance`, resmi olmayan API — rate-limit riskine dikkat, bkz. `CLAUDE.md`).
- **Push bildirimleri:** Telegram bot API (2026-10'da Firebase Cloud Messaging'in yerini aldı).

## Proje geçmişi (özet)

Proje `usd-info` adıyla başlayıp `momentum-scanner`'a taşındı ve kapsamı BIST'e daraltıldı (US/Crypto scanner'lar önce `diger-piyasalar/`'a ayrıldı, 2026-11-14'te hiçbir yerden kullanılmadığı doğrulanıp tamamen silindi). Başlıca gelişim adımları:

1. Terminal tabanlı BIST tarayıcı + konfluens skor sistemi
2. Web arayüzüne taşıma (Flask), responsive tasarım, arama/sıralama
3. Güvenlik taraması (XSS düzeltmesi) + domain/DNS/SSL kurulumu
4. BIST30-sabit-liste + dinamik-kazandıran karışık listeleme mantığının defalarca sadeleştirilmesi → sonunda **resmi BIST30/BIST100 endeks listeleri + dinamik BIST300** üç-sekmeli modele oturdu
5. Grafik indikatör katmanları, Skor Geçmişi paneli, büyütme popup'ı
6. Android mobil companion app (WebView + Firebase push) — Expo/EAS Build ile
7. Piyasa göstergesi şeridi (döviz/emtia/BTC)
8. Geçmişe dönük 1 yıllık backfill sistemi
9. Performans optimizasyonu: skor geçmişi dosyası milyonlarca satıra çıkınca bellek-içi index'e geçildi
10. Repo yeniden düzenlendi: `backend/`/`frontend/` ayrımı + CSV "veritabanı" dosyaları `backend/data/`'ya taşındı (Ekim 2026)
11. CSV "veritabanı" → SQLite'a geçiş: canlı CSV dosyası RAM'i doldurmaya başlamıştı (sınırlı VPS belleği + her başlangıçta tamamının belleğe okunması), tek bir `momentum_scanner.db` + indeksli sorgular ile değiştirildi (Ekim 2026)
12. Sanal Al-Sat (paper trading) simülasyonu eklendi: Skor sisteminin gerçek edge'ini ölçmek için 1.000.000 TL sanal bakiyeyle otomatik al-sat (Ekim 2026)
13. Push bildirimleri Firebase'den Telegram'a taşındı: kurulum/bakım yükü çok daha az, mobil app'e (APK/EAS) bağımlılık kalktı (Ekim 2026)

## Bilinmesi gerekenler

Kritik kurallar, mimari kararların gerekçeleri ve tekrarlanmaması gereken hatalar için **`CLAUDE.md`** dosyasına bak — özellikle VPS deploy ve test çalıştırma konusunda uyulması gereken onay kuralları orada.
