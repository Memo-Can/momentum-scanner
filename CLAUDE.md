# CLAUDE.md

Bu dosya, bu repo üzerinde çalışan Claude Code oturumları için kalıcı talimat/bağlam dosyasıdır. Koddan zaten çıkarılabilecek şeyleri (fonksiyon isimleri, satır numaraları vb.) tekrarlamaz — onun yerine mimariyi, kuralları ve tekrar tekrar aynı hataya düşülmesini engelleyecek dersleri içerir.

## Proje nedir

Borsa İstanbul (BIST) hisseleri için canlı momentum/sinyal tarayıcısı. Web arayüzü (Flask) `https://momentumscanner.com.tr` adresinde VPS üzerinde çalışıyor; ayrıca bir terminal arayüzü ve bir Android mobil companion app (push bildirimli) var.

## Klasör yapısı (Ekim 2026'da backend/frontend ayrımına geçildi)

```
backend/
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

VPS de bu yapıya göçürüldü (2026-10-23): `/root/momentum-scanner/backend/` (+ `data/`) ve `/root/momentum-scanner/frontend/` artık yerelle aynı düzende. `systemd` servis dosyası (`/etc/systemd/system/web-bist.service`) `WorkingDirectory=/root/momentum-scanner/backend`, `ExecStart=/root/momentum-scanner/venv/bin/python3 /root/momentum-scanner/backend/web_bistTop100.py`, `Environment=FIREBASE_CREDENTIALS_PATH=/root/momentum-scanner/backend/firebase-service-account.json` olacak şekilde güncellendi. Venv (`/root/momentum-scanner/venv/`) kasıtlı olarak `backend/`'in İÇİNE taşınmadı — dışarıda kalması sorun değil, yol zaten mutlak. Göç sırasında canlı CSV'ler (`score_history_bist.csv`, `signal_log_bist.csv`) checksum ile doğrulanarak taşındı, veri kaybı olmadı.

**`backend/diger-piyasalar/` klasörü (US/Crypto scanner'lar) KAPSAM DIŞI.** Kullanıcı sadece BIST'e odaklanmaya karar verdi, bu dosyalara dokunma, aktif geliştirme orada yapılmıyor. Sadece klasör düzeni için `backend/` altına taşındı (2026-10-23) — içindeki `web_usTop100.py`'nin `templates`/`static` yolu halihazırda eski/kullanılmayan bir referans (BIST tarafının `frontend/`'e taşınmasından önce de iki üst dizine göre hesaplanıyordu), bu dosya çalıştırılmadığı için düzeltilmedi.

## Hisse evreni: BIST 30 / BIST 100 / BIST 300 sekmeleri

- **`BIST30_TICKERS`** ve **`BIST100_TICKERS`** (`scanner_bistTop100.py`): resmi endeks üyeliğinin **sabit, elle girilmiş** listeleri (Midas + Yahoo `region=tr` evrenine çapraz kontrol edilerek doğrulandı, 2026-09). BIST30 ⊆ BIST100 (doğrulandı) — bu yüzden BIST100 taraması otomatik olarak BIST30'u da kapsar, ayrı istek gerekmez. **Endeks çeyreklik yeniden dengeleniyor, bu listelerin zamanla elle güncellenmesi gerekebilir.**
- **BIST 300 sekmesi resmi bir endeks DEĞİL** — günlük % değişime göre (yüksekten düşüğe) dinamik ilk `TOP300_SIZE=300` hisse (`fetch_top_gainers()`).
- Her tarama döngüsünde tek bir **birleşik havuz** çekilir: `BIST100_TICKERS ∪ fetch_top_gainers(300)`, çakışanlar tekrar çekilmez (`web_bistTop100.py: _scan_once()`). Her satır `bist30`/`bist100` bayraklarıyla etiketlenir; BIST 300 sekmesi bu havuzun tamamından türetilir (frontend'de `applyMarketFilter()`).
- **Bunu TÜM piyasayı (~628 hisse) taramaya GENİŞLETME** — bu zaten denendi ve Yahoo'nun resmi olmayan API'sini `YFRateLimitError` ile tetikledi (bkz. "Bilinen tuzaklar" altında). Birleşik havuz (~300-400 hisse) bu riski kabul edilebilir seviyede tutmak için bilinçli bir tasarım kararı.

## Skor sistemi

Confluence Score: `confluence_score_raw()` (-6..+6) + `confluence_label()` (GUCLU_AL/AL/NOTR/SAT/GUCLU_SAT). `ADX_TREND_THRESHOLD=20` ve `RS_THRESHOLD=0.5` sadece **etiketi** etkiler, hiçbir hisseyi listeden ELEMEZ — liste filtresi değil, güven eşiğidir.

Varsayılan tablo sıralaması: önce Skor gücü (GUCLU_AL üstte), aynı grup içinde günlük % (yüksekten düşüğe). Bu, `frontend/templates/index.html`'deki `applySort()`'un `sortState.key === null` dalında ve backend'in `_sort_key()`'inde **birebir aynı** tutulmalı.

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
- **Çıkış kuralı (ilk uyan kazanır):** `price <= stop_price` → `'stop'`; `price >= target_price` → `'target'`; skor gerçekten `GUCLU_SAT`'a dönerse → `'signal_reversal'`; `PAPER_MAX_HOLD_DAYS` (takvim günü yaklaşımı, gerçek işlem günü sayacı DEĞİL) aşılırsa → `'time_stop'`. Stop/hedef değerleri zaten var olan `swing_stop`/`swing_target` (ATR×3, 1:2 risk-ödül) sütunlarından geliyor — yeni bir ATR formülü YOK.
- **Pozisyon büyüklüğü:** Her yeni alım `min(PAPER_STARTING_BALANCE/PAPER_MAX_POSITIONS, mevcut nakit)` kadar (nakit yetmezse kısılır) — her zaman 5 slot dolu tutulmaya çalışılır, biri kapanınca aynı döngüde (ya da bir sonraki uygun adayla) yeni pozisyon açılabilir.
- **API:** `/api/paper/summary`, `/api/paper/positions` (canlı fiyat/K-Z `_state['rows']` ile zenginleştirilir), `/api/paper/trades`, `/api/paper/equity`. Frontend'de `frontend/templates/index.html`'deki `#paper-trading` bölümü (ana tablonun ALTINDA, ayrı bir section — BIST sekmelerinden bağımsız).

