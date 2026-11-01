# CLAUDE.md

Bu dosya, bu repo üzerinde çalışan Claude Code oturumları için kalıcı talimat/bağlam dosyasıdır. Koddan zaten çıkarılabilecek şeyleri (fonksiyon isimleri, satır numaraları vb.) tekrarlamaz — onun yerine mimariyi, kuralları ve tekrar tekrar aynı hataya düşülmesini engelleyecek dersleri içerir.

## Proje nedir

Borsa İstanbul (BIST) hisseleri için canlı momentum/sinyal tarayıcısı. Web arayüzü (Flask) `https://momentumscanner.com.tr` adresinde VPS üzerinde çalışıyor; ayrıca bir terminal arayüzü ve bir Android mobil companion app (push bildirimli) var.

## Klasör yapısı (Ekim 2026'da backend/frontend ayrımına geçildi)

```
backend/
  app_settings.json       → ÇALIŞMA ZAMANLAMASI ayarları (BIST_REFRESH_SECONDS, BIST açık
                            saatleri) — düz JSON (kod DEĞİL, "yazılım standartlarına" uygun
                            ayrı bir ayar dosyası olsun diye — ilk sürümü .py idi, kullanıcı
                            isteğiyle JSON'a çevrildi), 2026-11-01'de eklendi, bkz. aşağıda
                            "BIST piyasa saatleri dışında tarama durdurulması". Anahtarlar
                            BIST_ önekli (ileride başka bir piyasanın ayarları eklenirse
                            karışmasın diye). Strateji/indikator sabitleri (ATR/PAPER_*/
                            eşikler) BURADA DEĞİL, hâlâ scanner_bistTop100.py'de.
  scanner_bistTop100.py   → İŞ KATMANI (sunum içermez). Veri çekme, indikatör/skor
                            hesaplama, CSV loglama, push bildirim, piyasa göstergeleri.
                            Doğrudan çalıştırılmaz.
  web_bistTop100.py       → WEB sunum katmanı (Flask). AKTİF OLARAK DEPLOY EDİLEN budur.
                            Flask(template_folder=../frontend/templates,
                            static_folder=../frontend/static) ile frontend/'e bakar.
  terminal_bistTop100.py  → Terminal sunum katmanı. scanner'ı import edip kullanır.
  backfill_daily_bist.py  → Tek seferlik, elle çalıştırılan geçmişe dönük günlük skor
                            backfill script'i (bkz. aşağıda "Skor geçmişi").
  requirements.txt
  sounds/                 → terminal_bistTop100.py'nin yerel beep sesleri (web'in
                            frontend/static sesleriyle KARIŞTIRMA, ikisi ayrı).
  data/                   → "database" olarak kullanılan dosyalar (hepsi .gitignore'da,
                            repo'da TAKİP EDİLMEZ — bkz. "Skor geçmişi" altında).
    momentum_scanner.db    → SQLite (2026-10'da CSV'lerden buraya taşındı), score_history
                            + signal_log tabloları.
    _backup/                → migrate_csv_to_sqlite.py'nin tasidigi eski CSV'ler (rollback
                            yedeği, aktif kodun okuduğu/yazdığı bir yer DEĞİL).
  device_tokens_bist.json → data/ İÇİNDE DEĞİL, backend/ kökünde (SQLite'a taşınmadı, ayrı tutuldu).
  diger-piyasalar/        → KAPSAM DIŞI (aşağıda ayrıca bahsedilir), sadece klasör düzeni.
frontend/
  templates/index.html    → Tek sayfalık web UI (tablo + sekmeler + grafik + skor geçmişi
                            + piyasa şeridi + Sanal Al-Sat). Tüm JS inline (dosyanın içinde),
                            CSS ise AYRI bir dosyada (static/style.css) — 2026-10'da inline
                            <style>'den çıkarıldı (bkz. aşağıda).
  static/                 → favicon + web bildirim sesleri + style.css (Flask'ın /static/ rotası).
```

`scanner_bistTop100.py`'deki `DATA_DIR = backend/data` sabiti tüm CSV/JSON yollarının kökü — yeni bir log/CSV dosyası eklenecekse buraya, `os.path.join(DATA_DIR, ...)` ile eklenir. `backend/data/.gitkeep` dizin git'te boş kalmasın diye var (asıl CSV'ler gitignored, fresh clone'da dizin yine de var olsun diye).

`scanner_bistTop100.py` + `web_bistTop100.py` + `terminal_bistTop100.py` aynı iş mantığını paylaşır — biri değişince diğerlerinde de tutarlılık kontrolü yap.

VPS de bu yapıya göçürüldü (2026-10-23): `/root/momentum-scanner/backend/` (+ `data/`) ve `/root/momentum-scanner/frontend/` artık yerelle aynı düzende. `systemd` servis dosyası (`/etc/systemd/system/web-bist.service`) `WorkingDirectory=/root/momentum-scanner/backend`, `ExecStart=/root/momentum-scanner/venv/bin/python3 /root/momentum-scanner/backend/web_bistTop100.py`, (Firebase kullanılırken `Environment=FIREBASE_CREDENTIALS_PATH=...` vardı, 2026-10'da Telegram'a geçişle KALDIRILDI — bkz. aşağıda "Bildirimler", artık `Environment=TELEGRAM_BOT_TOKEN=...` ve `Environment=TELEGRAM_CHAT_ID=...` var). Venv (`/root/momentum-scanner/venv/`) kasıtlı olarak `backend/`'in İÇİNE taşınmadı — dışarıda kalması sorun değil, yol zaten mutlak. Göç sırasında canlı CSV'ler (`score_history_bist.csv`, `signal_log_bist.csv`) checksum ile doğrulanarak taşındı, veri kaybı olmadı.

**`backend/diger-piyasalar/` klasörü (US/Crypto scanner'lar) KAPSAM DIŞI.** Kullanıcı sadece BIST'e odaklanmaya karar verdi, bu dosyalara dokunma, aktif geliştirme orada yapılmıyor. Sadece klasör düzeni için `backend/` altına taşındı (2026-10-23) — içindeki `web_usTop100.py`'nin `templates`/`static` yolu halihazırda eski/kullanılmayan bir referans (BIST tarafının `frontend/`'e taşınmasından önce de iki üst dizine göre hesaplanıyordu), bu dosya çalıştırılmadığı için düzeltilmedi.

## Hisse evreni: BIST 30 / BIST 100 / BIST 300 sekmeleri

