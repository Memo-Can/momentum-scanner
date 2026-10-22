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
- **Mobil uygulama (Android):** Web sitesini saran bir WebView + Firebase üzerinden GÜÇLÜ AL/SAT push bildirimleri.
- **Sinyal/skor geçmişi CSV logları:** Zamanla "Skor gerçekten işe yarıyor mu" diye kendi verinle değerlendirebilmen için.

## Mimari (özet — ayrıntı için `CLAUDE.md`)

```
scanner_bistTop100.py   → iş katmanı: veri çekme, indikatör/skor hesaplama, loglama
web_bistTop100.py       → Flask web arayüzü (AKTİF deploy edilen)
terminal_bistTop100.py  → terminal arayüzü
templates/index.html    → tek sayfalık web UI
backfill_daily_bist.py  → geçmişe dönük günlük skor backfill (elle, tek seferlik)
mobile/                 → Expo/React Native Android uygulaması
diger-piyasalar/        → US/Crypto scanner'lar (KAPSAM DIŞI, artık geliştirilmiyor)
```

## Altyapı

- **VPS:** DigitalOcean (77.245.146.216), nginx reverse proxy + Let's Encrypt SSL, Flask `systemd` servisi (`web-bist.service`).
- **Domain:** `momentumscanner.com.tr` (hosting.com.tr üzerinden alındı).
- **Veri kaynağı:** Yahoo Finance (`yfinance`, resmi olmayan API — rate-limit riskine dikkat, bkz. `CLAUDE.md`).
- **Push bildirimleri:** Firebase Cloud Messaging.

## Proje geçmişi (özet)

Proje `usd-info` adıyla başlayıp `momentum-scanner`'a taşındı ve kapsamı BIST'e daraltıldı (US/Crypto scanner'lar `diger-piyasalar/`'a ayrıldı). Başlıca gelişim adımları:

1. Terminal tabanlı BIST tarayıcı + konfluens skor sistemi
2. Web arayüzüne taşıma (Flask), responsive tasarım, arama/sıralama
3. Güvenlik taraması (XSS düzeltmesi) + domain/DNS/SSL kurulumu
4. BIST30-sabit-liste + dinamik-kazandıran karışık listeleme mantığının defalarca sadeleştirilmesi → sonunda **resmi BIST30/BIST100 endeks listeleri + dinamik BIST300** üç-sekmeli modele oturdu
5. Grafik indikatör katmanları, Skor Geçmişi paneli, büyütme popup'ı
6. Android mobil companion app (WebView + Firebase push) — Expo/EAS Build ile
7. Piyasa göstergesi şeridi (döviz/emtia/BTC)
8. Geçmişe dönük 1 yıllık backfill sistemi
9. Performans optimizasyonu: skor geçmişi dosyası milyonlarca satıra çıkınca bellek-içi index'e geçildi

## Bilinmesi gerekenler

Kritik kurallar, mimari kararların gerekçeleri ve tekrarlanmaması gereken hatalar için **`CLAUDE.md`** dosyasına bak — özellikle VPS deploy ve test çalıştırma konusunda uyulması gereken onay kuralları orada.
