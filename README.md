# BIST Momentum Scanner

Borsa İstanbul (BIST) hisseleri için canlı momentum/sinyal tarama uygulaması.

🔗 **Canlı site:** [momentumscanner.com.tr](https://momentumscanner.com.tr)

## Ne yapıyor

Her 5 dakikada bir Yahoo Finance'ten BIST hisse verisi çekilip; EMA(9/21), MACD(12,26,9), RSI(14), Bollinger Bantları(20,2σ), ADX(14) ve endekse göre göreceli güç (RS) hesaplanıp tek bir **Konfluens Skoru**'na (-6..+6 → GÜÇLÜ AL/AL/NÖTR/SAT/GÜÇLÜ SAT) birleştiriliyor. Amaç: 6 ayrı göstergeye tek tek bakmak yerine tek bakışta net bir yön görmek.

## Özellikler

- **Üç sekme:** BIST 30 / BIST 100 (resmi endeks üyeliği) / BIST 300 (günlük en çok kazandırandan en aza, dinamik).
- **Piyasa göstergesi şeridi:** Dolar, Euro, Sterlin, BIST100 endeksi, Bitcoin, Gram Altın, Gram Gümüş, Petrol.
- **Hisse grafiği:** 3 aylık mum grafiği + açılır/kapanır EMA/Bollinger/MACD/RSI katmanları.
- **Skor Geçmişi:** Günlük/Saatlik/5 Dakikalık sekmeleri, her zaman son 1 yılı kapsar.
- **Arama, sütun sıralama, web bildirimleri + Telegram bildirimleri** (bir hisse GÜÇLÜ AL/SAT'a geçtiğinde).
- **Android companion app:** Web sitesini saran bir WebView.

## Proje yapısı

```
backend/                 → Flask API + tarama/indikator/skor mantığı + terminal arayüzü
  bist/                  BIST tarayıcısının tüm dosyaları (hepsi bist_ önekli)
    bist_scanner.py        DB/skor geçmişi/sinyal bildirimi (iş katmanı)
    bist_web.py            Flask web sunucusu (aktif deploy edilen)
    bist_terminal.py       terminal arayüzü
    bist_backfill_daily.py geçmişe dönük günlük skor backfill (elle, tek seferlik)
    bist_market_data.py    Yahoo Finance veri çekme katmanı
    bist_paper_trading.py  Sanal Al-Sat mantığı
    bist_indicators.py     saf indikator matematiği (EMA/RSI/MACD/ADX/vb.)
  notification.py        Telegram bildirim gönderimi
  settings.py            Ayarlar ekranının iş katmanı
  admin.py               Yönetici Paneli'nin iş katmanı
  data/                  momentum_scanner.db (SQLite, git'e dahil değil)
  diger-piyasalar/       kapsam dışı (US/Crypto scanner denemeleri)
frontend/
  templates/index.html     tek sayfalık web UI
  static/                  favicon + bildirim sesleri
mobile/                   Expo/React Native Android uygulaması
```

## Kurulum (yerel geliştirme)

```bash
git clone https://github.com/Memo-Can/momentum-scanner.git
cd momentum-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
python backend/bist/bist_web.py
```

Tarayıcıda `http://127.0.0.1:5000` adresini aç.

## Daha fazlası

Mimari kararlar, bilinen tuzaklar ve geliştirme geçmişi için [`PROJECT.md`](PROJECT.md) dosyasına bak.