- **`BIST30_TICKERS`** ve **`BIST100_TICKERS`** (`scanner_bistTop100.py`): resmi endeks üyeliğinin **sabit, elle girilmiş** listeleri (Midas + Yahoo `region=tr` evrenine çapraz kontrol edilerek doğrulandı, 2026-09). BIST30 ⊆ BIST100 (doğrulandı) — bu yüzden BIST100 taraması otomatik olarak BIST30'u da kapsar, ayrı istek gerekmez. **Endeks çeyreklik yeniden dengeleniyor, bu listelerin zamanla elle güncellenmesi gerekebilir.**
- **BIST 300 sekmesi resmi bir endeks DEĞİL** — günlük % değişime göre (yüksekten düşüğe) dinamik ilk `TOP300_SIZE=300` hisse (`fetch_top_gainers()`).
- Her tarama döngüsünde tek bir **birleşik havuz** çekilir: `BIST100_TICKERS ∪ fetch_top_gainers(300)`, çakışanlar tekrar çekilmez (`web_bistTop100.py: _scan_once()`). Her satır `bist30`/`bist100` bayraklarıyla etiketlenir; BIST 300 sekmesi bu havuzun tamamından türetilir (frontend'de `applyMarketFilter()`).
- **Bunu TÜM piyasayı (~628 hisse) taramaya GENİŞLETME** — bu zaten denendi ve Yahoo'nun resmi olmayan API'sini `YFRateLimitError` ile tetikledi (bkz. "Bilinen tuzaklar" altında). Birleşik havuz (~300-400 hisse) bu riski kabul edilebilir seviyede tutmak için bilinçli bir tasarım kararı.

## Skor sistemi

Confluence Score: `confluence_score_raw()` (-5..+5) + `confluence_label()` (GUCLU_AL/AL/NOTR/SAT/GUCLU_SAT). `ADX_TREND_THRESHOLD=20` ve `RS_THRESHOLD=0.5` sadece **etiketi** etkiler, hiçbir hisseyi listeden ELEMEZ — liste filtresi değil, güven eşiğidir.

Varsayılan tablo sıralaması: önce Skor gücü (GUCLU_AL üstte), aynı grup içinde günlük % (yüksekten düşüğe). Bu, `frontend/templates/index.html`'deki `applySort()`'un `sortState.key === null` dalında ve backend'in `_sort_key()`'inde **birebir aynı** tutulmalı.

### 5 dakikalık bileşen kaldırıldı (2026-10-31)

Confluence Score eskiden **6** bileşenden oluşuyordu (`signal_1d`, `signal_5m`, `signal_macd`, RSI, BB%, RS), aralık **-6..+6** idi. `signal_5m` (`get_intraday_signal()`, 5 dakikalık mumda EMA9/EMA21) **tamamen kaldırıldı** — şimdi **5** bileşen, aralık **-5..+5**.

- **Neden:** (1) `get_intraday_signal()` hisse başına AYRI bir Yahoo isteği (`tk.history(period="5d", interval="5m")`) gerektiriyordu — kaldırılması hisse başına istek sayısını **3'ten 2'ye** düşürüp Yahoo rate-limit riskini azaltıyor. (2) `signal_1d` zaten gün içinde canlı güncelleniyor (Yahoo'nun "bugün" günlük mumu piyasa açıkken sürekli güncelleniyor) — 5m'nin eklediği ek bilgi küçük, gürültüsü (gün içi kısa salınımlarla sık AL/SAT değişmesi) ise gerçek. (3) 1 yıllık backtest'lerin (ATR_TARGET_R, PAPER_MIN_VOL20) HEPSİ zaten fiilen 5m'siz bir sistemle doğrulanmıştı — `backfill_daily_bist.py` tarihsel günlerde `signal_5m`'i hep `NOTR` sabitliyordu (Yahoo'nun 5dk verisi ~60 günden eski gitmiyor), yani canlı sistemi backfill'in ZATEN test ettiği modele getiriyor.
- **`confluence_score_raw(signal_1d, signal_macd, rsi_val, bb_percent, rel_strength)`** ve **`confluence_label(signal_1d, signal_macd, rsi_val, bb_percent, adx_val, rel_strength)`** — parametre listesinden `signal_5m` tamamen çıkarıldı (sadece 0 geçmek değil, parametrenin kendisi yok).
- **Eşikler yeniden ölçeklendi** (maks |skor| 6→5), "GÜÇLÜ AL/SAT için en fazla 1 çekimser, SIFIR karşıt oy" kuralı AYNEN korunarak: `GUCLU_AL: score>=4` (eskiden >=5), `AL: score>=2` (değişmedi), `NOTR: >=-1` (değişmedi), `SAT: >=-3` (eskiden >=-4), `GUCLU_SAT: <-3` (eskiden <-4). **AL/NOTR/SAT sınırları mutlak değer olarak değişmedi** — sadece GÜÇLÜ eşikleri (4 ve -3) yeni maksimuma göre ayarlandı.
- **Tutarlılık kontrolü yapılan 3 yer:** `frontend/templates/index.html: scoreLabelFromAvg()` (Skor Geçmişi ortalama hesaplaması, AYNI eşikler), `backend/backfill_daily_bist.py` (artık `"NOTR"` sabit değer GEÇMİYOR, gerçek 5-parametre çağrısı), `backend/terminal_bistTop100.py` (tablo başlığından "5m" sütunu kaldırıldı). Frontend tablosundaki "5m" sütunu (`<th data-key="signal_5m">`) de kaldırıldı.
- 1 yıllık backtest'lerle farklı EMA periyot çiftleri (5/13, 10/20, 12/26, 20/50) de karşılaştırıldı — (5,13) her metrikte 9/21'den daha iyi çıktı ama **kullanıcı mevcut EMA(9,21)'i değiştirmemeyi seçti**, sadece 5m bileşenini kaldırmaya karar verdi.

## Skor geçmişi (SQLite'a taşındı — Ekim 2026)

**Eski mimari (artık YOK):** `score_history_bist.csv` canlı sunucuda sürekli büyüyordu (her 5 dakikada bir havuzdaki her hisse için bir satır) — Ekim 2026'da 130MB+/3.2M+ satıra çıkmıştı, `web_bistTop100.py` her başlangıçta bunun TAMAMINI bellek içi bir index'e (`_live_score_index`) okuyordu. Bu hem grafik açılışını saniyelerce yavaşlatıyordu HEM DE VPS'in zaten sınırlı RAM'ini (toplam 1.9GB) doldurmaya başlamıştı (servis tek başına ~1.4-1.5GB RAM yiyordu, sistem genelinde sadece 98MB boş RAM kalmıştı, swap yok) — disk alanı değil, bu RAM baskısı asıl aciliyet kaynağıydı. **SQLite'a geçiş sonrası doğrulandı (2026-10-23):** VPS'te servis belleği 1.4GB'dan ~170MB'a düştü, sistem genelinde boş RAM 98MB'dan ~1.2GB'a çıktı.

**Yeni mimari:** `backend/data/momentum_scanner.db` (SQLite), iki tablo:
- `score_history(id, timestamp, ticker, score_raw, score, price, source)`
- `signal_log(id, timestamp, ticker, score, price, daily_pct, rsi, adx, rel_strength, source)`

`source` sütunu `'live'` (her tarama döngüsünde yazılan ham veri) / `'backfill'` (`backfill_daily_bist.py`'nin ürettiği geçmişe dönük günlük veri) değerlerini alır — eskiden bunlar 2 ayrı CSV dosyasıydı, artık aynı tabloda tek bir sütunla ayrışıyor. `(ticker, timestamp)` üzerinde composite index var — `scanner_bistTop100.py: get_score_history()` bu index'i kullanan tek bir SQL sorgusuyla okur, **tabloyu asla tam olarak belleğe yüklemez**. `web_bistTop100.py: api_score_history()` eskisi gibi `SCORE_HISTORY_WINDOW_DAYS=365` penceresiyle backfill+live'ı birleştirip döndürür, backfill script'i tekrar çalıştırmaya gerek yok (pencere otomatik kayıyor).

**Yazma tarafı:** `scanner_bistTop100.py: log_score_history()` / `log_signal_changes()` artık CSV'ye `append` değil, SQLite'a `INSERT` yapıyor — tüm yazmalar `_DB_LOCK` (process-içi `threading.Lock`) altında serileştiriliyor, WAL journal modu (`get_db_connection()`) process/thread'ler arası çoklu okuyucu + tek yazarın birbirini bloklamadan çalışmasını sağlıyor. Her çağrı KISA ÖMÜRLÜ bir `sqlite3.connect()` açıp işini yapıp kapatıyor — tek bir Connection nesnesini thread'ler arasında paylaşmıyoruz.

**`backfill_daily_bist.py`** artık dosya yazmıyor — her çalıştırıldığında ÖNCE `source='backfill'` satırlarını SİLİP sonra yeniden hesapladığı tam seti ekliyor (eski CSV'nin `"w"` modunda baştan yazımına denk, tekrar tekrar çalıştırmak satır biriktirmiyor).

**Migrasyon:** `backend/migrate_csv_to_sqlite.py` tek seferlik script'i eski 4 CSV'yi (varsa) okuyup DB'ye aktarır, satır sayısı karşılaştırmasıyla doğrular, başarılıysa eski CSV'leri SİLMEZ, `backend/data/_backup/` klasörüne (orijinal isimleriyle) taşır (rollback için). Hem yerelde hem VPS'te çalıştırıldı (2026-10-23) — VPS'te servis durdurulup checksum ile doğrulanarak yapıldı, veri kaybı olmadı.

**SQLite dosyasını görüntülemek için** (DB client-server değil, dosya tabanlı — VPS'tekine doğrudan "bağlanılamaz", önce dosyayı `scp` ile indirmek ya da VPS'te `sqlite3` CLI/bir web arayüzü çalıştırmak gerekir): yerelde DB Browser for SQLite (GUI) veya VS Code'un SQLite eklentisi önerilir. VS Code Remote-SSH ile VPS'e bağlanıp dosyayı CANLI (indirmeden) açmak da mümkün — eklentinin "SSH: <host> - Installed" listesinde de kurulu olması gerekir, yoksa dosya düz metin editörüyle açılmaya çalışılır.

## Sanal para (paper trading) simülasyonu

Konfluens Skor'un gerçekte bir edge sağlayıp sağlamadığını kendi canlı verimizle ampirik ölçmek için kurulan, GERÇEK PARA OLMAYAN bir simülasyon (2026-10). `scanner_bistTop100.py: run_paper_trading_cycle(rows)` — `web_bistTop100.py: _scan_once()`'tan her tarama döngüsünde (aynı `top_rows`'la, EKSTRA Yahoo isteği yapmadan) çağrılır, **kendi try/except'i içinde** (bir hata ana tarama/skor loglama akışını bozmasın).

- **Sabitler:** `PAPER_STARTING_BALANCE=1_000_000.0`, `PAPER_MAX_POSITIONS=5`, `PAPER_MAX_HOLD_DAYS=21` (bu sabitler, `_init_paper_trading()`'in ihtiyacı olduğu için dosyada `_init_db()`'den ÖNCE tanımlı — `ATR_*` sabitlerinin yanına taşınırsa bu sırayı bozma).
- **Tablolar** (aynı `momentum_scanner.db`): `paper_account` (tekil satır, nakit), `paper_trades` (açık/kapalı işlemler, `status`/`exit_reason` sütunlu), `paper_equity_history` (her döngüde bir equity satırı — Skor Geçmişi'yle aynı "ham veri tut, agregasyonu okuma anında yap" deseni).
- **Alım kuralı — SADECE YENİ GUCLU_AL geçişleri:** `_paper_last_score` (modül-seviyesi dict, `log_signal_changes`'in `_last_logged_score`'undan BAĞIMSIZ) her ticker'ın önceki döngüdeki skorunu tutar. Process her (re)başladığında BOŞ başlar — bu KASITLI: ilk döngüde `prev=None` olduğu için hiçbir şey "yeni geçiş" sayılmaz, yani **deploy/restart anında zaten GUCLU_AL olan hiçbir hisse satın alınmaz**. Sadece `prev != 'GUCLU_AL'` iken `score=='GUCLU_AL'` olan gerçek geçişler alım adayı olur.
- **Çıkış kuralı (ilk uyan kazanır):** `price <= stop_price` → `'stop'`; `price >= target_price` → `'target'`; skor `SAT` **veya** `GUCLU_SAT`'a dönerse (2026-10-31'de `GUCLU_SAT` tek başına yeterliyken genişletildi — bkz. aşağıdaki "Manuel kontrol") → `'signal_reversal'`; `PAPER_MAX_HOLD_DAYS` (takvim günü yaklaşımı, gerçek işlem günü sayacı DEĞİL) aşılırsa → `'time_stop'`. Stop/hedef değerleri zaten var olan `swing_stop`/`swing_target` (ATR×3, 1:3 risk-ödül — bkz. aşağıdaki "Al/satış seviyeleri (ATR_TARGET_R) — backtest") sütunlarından geliyor — yeni bir ATR formülü YOK.

### Al/satış seviyeleri (ATR_TARGET_R) — backtest ile 2.0'dan 3.0'a çıkarıldı (2026-10-30)

`scanner_bistTop100.py: ATR_TARGET_R` (hedef mesafesi = stop mesafesi × bu katsayı, hem `ATR_DAY_MULT` hem `ATR_SWING_MULT` için ortak) **2.0'dan 3.0'a** çıkarıldı — GunHedef/SwHedef (siteyi gösterilen) VE otomatik Sanal Al-Sat'ın `target_price`'ı (`run_paper_trading_cycle()`, `swing_target`'ı doğrudan kullanıyor) ikisi de bundan etkilendi. Stop mesafeleri (`ATR_DAY_MULT=1.5`, `ATR_SWING_MULT=3.0`) DEĞİŞMEDİ.

**Neden:** 1 yıllık günlük backfill verisiyle (`score_history WHERE source='backfill'`, 2025-09-15..2026-09-14, 620 hisse) tek seferlik bir backtest yapıldı — gerçek alım kuralının (yeni GUCLU_AL geçişi) aynısını kullanarak 7.916 geçiş tespit edildi, her biri için 21 işlem günü ufkunda farklı stop%/risk-ödül kombinasyonlarının sonucu simüle edildi (stop veya hedefe önce kim ulaşıyor, ulaşan yoksa 21. gün fiyatı). Bulgular:
- **Dar stop'lar (ör. %5-12, GunStop'un ATR×1.5'inin tipik ürettiği mesafeye yakın) whipsaw'a (gereksiz erken çıkış) yol açıyor** — medyan getiri derinden negatif (-%5 ile -%8), ortalamayı sadece birkaç büyük kazanç kurtarıyor.
- **Geniş stop'lar (%17-29, SwStop'un gerçek pozisyonlarda ürettiği aralık) daha az whipsaw'a yol açıyor** — swing seviyelerinin gün-içi seviyelerden tarihsel olarak daha "sağlıklı" olduğunu doğruluyor, bu yüzden stop'lara DOKUNULMADI.
- **Her stop genişliğinde, risk:ödül oranı arttıkça (1:1→1:3) ortalama getiri TUTARLI şekilde arttı** (örn. %28 stop'ta 1:1→+%0.47, 1:3→+%1.35) — en iyi test edilen değer RR=3.0 oldu, bu yüzden `ATR_TARGET_R=3.0` seçildi.
- **Önemli çekince (kullanıcıya da iletildi):** 7.916 "işlem" birbirinden bağımsız değil (aynı ~620 hissenin, aynı 1 yıllık piyasa rejiminin içindeki geçişleri) — yönlendirici bir sinyal, istatistiksel bir garanti değil. Backfill günlük etiketler de canlı sistemin 5 dakikalık tespitiyle birebir aynı zamanlamayı yakalamıyor.
- Kullanıcı ayrıca "daha sık/kısa aralıkla kârla satma" (RR≈1, hızlı kâr alma) senaryosunu sordu — backtest'te RR=1.0 satırlarının ortalama getirisi her stop genişliğinde ~sıfıra yakın çıktı (ör. %-0.14 ile +%0.47), bu yüzden o yöne GİDİLMEDİ; bunun yerine (henüz uygulanmadı, sadece teklif edildi) breakeven-stop (pozisyon belirli bir kâra geçince stop'u girişe çekme) fikri konuşuldu — kodlanmadan önce aynı veriyle test edilmesi planlanıyor.
- Backtest script'i kalıcı bir dosya olarak repoya eklenmedi (tek seferlik, VPS'teki `/tmp`'e kopyalanmadan `ssh ... python3 - < script.py` ile stdin üzerinden çalıştırıldı, DB'ye hiçbir yazma yapmadı) — tekrar çalıştırmak istenirse bu konuşmadaki script'ten yeniden oluşturulması gerekir.

### GUCLU_AL volatilite filtresi (2026-10-30'da eklendi) — backtest ile

Aynı 1 yıllık backfill verisiyle takip analizi yapıldı: `GUCLU_AL` geçişleri, sinyal öncesi **20 günlük kapanış getirilerinin standart sapmasına** (volatilite) göre 4'e bölündü. **En sakin çeyrekteki (Q1) hisselerde GUCLU_AL'in pratikte hiç edge taşımadığı görüldü** (ortalama getiri ~%0.16, diğer çeyreklerde %1.3-2.2). Bu çeyreği (n=1838/7350, eşik: günlük getiri std ≤ ~%2.13) hariç tutunca kalan işlemlerin ortalama getirisi %1.47'den **%1.91'e** çıktı (işlem sayısı ~%25 azalsa da kalite arttı).

- **`scanner_bistTop100.py: PAPER_MIN_VOL20 = 0.0213`** — bu backtest'teki Q1 eşiği. `fetch_row()` zaten çekilmiş olan 6 aylık günlük kapanış serisinden (`closes`, ekstra Yahoo isteği YOK) son 20 günün getiri std'sini hesaplayıp `row["vol20"]` olarak ekliyor.
- **SADECE `run_paper_trading_cycle()`'ın otomatik alım kararını etkiler** — `vol20 < PAPER_MIN_VOL20` ise GUCLU_AL geçişi tespit edilse de (yani `_paper_last_score` hâlâ normal güncellenir) **satın ALINMAZ**. `confluence_label()`/sitede gösterilen GUCLU_AL etiketi bundan HABERSİZ, hiçbir kullanıcıya görünen davranış değişmedi — sadece botun hangi sinyallere göre alım yaptığı değişti.
- Aynı çekince burada da geçerli: tek yıllık, korelasyonlu bir örneklem üzerinden bulunan ampirik bir eşik, kesin bir kural değil.
- **Pozisyon büyüklüğü:** Her yeni alım `min(PAPER_STARTING_BALANCE/PAPER_MAX_POSITIONS, mevcut nakit)` kadar (nakit yetmezse kısılır) — her zaman 5 slot dolu tutulmaya çalışılır, biri kapanınca aynı döngüde (ya da bir sonraki uygun adayla) yeni pozisyon açılabilir.
- **API:** `/api/paper/summary`, `/api/paper/positions` (canlı fiyat/K-Z `_state['rows']` ile zenginleştirilir), `/api/paper/trades`, `/api/paper/equity`. Frontend'de `frontend/templates/index.html`'deki `#paper-trading` bölümü (ana tablonun ALTINDA, ayrı bir section — BIST sekmelerinden bağımsız), başlığa tıklanınca daralır/genişler (tercih `localStorage`'da tutulur, sadece tarayıcı bazlı).
- VPS'e deploy edildi (2026-10-23) — `paper_*` tabloları `CREATE TABLE IF NOT EXISTS` ile ilk `import`'ta otomatik oluştu, manuel migrasyon gerekmedi. Deploy anında (restart ile `_paper_last_score` sıfırlandığı için) hiçbir pozisyon otomatik açılmadı, doğrulandı.

### Manuel kontrol (2026-10-28'de eklendi) — otomatik sistemden BAĞIMSIZ

Kullanıcı artık nakti elle düzenleyebiliyor, herhangi bir hisseyi "manuel takip listesi"ne ekleyip o listeden istediği an elle alıp satabiliyor. Bu, yukarıdaki otomatik sistemle (`run_paper_trading_cycle`) **karışmaz**:

- `paper_trades`'e **`source`** sütunu eklendi (`'auto'`/`'manual'`, var olan tablo `ALTER TABLE` ile migrate edildi — `PRAGMA table_info` kontrolüyle idempotent, `_init_db()`'de). Mevcut (deploy öncesi açılmış) satırlar otomatik `'auto'` sayılır.
- **`run_paper_trading_cycle()`'ın çıkış kontrolü `source`'a göre dallanır** — `auto` için stop/hedef/**SAT-veya-GÜÇLÜ-SAT**/süre kuralı (2026-10-31'de sinyal kosulu sadece `GUCLU_SAT` iken `SAT`'i de kapsayacak sekilde genişletildi, `exit_reason='signal_reversal'`); `manual` için stop/hedef/süre kuralı HALA uygulanmıyor, ama **aynı gün eklenen** bir kural var: skor **SAT veya GÜÇLÜ SAT**'a dönerse (`exit_reason='manual_sat_signal'`) pozisyon OTOMATİK satılıyor — kullanıcı ayrıca istediği an elle de "Sat" diyebilir (`manual_sell()`), ikisi birlikte var. İkisi de Telegram'a bildiriliyor (bkz. "Bildirimler"). `positions_value`/equity hesaplaması DEĞİŞMEDİ — hem auto hem manual açık pozisyonları sayar. **5-slot limiti sayımı** (`open_tickers`) artık `open_positions` sorgusu ikisini de içerse de SADECE `source=='auto'` filtrelenerek hesaplanıyor — manuel pozisyonlar hâlâ limite dahil değil.
- Yeni tablo: `paper_watchlist(ticker PRIMARY KEY, added_at)` — kullanıcının elle eklediği, henüz almadığı hisseler. `web_bistTop100.py: _scan_once()` bu ticker'ları da (açık pozisyonlarla AYNI sebeple — bkz. yukarıdaki USHOL notu) her döngüde tarama havuzuna ekler, yoksa fiyatsız kalır.
- Manuel alımda tutar HER ALIMDA elle girilir (otomatik sistemin sabit 1/5 payı YOK) — `manual_buy(ticker, allocation, price)`, tek sınırı mevcut nakit, 5-slot limitine dahil DEĞİL.
- Yeni API'ler (`web_bistTop100.py`, hepsi `@_login_required`): `POST /api/paper/cash`, `POST /api/paper/reset` (body `{amount}`), `GET/POST /api/paper/watchlist`, `DELETE /api/paper/watchlist/<ticker>`, `POST /api/paper/manual-buy`, `POST /api/paper/manual-sell`.
- Frontend: yan panelde (`#side-content`) "Sanal Al-Sat Takibine Ekle/Çıkar" butonu; Sanal Al-Sat panelinde nakit düzenleme input'u + "Her Şeyi Sıfırla" butonu (JS `confirm()` ile onay ister) + "Manuel Takip Listesi" alt-bölümü; Açık Pozisyonlar/Kapanan İşlemler tablolarına "Kaynak" (Otomatik/Manuel) sütunu, manuel açık pozisyonlara "Sat" butonu.
- **`reset_paper_trading(starting_balance)`:** `paper_trades`/`paper_equity_history`/`paper_watchlist` tamamen SİLİNİR, `cash` VE `starting_balance` (K/Z'nin ölçüldüğü baz) verilen tutara eşitlenir — **sabit `PAPER_STARTING_BALANCE` değil**, kullanıcı her sıfırlamada kendi başlangıç tutarını seçer (frontend "Yeni nakit" kutusundaki değeri kullanır, boşsa `prompt()` ile sorar). `_paper_last_score` KASITLI dokunulmaz (yoksa restart-hatasıyla aynı sebepten sıfırlama sonrası toplu yanlış alım tetiklenir).

### Görünüm değiştirici (2026-10-28'de ayrı sayfaya taşındı, 2026-10-30'da TEK SAYFAYA geri alındı)

**Güncel mimari:** Sanal Al-Sat, `/sanal-al-sat` gibi ayrı bir route/sayfa DEĞİL — tamamı `index.html` içinde, header/footer/`#market-tabs`/yan panel (`#side`) SABİT kalırken sadece `#table-wrap`'in İÇİNDEKİ görünüm JS ile değişiyor (`showView('stocks' | 'paper')`): `#view-stocks` (tablo) ve `#view-paper-trading` (Sanal Al-Sat bölümünün tamamı) aynı `#table-wrap` içinde iki kardeş `<div>`, her an sadece biri `hidden` değil. Geçiş butonu (`#paper-view-toggle`, sadece login olana görünür) `#market-tabs` satırında BIST 300'ün yanında — diğer BIST sekmeleriyle AYNI `.market-tab-btn` class'ını (ve aktifken aynı `.active` stilini) paylaşıyor. Bir BIST sekmesine (BIST 30/100/300) tıklamak da otomatik olarak `showView('stocks')` çağırıp Sanal Al-Sat'tan geri döner.

- **Neden "ayrı sayfa" denemesi geri alındı:** Kullanıcı header/footer/yan panelin SABİT kalıp sadece iç içeriğin değişmesini istedi — tam sayfa yönlendirmesi (`window.location`) bunu sağlayamıyordu. `web_bistTop100.py: /sanal-al-sat` route'u ve `frontend/templates/paper_trading.html` SİLİNDİ, içerikleri `index.html`'e geri taşındı.
- `_login_required`/`_is_logged_in()` login mantığı DEĞİŞMEDİ — Sanal Al-Sat'ın HTML'i (`#view-paper-trading` ve `#paper-view-toggle`) hâlâ `{% if logged_in %}` ile SADECE login olana gönderiliyor (gerçek sunucu-taraflı gizleme, sadece CSS/JS ile gizleme değil); `/api/paper/*` endpoint'leri hâlâ `@_login_required` ile korunuyor.
- **Pozisyon/işlem/takip listesi satırlarına tıklamak** artık (ayrı sayfa döneminde olduğu gibi `window.open` ile yeni sekme DEĞİL) `openTickerFromPaper()` ile `showView('stocks')` + `selectTicker()` çağırıyor — aynı sayfada olduğumuz için doğrudan hisse tablosuna dönüp yan paneldeki grafiği açıyor.
- `#paper-trading`'in CSS'i artık `position: sticky` DEĞİL (eski "tablonun altında sabit panel" tasarımından kalma) — `#view-paper-trading` tabloyu TAMAMEN yerini aldığı için normal akışta, tam genişlikte bir blok.
- **TL input'ları artık ondalık da destekliyor (2026-10-30):** `attachTLInputMask()` eskiden sadece binlik nokta ekleyip ondalığı tamamen siliyordu (digit-only strip) — şimdi TR para birimi kuralına göre binlik NOKTA + ondalık VİRGÜL (en fazla 2 hane) canlı uygulanıyor (örn. yazarken "1234567,5" → "1.234.567,5"). `parseTLAmount()` bunu geri sayıya çevirirken önce noktaları siliyor, sonra virgülü ondalık noktaya çeviriyor. Nakit düzenleme input'u (`#paper-cash-input`, eskiden mask'siz) artık DA bu maskeyi kullanıyor; cash-save/reset butonları artık ham `parseFloat` DEĞİL, `parseTLAmount` çağırıyor (maskelenmiş metni doğru parse etmek için).

## Ana/MACD/RSI grafik crosshair senkronizasyonu ve canlı gösterge legend'i (2026-10-31'de eklendi)

Kullanıcı isteği: bir muma gelince (hover), o günün TÜM gösterge değerlerini (OHLC + EMA9/21 + MACD/Sinyal + RSI) tek bakışta görmek — ayrı ayrı 3 grafiğe (ana/MACD/RSI) tek tek bakmak zorunda kalmadan. EMA/MACD/RSI'yi TEK bir canvas'a gerçekten birleştirmek mümkün değildi (fiyat TL, MACD ~0 civarı, RSI 0-100 — çok farklı ölçekler, lightweight-charts da bu yüzden ayrı "pane" kullanıyor) — bunun yerine 3 grafiğin crosshair'leri (imleç dikey çizgisi) birbirine senkronize edildi + üstüne bir canlı bilgi satırı (`#chart-legend`) eklendi.

- `frontend/templates/index.html: wireCrosshairSync(sourceChart)` — her `ensureChart()`/`ensureMacdChart()`/`ensureRsiChart()` kendi grafiğini oluşturduktan SONRA bunu çağırır. `chart.subscribeCrosshairMove()` (lightweight-charts 4.1.0+'ta eklenen resmi API) ile tetiklenir; `setCrosshairPosition(price, time, series)`/`clearCrosshairPosition()` ile DİĞER iki grafiğin crosshair'i AYNI zamana taşınır. `crosshairSyncing` kilidi, programatik taşımanın kendi `subscribeCrosshairMove`'unu tekrar tetikleyip sonsuz döngü oluşturmasını önler.
- `candleTimeMap`/`indicatorTimeMaps` — `selectTicker()`/`applyIndicatorData()` her hisse seçiminde bunları (backend'den gelen `time` string'leriyle anahtarlanmış `Map`'ler) yeniden oluşturur. `updateChartLegend(time)` hangi grafiğin crosshair'i tetiklerse tetiklesin AYNI Map'lerden okur — üç grafik arasında veri tekrarı/ayrı sorgu YOK.
- **`timeKey()` normalizasyonu ZORUNLU** — bkz. "Bilinen tuzaklar" altındaki ayrı madde: crosshair'in `param.time`'ı bazen `{year,month,day}` nesnesi dönüyor, Map'ler ise orijinal string'lerle anahtarlanmış.
- Grafik "büyütme" modal'ı (`openInModal()`, tüm `#chart-group`'u taşıyor) bunu etkilemiyor — legend `#chart-group`'un İÇİNDE olduğu için modal'a birlikte taşınıyor, crosshair subscription'ları da DOM konumundan bağımsız (chart JS nesnelerine bağlı) çalışmaya devam ediyor.
- **Grafikte AL/GÜÇLÜ AL/SAT/GÜÇLÜ SAT geçiş işaretleri** (aynı gün eklendi): `updateChartMarkers()`, Skor Geçmişi'nin GÜNLÜK sekmesinin kullandığı AYNI `aggregateBy(scoreHistoryRaw, dayKey)` verisini kronolojik gezip `candleSeries.setMarkers()` ile mumların üstüne/altına ok koyar. İKİ önemli detay: (1) Skor Geçmişi 1 yıl geriye gidiyor ama grafikteki mumlar sadece ~3 ay — bir işaret SADECE `candleTimeMap.has(d.label)` ise (o gün gerçekten grafikte bir mum varsa) eklenir, aksi halde lightweight-charts aralık dışı işaretleri ilk muma yığıyordu. (2) AL/GÜÇLÜ AL (ve SAT/GÜÇLÜ SAT) `markerZone()` ile TEK bir "bölge" sayılır — ADX'in GÜÇLÜ eşiğinin (20) etrafında gün gün gidip gelmesi aynı trend sürerken tekrar tekrar işaretlemeye yol açıyordu, bölge gerçekten değişmeden (örn. NÖTR/SAT'tan AL'a) yeni işaret konulmuyor.

## BIST piyasa saatleri dışında tarama durdurulması (2026-11-01'de eklendi)

`web_bistTop100.py: _scan_loop()` (hisse tarama döngüsü) artık BIST'in **hafta içi (Pazartesi-Cuma) VE** `app_settings.json`'daki `BIST_MARKET_OPEN_*`/`BIST_MARKET_CLOSE_*` penceresi (varsayılan 09:30-18:30, TR saati) **dışındayken** `_scan_once()`'ı hiç çağırmıyor — döngü yine `BIST_REFRESH_SECONDS`'ta bir uyanıp `scanner.is_market_open()`'a bakıyor, Yahoo'ya istek atmadan tekrar uyuyor.

- **Neden:** Önceden hem hisse tarama hem market-şeridi döngüsü 7/24 (hafta sonu ve BIST kapalıyken DAHİL) her 5 dakikada bir Yahoo'ya istek atıyordu — BIST haftada sadece ~40/168 saat açık olduğu için bu, gerçekte gerekenin ~4 katı istek anlamına geliyordu ve CLAUDE.md'de zaten en çok vurgulanan riski (Yahoo rate-limit) gereksiz büyütüyordu.
- **Kapsam BİLİNÇLİ olarak SADECE hisse tarama döngüsüne (`_scan_loop`)** uygulandı — `_market_scan_loop()` (Dolar/Euro/Sterlin/BIST100/BTC/Altın/Gümüş/Petrol şeridi) buna TABİ DEĞİL, çünkü bu enstrümanların (özellikle döviz/BTC) kendi işlem saatleri BIST'ten bağımsız ve BIST dışı saatlerde de gerçek fiyat hareketi oluyor. `terminal_bistTop100.py`'nin kendi döngüsü de BİLİNÇLİ olarak DOKUNULMADI — o, kullanıcının elle başlatıp izlediği bir araç, piyasa kapalıyken "neden güncellenmiyor" kafa karışıklığı yaratmasın diye.
- **`app_settings.json`** (yeni dosya, `backend/`) — **düz JSON, Python kodu DEĞİL** (kullanıcı "yazılım standartları gereği ayar dosyası .py olmamalı" dedi, ilk sürüm `.py` idi, sonra buna çevrildi). İçeriği: `{"BIST_REFRESH_SECONDS": 300, "BIST_MARKET_OPEN_HOUR": 9, "BIST_MARKET_OPEN_MINUTE": 30, "BIST_MARKET_CLOSE_HOUR": 18, "BIST_MARKET_CLOSE_MINUTE": 30}`. Anahtarlar **BIST_ önekli** — kullanıcı isteği: ileride başka bir piyasanın (örn. ABD/kripto) kendi REFRESH_SECONDS/MARKET_OPEN_* değerleri eklenirse BIST'inkiyle KARIŞMASIN diye. `scanner_bistTop100.py` dosyanın başında `json.load()` ile okuyup **AYNI BIST_ önekiyle** kendi isim alanına sabit olarak atıyor (`BIST_REFRESH_SECONDS = _app_settings["BIST_REFRESH_SECONDS"]` vb.) — bu yüzden `scanner.REFRESH_SECONDS` DEĞİL, **`scanner.BIST_REFRESH_SECONDS`** (aynı şekilde `BIST_MARKET_OPEN_HOUR`/`_MINUTE`/`BIST_MARKET_CLOSE_HOUR`/`_MINUTE`) — bu, isim DEĞİŞİKLİĞİ olduğu için `web_bistTop100.py` VE `terminal_bistTop100.py`'deki TÜM çağrı noktaları (`scanner.REFRESH_SECONDS` geçen her yer) bu yeni isme güncellendi. Strateji/indikator sabitleri (ATR_*/PAPER_*/eşikler) bu dosyada DEĞİL, hâlâ `scanner_bistTop100.py`'de düz Python sabiti olarak kalıyor — `app_settings.json` SADECE "ne zaman/ne sıklıkla çalışıyoruz" sorusuna cevap veren ayarlar için.
- **`scanner_bistTop100.py: is_market_open(now=None)`** — hafta sonu kontrolü (`now.weekday() >= 5`) + saat penceresi kontrolü. Resmi BIST tatilleri (bayram vb.) BİLİNÇLİ olarak hesaba katılmıyor — o günlerde tarama yine çalışır ama Yahoo zaten değişmeyen son fiyatı döndürür, zararsız bir fazladan istek olur.
- Piyasa açık saatlerinde `_state["rows"]`/`updated_at` normal akışında (`_scan_loop`, her `BIST_REFRESH_SECONDS`'ta bir) güncellenmeye devam eder.

### İlk taramanın piyasa kapalıyken bile anında yapılması (`_ensure_scanned_once`, aynı gün eklendi)

**Sorun:** `_scan_loop()` piyasa kapalıyken `_scan_once()`'ı HİÇ çağırmadığı için, servis TAM OLARAK piyasa kapalıyken (re)başlarsa `_state["rows"]` ilk tarama yapılana kadar (piyasa açılana kadar, saatlerce) **BOŞ** kalıyordu — kullanıcı siteyi hafta sonu açtığında "0 hisse" görüyordu. Kullanıcı isteği: "sayfaya istek atıldığında datalar görünsün ama 5 dakikada bir çalışmasın, o anda Yahoo'da hangi veriler varsa onları yansıtsın."

- **`web_bistTop100.py: _ensure_scanned_once()`** — `/api/rows` çağrılan HER istekte en başta çalışır: `_state["updated_at"]` hâlâ `None` ise (yani HENÜZ hiç tarama yapılmadıysa) piyasa saatinden BAĞIMSIZ, **bir kerelik** senkron bir `_scan_once()` tetikler; zaten bir tarama yapılmışsa (en son ne zaman olduğuna bakılmaksızın) hiçbir şey yapmadan hemen döner. `_ensure_scan_lock` ile çift kontrol (double-checked locking) yapılır — aynı anda gelen birden fazla istek, aynı anda birden fazla tarama tetiklemez (biri tararken diğerleri bekler, sonucu paylaşır).
- **Bilinçli tasarım:** Bu, SÜREKLİ çalışan bir mekanizma DEĞİL — sadece "hiç taranmamış" durumunu bir kez kurtarır. İlk istek sonrası (piyasa kapalıyken bile) `_state["rows"]` dolar ve bir SONRAKİ istek anında (`updated_at` artık `None` olmadığı için) döner, YENİDEN TARAMAZ — "5 dakikada bir çalışmasın" isteği böyle karşılanıyor. Piyasa açıldığında `_scan_loop()` normal akışına (her `BIST_REFRESH_SECONDS`'ta bir tazeleme) kendiliğinden devam eder.
- **Doğrulandı (2026-11-01, Pazar günü, yerelde):** İlk `/api/rows` isteği ~14 saniye sürdü (353 hisse, piyasa kapalı olduğu için Yahoo'nun elindeki son kapanış verisi), hemen ardından gelen ikinci istek 0.02 saniyede AYNI `updated_at` ile döndü (yeniden tarama YAPILMADI).

## Deploy süreci (VPS)

- VPS: `77.245.146.216`, SSH portu **22667** (standart 22 DEĞİL — port değişmiş durumda, `ssh -i ~/.ssh/momentum_vps_key -p 22667 root@77.245.146.216`).
- Dosyalar: `scp -P 22667 <dosya> root@77.245.146.216:/root/momentum-scanner/backend/...` veya `.../frontend/...` (VPS artık yerelle aynı backend/frontend/data yapısında, bkz. aşağıda).
- Servis: `systemctl restart web-bist.service` (restart gerekir, Flask şablon/kod değişikliklerini otomatik almaz).
- nginx reverse proxy 80/443'ten Flask'in 127.0.0.1:5000'ine yönlendiriyor, SSL orada sonlanıyor. Flask `host="127.0.0.1"` — dışarıdan doğrudan erişilemez, güvenlik için bilinçli.
- Domain: `momentumscanner.com.tr`.

**⚠️ KURAL: Kullanıcı açıkça onay vermeden VPS'e hiçbir şey gönderme (scp/systemctl restart), yerel testler geçse bile.** Her deploy için ayrı onay gerekir.

**⚠️ KURAL: Kullanıcı açıkça istemeden test çalıştırma** (yerel sunucu başlatma, Playwright, curl/canlı endpoint kontrolü). Değişikliği yap, syntax/mantık kontrolü yap, kullanıcıya "hazır, test edebilirsin" de — kullanıcı önce kendisi test edip takıldığı yerde soruyor.

## Mobil uygulama (`mobile/`)

Expo/React Native, WebView tabanlı (ayrı native ekranlar değil — doğrudan web sitesini sarmalıyor). Android paket adı `com.momentumscanner.app`, EAS proje ID `b40201f7-ff06-4118-8afb-0d90be816898`, owner `memo-can`. `mobile/AGENTS.md` içinde Expo SDK'nın değiştiğine dair bir uyarı var — kod yazmadan önce oradaki talimata uy.

**⚠️ Push bildirimi artık BURADAN gelmiyor (2026-10'da Telegram'a geçildi, bkz. "Bildirimler" altında).** Mobil app'in `getDevicePushTokenAsync()` ile backend'e kayıt olma kodu hâlâ `mobile/` içinde durabilir ama backend'deki `/api/register-device` endpoint'i SİLİNDİ — o çağrı artık 404 alır, sessizce başarısız olur. Mobil app'in kendisi (WebView ile siteyi gösterme) etkilenmez, sadece push-kayıt özelliği karşılıksız. Mobil app kodunu bu nedenle güncellemek ayrı bir görev, henüz yapılmadı.

## Bildirimler (Telegram — 2026-10'da Firebase'den geçildi)

GUCLU_AL/GUCLU_SAT sinyal geçişlerinde (`log_signal_changes()`, sadece durum DEĞİŞTİĞİNDE, spam yok) artık Telegram'a mesaj gönderiliyor — mobil push (Firebase Cloud Messaging) tamamen kaldırıldı, tek kanal bu.

- `scanner_bistTop100.py: _telegram_send(text)` — `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` ortam değişkenleriyle (sır, koda/repoya YAZILMAZ) `https://api.telegram.org/bot<TOKEN>/sendMessage`'a Python stdlib `urllib.request` ile POST yapan TEK ortak fonksiyon — yeni bir pip bağımlılığı YOK. İkisi de tanımlı değilse sessizce atlar (Telegram kurulmadan da tarama normal çalışır). `_send_push_notifications()` (sinyal geçişleri) VE `_send_paper_trade_notification()` (aşağıda) bunu kullanır.
- Hata (ağ/API) `print()` ile loglanır, exception fırlatılmaz — bildirim başarısız olsa da CSV/DB loglama (tarihsel kayıt) her zaman yazılmış olur.
- **Sanal Al-Sat alım/satım bildirimleri (2026-10-28'de eklendi):** `run_paper_trading_cycle()`'ın yaptığı TÜM alım/satımları (hem `source='auto'` hem `source='manual'`, 2026-10-31'de manuel SAT-sinyali satışları da dahil edildi) Telegram'a bildiriyor. **İstisna:** kullanıcının `manual_sell()` ile (API'den) doğrudan elle sattığı işlemler bu akışın İÇİNDEN GEÇMEZ (kullanıcı zaten ne yaptığını bildiği için) — bildirim SADECE `run_paper_trading_cycle()`'ın kendisinin tetiklediği satışlarda (stop/hedef/sinyal-tersi/süre-aşımı/manuel-SAT-sinyali) gönderiliyor. **Gönderim DB kilidi/commit'inden SONRA yapılır** (`trade_events` listesi `with _DB_LOCK:` bloğu İÇİNDE toplanır, bildirimler bloğun DIŞINDA gönderilir) — `log_signal_changes()`'teki aynı sebep: ağ isteği `_DB_LOCK`'u (paper trading tablolarına erişen diğer thread'leri/istekleri) gereksiz uzun tutmasın, gönderim başarısız olsa da işlem her zaman DB'ye yazılmış olsun.
- **İkon ailesi sinyal bildirimlerinden BİLİNÇLİ olarak farklı** (önce 🟢/🔴 daire denendi, kullanıcı telefon bildirim önizlemesinde sinyal mesajlarıyla karıştığını fark edince değiştirildi): alım 🛒 (sepet, giriş — henüz iyi/kötü değil), satım kâr ise 📈 zarar ise 📉 (`_send_paper_trade_notification()`'daki `pnl >= 0` kontrolü) — yukarı/aşağı grafik oku, trading botlarında kâr/zarar yönü için yaygın kullanılan ikon. Daireler (🟢/🔴) artık SADECE sinyal (GUCLU_AL/SAT) mesajlarına özel, trade mesajlarıyla şekil bazında hiç çakışmıyor.
- **GUCLU_SAT sinyal bildirimleri kullanıcı isteğiyle GEÇİCİ olarak kapatıldı (2026-10-30):** `_send_push_notifications()`'da `entry["score"] == "GUCLU_SAT"` ise `continue` ile Telegram gönderimi atlanıyor — GUCLU_AL bildirimleri hâlâ geliyor. **`signal_log` tablosuna yazım ETKİLENMEDİ** (bu fonksiyon DB yazımından SONRA çağrılıyor, bkz. `log_signal_changes()`) — sadece push gönderimi durdu, tarihsel kayıt kesintisiz sürüyor. Sebep: GUCLU_SAT, GUCLU_AL'dan çok daha sık tetikleniyor (örn. 2026-10-28'de aynı anda 102 GUCLU_SAT'a karşı 3 GUCLU_AL) ve Telegram'ı spam'liyordu. Tekrar açmak için `if entry["score"] == "GUCLU_SAT": continue` satırını kaldır.
- VPS'te `/etc/systemd/system/web-bist.service`'e `Environment=TELEGRAM_BOT_TOKEN=...` ve `Environment=TELEGRAM_CHAT_ID=...` eklenir (eski `FIREBASE_CREDENTIALS_PATH` satırı kaldırıldı).
- **Kaldırılanlar:** `firebase-admin` paketi (`requirements.txt`'ten de çıkarıldı), `load_device_tokens`/`save_device_tokens`/`register_device_token`/`prune_device_tokens`, `DEVICE_TOKENS_PATH`, `device_tokens_bist.json`'un okunması, `web_bistTop100.py: /api/register-device` endpoint'i. VPS'teki `firebase-service-account.json`/`device_tokens_bist.json` dosyaları SİLİNMEDİ (artık okunmuyor, zararsız, arşiv olarak duruyor).

**⚠️ Bilinen hata ve düzeltmesi (2026-10-28): restart'ta toplu yanlış bildirim.** `_last_logged_score` (ticker -> en son loglanan skor, `log_signal_changes()`'in "sadece durum DEĞİŞTİĞİNDE bildir" mantığının kalbi) process her (re)başladığında BOŞ başlıyordu — bu yüzden her deploy/restart'ta, o anda ZATEN GUCLU_AL/SAT durumunda olan TÜM hisseler (gerçekte hiçbir şey değişmemiş olsa bile) "yeni geçiş" sayılıp Telegram'a yeniden bildiriliyordu (gerçekten yaşandı: tek bir restart'ta 108 hisse aynı anda "yeni sinyal" olarak loglandı). **Düzeltme:** yeni `last_known_score` tablosu (`ticker PRIMARY KEY, score, timestamp` - tek satır/ticker, `log_score_history()` her döngüde upsert yapıyor) eklendi; `_seed_last_logged_scores()` process başlarken BU tablodan (büyük `score_history`'yi TARAMADAN, O(1)) `_last_logged_score`'u doldurur. **Neden ayrı bir tablo, neden `score_history`'den `MAX(id) GROUP BY ticker` değil:** o sorgu VPS'teki 3.4M+ satırlık tabloda ~12 saniye sürüyordu (her restart'ı 12sn geciktirirdi) - `last_known_score` en fazla ~630 satır (bir ticker için bir satır) olduğu için sorgu anlık.

## Kullanıcı girişi (2026-10'da eklendi — SADECE belirli bölümler için)

Site büyük ölçüde herkese açık kalıyor — login sadece İKİ şey için gerekli: (1) sütun başlıklarındaki detaylı açıklama balonları (`data-tip` tooltip'leri), (2) Sanal Al-Sat bölümü. Çoklu kullanıcı/kayıt sistemi YOK, tek sabit admin hesabı.

- Flask'ın kendi imzalı çerez tabanlı `session`'ı kullanılıyor — yeni bir DB tablosu yok. Ortam değişkenleri (sır, koda/repoya YAZILMAZ — Telegram/Firebase ile AYNI desen): `FLASK_SECRET_KEY` (session imzalama), `ADMIN_USERNAME` (varsayılan `admin`), `ADMIN_PASSWORD_HASH` (werkzeug `generate_password_hash` çıktısı, DÜZ METİN DEĞİL).
- `session.permanent=True` + `PERMANENT_SESSION_LIFETIME=365 gün` — login bir kez yapılınca tarayıcıda ~1 yıl kalıcı kalır.
- `web_bistTop100.py: /login` (GET form, POST doğrulama), `/logout`. `_login_required` decorator'ı `/api/paper/*` endpoint'lerini gerçekten korur (sadece HTML gizleme değil — login olmadan bu API'lere direkt istek de 401 döner).
- **Sanal Al-Sat** artık ayrı bir sayfa (`/sanal-al-sat`, bkz. yukarıdaki "Ayrı sayfa" notu) — login olmayana bu route `/login`'e REDIRECT eder, HTML'i hiç göndermez (gerçek sunucu-taraflı gizleme). GunStop/GunHedef/SwStop/SwHedef sütunları (2026-10-28'de eklendi) ise bunun TERSİ, tooltip'lerle aynı HAFİF yöntemle gizlenir (aşağıdaki madde).
- **Tooltip'ler ve stop/hedef sütunları farklı/daha hafif bir yöntemle gizlenir:** `data-tip` metni ve GunStop/GunHedef/SwStop/SwHedef verisi HTML/API'de HER ZAMAN var (view-source'tan/`/api/rows`'tan görülebilir) ama GÖRSEL olarak sadece `<body class="logged-in">` iken açılır (`thead th[data-tip]:hover::after` VE `.login-only-col` class'lı th/td çiftleri — `body:not(.logged-in) .login-only-col { display: none; }`). Bilinçli bir tradeoff — bunlar düşük hassasiyetli içerik (gösterge açıklamaları, stop/hedef fiyatları) olduğu için tam sunucu-taraflı gizlemeye değmedi, Sanal Al-Sat için (gerçek "değer" taşıyan kısım) gerçek gizleme kullanıldı.
- Frontend'de `const LOGGED_IN` JS sabiti `refreshPaperState()`'in çağrılmasını/interval'ini korur — login değilken anlamsız 401 isteği atılmaz.
- VPS deploy: `systemd` servis dosyasına 3 yeni `Environment=` satırı (`FLASK_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`) eklenir.
- Çıkış Yap linkine (`#auth-link`, hem `index.html` hem `paper_trading.html`'de) tıklanınca `confirm()` ile onay sorulur (2026-10-28'de eklendi) — yanlışlıkla tıklanıp session'ın sıfırlanmasını (ve lokalde her hisse detayinin/Sanal Al-Sat'in tekrar login gerektirmesini) önlemek için.
- **Lokal otomatik giriş (2026-10-28'de eklendi):** `ADMIN_PASSWORD_HASH` ortam değişkeni tanımlı DEĞİLSE (`_LOCAL_AUTO_LOGIN = not ADMIN_PASSWORD_HASH`, `web_bistTop100.py`) kullanıcı adı/şifre HİÇ sorulmadan otomatik giriş yapılmış sayılır — `_is_logged_in()` bu durumda session'a bakmadan `True` döner, `/login` GET/POST'ta da kullanıcı adı/şifreyi kontrol etmeden direkt `/`'e yönlendirir. VPS'te systemd bu değişkeni HER ZAMAN env ile verdiği için (`_LOCAL_AUTO_LOGIN=False`) canlıda davranış DEĞİŞMEDİ — bu sadece ADMIN_PASSWORD_HASH export edilmemiş lokal geliştirme ortamları için. **Lokalde gerçek login akışını (şifre hatası, vs.) test etmek istersen** `ADMIN_PASSWORD_HASH`'i (ve `ADMIN_USERNAME`/`FLASK_SECRET_KEY`'i) terminalde `export` edip bu otomatik girişi geçersiz kılman gerekir.

## Bilinen tuzaklar / dersler (tekrar düşmemek için)

- **Yahoo rate-limit:** Tam piyasa (~628 hisse × 3 istek/hisse) taraması art arda/sık yapılınca `YFRateLimitError` ve "Invalid Crumb" hatalarına yol açıyor. Hem yerel sandbox hem VPS bunu yaşadı. Büyük hacimli canlı test yapmadan önce düşün; gerçekten gerekliyse VPS'in kendi (genelde daha "temiz") IP'sinden doğal 5 dakikalık döngülerle gözlemle, art arda patlamalar yapma.
- **Yahoo intraday veri limitleri:** 5 dakikalık (`5m`) veri sadece ~60 gün geriye gidiyor; `1h` veri ~1-2 yıl geriye gidiyor; `1d` sınırsız. Geçmişe dönük saatlik/günlük analiz isterken bunu hesaba kat.
- **Stale local process:** Yerel test sırasında eski bir `web_bistTop100.py` süreci port 5000'i tutmaya devam edebilir, yeni başlattığın süreç sessizce bağlanamayabilir ve eski/yanlış veri test ediliyormuş gibi görünebilir. Her zaman `lsof -i :5000` ile GERÇEK PID'i doğrula, sadece kendi başlattığın PID'e güvenme.
- **`.venv` kırılabilir:** Sistem Python'ı güncellenirse (örn. 3.12→3.14) venv'in sembolik bağlantıları bozulur, `ModuleNotFoundError` verir görünüşte paket kuruluyken. Çözüm: `.venv`'i silip `python3 -m venv .venv` ile yeniden oluşturup `pip install -r requirements.txt`.
- **`requirements.txt`'e asla sır/şifre ekleme** — bir keresinde yanlışlıkla bir DigitalOcean DB şifresi buraya yapıştırılmıştı (commit edilmeden yakalandı). Bu dosya sadece paket isimleri içermeli.
- **Sandbox scratchpad yolu session'a özel** — eski bir session'daki `/tmp/claude-.../<session-id>/scratchpad` yolu yeni bir session'da geçersiz olabilir (günler sonra devam eden oturumlarda ortam resetlenebiliyor). Dosya yazmadan önce yolun var olduğunu doğrula.
- **lightweight-charts'ın crosshair `time` değeri string OLMAYABİLİR:** `subscribeCrosshairMove()`'un `param.time`'ı, seriye "YYYY-MM-DD" string verilmiş olsa bile bazen `{year,month,day}` NESNESİ olarak geliyor — backend'den gelen orijinal string'lerle anahtarlanmış bir `Map`'te doğrudan `.get(param.time)` yaparsan (nesne referans eşitliği asla string'le eşleşmediği için) SESSİZCE `undefined` döner, hata fırlatmaz. `frontend/templates/index.html: timeKey()` bu yüzden her zaman önce `"YYYY-MM-DD"` string'ine normalize ediyor (bkz. "Ana/MACD/RSI grafik crosshair senkronizasyonu ve canlı gösterge legend'i").