## Deploy süreci (VPS)

- VPS: `77.245.146.216`, SSH portu **22667** (standart 22 DEĞİL — port değişmiş durumda, `ssh -i ~/.ssh/momentum_vps_key -p 22667 root@77.245.146.216`).
- Dosyalar: `scp -P 22667 <dosya> root@77.245.146.216:/root/momentum-scanner/backend/...` veya `.../frontend/...` (VPS artık yerelle aynı backend/frontend/data yapısında, bkz. aşağıda).
- Servis: `systemctl restart web-bist.service` (restart gerekir, Flask şablon/kod değişikliklerini otomatik almaz).
- nginx reverse proxy 80/443'ten Flask'in 127.0.0.1:5000'ine yönlendiriyor, SSL orada sonlanıyor. Flask `host="127.0.0.1"` — dışarıdan doğrudan erişilemez, güvenlik için bilinçli.
- Domain: `momentumscanner.com.tr`.

**⚠️ KURAL: Kullanıcı açıkça onay vermeden VPS'e hiçbir şey gönderme (scp/systemctl restart), yerel testler geçse bile.** Her deploy için ayrı onay gerekir.

**⚠️ KURAL: Kullanıcı açıkça istemeden test çalıştırma** (yerel sunucu başlatma, Playwright, curl/canlı endpoint kontrolü). Değişikliği yap, syntax/mantık kontrolü yap, kullanıcıya "hazır, test edebilirsin" de — kullanıcı önce kendisi test edip takıldığı yerde soruyor.

## Mobil uygulama (`mobile/`)

Expo/React Native, WebView tabanlı (ayrı native ekranlar değil — doğrudan web sitesini sarmalıyor). Push bildirimleri Firebase Cloud Messaging üzerinden (Expo'nun kendi push servisi DEĞİL — `getDevicePushTokenAsync()` kullanılıyor, `getExpoPushTokenAsync()` değil). Android paket adı `com.momentumscanner.app`, EAS proje ID `b40201f7-ff06-4118-8afb-0d90be816898`, owner `memo-can`. `mobile/AGENTS.md` içinde Expo SDK'nın değiştiğine dair bir uyarı var — kod yazmadan önce oradaki talimata uy.

## Bilinen tuzaklar / dersler (tekrar düşmemek için)

- **Yahoo rate-limit:** Tam piyasa (~628 hisse × 3 istek/hisse) taraması art arda/sık yapılınca `YFRateLimitError` ve "Invalid Crumb" hatalarına yol açıyor. Hem yerel sandbox hem VPS bunu yaşadı. Büyük hacimli canlı test yapmadan önce düşün; gerçekten gerekliyse VPS'in kendi (genelde daha "temiz") IP'sinden doğal 5 dakikalık döngülerle gözlemle, art arda patlamalar yapma.
- **Yahoo intraday veri limitleri:** 5 dakikalık (`5m`) veri sadece ~60 gün geriye gidiyor; `1h` veri ~1-2 yıl geriye gidiyor; `1d` sınırsız. Geçmişe dönük saatlik/günlük analiz isterken bunu hesaba kat.
- **Stale local process:** Yerel test sırasında eski bir `web_bistTop100.py` süreci port 5000'i tutmaya devam edebilir, yeni başlattığın süreç sessizce bağlanamayabilir ve eski/yanlış veri test ediliyormuş gibi görünebilir. Her zaman `lsof -i :5000` ile GERÇEK PID'i doğrula, sadece kendi başlattığın PID'e güvenme.
- **`.venv` kırılabilir:** Sistem Python'ı güncellenirse (örn. 3.12→3.14) venv'in sembolik bağlantıları bozulur, `ModuleNotFoundError` verir görünüşte paket kuruluyken. Çözüm: `.venv`'i silip `python3 -m venv .venv` ile yeniden oluşturup `pip install -r requirements.txt`.
- **`requirements.txt`'e asla sır/şifre ekleme** — bir keresinde yanlışlıkla bir DigitalOcean DB şifresi buraya yapıştırılmıştı (commit edilmeden yakalandı). Bu dosya sadece paket isimleri içermeli.
- **Sandbox scratchpad yolu session'a özel** — eski bir session'daki `/tmp/claude-.../<session-id>/scratchpad` yolu yeni bir session'da geçersiz olabilir (günler sonra devam eden oturumlarda ortam resetlenebiliyor). Dosya yazmadan önce yolun var olduğunu doğrula.
