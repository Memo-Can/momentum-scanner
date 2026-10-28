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

VPS de bu yapıya göçürüldü (2026-10-23): `/root/momentum-scanner/backend/` (+ `data/`) ve `/root/momentum-scanner/frontend/` artık yerelle aynı düzende. `systemd` servis dosyası (`/etc/systemd/system/web-bist.service`) `WorkingDirectory=/root/momentum-scanner/backend`, `ExecStart=/root/momentum-scanner/venv/bin/python3 /root/momentum-scanner/backend/web_bistTop100.py`, (Firebase kullanılırken `Environment=FIREBASE_CREDENTIALS_PATH=...` vardı, 2026-10'da Telegram'a geçişle KALDIRILDI — bkz. aşağıda "Bildirimler", artık `Environment=TELEGRAM_BOT_TOKEN=...` ve `Environment=TELEGRAM_CHAT_ID=...` var). Venv (`/root/momentum-scanner/venv/`) kasıtlı olarak `backend/`'in İÇİNE taşınmadı — dışarıda kalması sorun değil, yol zaten mutlak. Göç sırasında canlı CSV'ler (`score_history_bist.csv`, `signal_log_bist.csv`) checksum ile doğrulanarak taşındı, veri kaybı olmadı.

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
- **API:** `/api/paper/summary`, `/api/paper/positions` (canlı fiyat/K-Z `_state['rows']` ile zenginleştirilir), `/api/paper/trades`, `/api/paper/equity`. Frontend'de `frontend/templates/index.html`'deki `#paper-trading` bölümü (ana tablonun ALTINDA, ayrı bir section — BIST sekmelerinden bağımsız), başlığa tıklanınca daralır/genişler (tercih `localStorage`'da tutulur, sadece tarayıcı bazlı).
- VPS'e deploy edildi (2026-10-23) — `paper_*` tabloları `CREATE TABLE IF NOT EXISTS` ile ilk `import`'ta otomatik oluştu, manuel migrasyon gerekmedi. Deploy anında (restart ile `_paper_last_score` sıfırlandığı için) hiçbir pozisyon otomatik açılmadı, doğrulandı.

### Manuel kontrol (2026-10-28'de eklendi) — otomatik sistemden BAĞIMSIZ

Kullanıcı artık nakti elle düzenleyebiliyor, herhangi bir hisseyi "manuel takip listesi"ne ekleyip o listeden istediği an elle alıp satabiliyor. Bu, yukarıdaki otomatik sistemle (`run_paper_trading_cycle`) **karışmaz**:

- `paper_trades`'e **`source`** sütunu eklendi (`'auto'`/`'manual'`, var olan tablo `ALTER TABLE` ile migrate edildi — `PRAGMA table_info` kontrolüyle idempotent, `_init_db()`'de). Mevcut (deploy öncesi açılmış) satırlar otomatik `'auto'` sayılır.
- **`run_paper_trading_cycle()`'ın çıkış kontrolü SADECE `source='auto'` satırları işler** — manuel pozisyonlara stop/hedef/GUCLU_SAT/süre kuralı hiç UYGULANMAZ, sadece kullanıcı elle "Sat" dediğinde kapanır (`manual_sell()`). `positions_value`/equity hesaplaması ise DEĞİŞMEDİ — hem auto hem manual açık pozisyonları sayar (toplam portföy değeri ikisini de yansıtmalı).
- Yeni tablo: `paper_watchlist(ticker PRIMARY KEY, added_at)` — kullanıcının elle eklediği, henüz almadığı hisseler. `web_bistTop100.py: _scan_once()` bu ticker'ları da (açık pozisyonlarla AYNI sebeple — bkz. yukarıdaki USHOL notu) her döngüde tarama havuzuna ekler, yoksa fiyatsız kalır.
- Manuel alımda tutar HER ALIMDA elle girilir (otomatik sistemin sabit 1/5 payı YOK) — `manual_buy(ticker, allocation, price)`, tek sınırı mevcut nakit, 5-slot limitine dahil DEĞİL.
- Yeni API'ler (`web_bistTop100.py`, hepsi `@_login_required`): `POST /api/paper/cash`, `POST /api/paper/reset` (body `{amount}`), `GET/POST /api/paper/watchlist`, `DELETE /api/paper/watchlist/<ticker>`, `POST /api/paper/manual-buy`, `POST /api/paper/manual-sell`.
- Frontend: yan panelde (`#side-content`) "Sanal Al-Sat Takibine Ekle/Çıkar" butonu; Sanal Al-Sat panelinde nakit düzenleme input'u + "Her Şeyi Sıfırla" butonu (JS `confirm()` ile onay ister) + "Manuel Takip Listesi" alt-bölümü; Açık Pozisyonlar/Kapanan İşlemler tablolarına "Kaynak" (Otomatik/Manuel) sütunu, manuel açık pozisyonlara "Sat" butonu.
- **`reset_paper_trading(starting_balance)`:** `paper_trades`/`paper_equity_history`/`paper_watchlist` tamamen SİLİNİR, `cash` VE `starting_balance` (K/Z'nin ölçüldüğü baz) verilen tutara eşitlenir — **sabit `PAPER_STARTING_BALANCE` değil**, kullanıcı her sıfırlamada kendi başlangıç tutarını seçer (frontend "Yeni nakit" kutusundaki değeri kullanır, boşsa `prompt()` ile sorar). `_paper_last_score` KASITLI dokunulmaz (yoksa restart-hatasıyla aynı sebepten sıfırlama sonrası toplu yanlış alım tetiklenir).

### Ayrı sayfa (2026-10-28'de taşındı) — artık ana tablonun ALTINDA değil

Sanal Al-Sat bölümü (özet/nakit düzenleme/manuel takip listesi/açık pozisyonlar/kapanan işlemler) ana sayfadan (`index.html`) çıkarılıp kendi başına bir sayfaya (`frontend/templates/paper_trading.html`, route: `web_bistTop100.py: /sanal-al-sat`) taşındı. Ana sayfada header'a (`#header-controls`) sadece login olana görünen bir **"Sanal Al-Sat"** linki eklendi.

- `/sanal-al-sat` route'u `_login_required` (API'lerin kullandığı, JSON 401 dönen) decorator'ını KULLANMIYOR — çünkü bu bir HTML sayfası, JSON hatası anlamsız olurdu. Kendi `if not session.get("logged_in"): return redirect(url_for("login"))` kontrolüyle login'e yönlendiriyor.
- **İki ayrı JS dosyası/script bloğu var, paylaşılan bir frontend modülü YOK** (bu projede zaten yok, bilinçli tutarlılık): `paper_trading.html`'in kendi `<script>` bloğu `fmtPaperPrice`/`renderPaperPositions`/`refreshPaperTrading()` gibi TÜM render fonksiyonlarını içeriyor; `index.html`'deki eski aynı isimli fonksiyonlar SİLİNDİ, yerine çok daha hafif bir **`refreshPaperState()`** geldi.
- **`refreshPaperState()`'in var olma sebebi:** ana sayfanın (artık UI'si olmayan) Sanal Al-Sat'tan hâlâ ihtiyacı olan İKİ şey var — (1) `openPaperTickers`/`watchlistTickers` setleri (BIST 300 sekmesinin açık pozisyon/takip listesi hisselerini havuzdan düşürmemesi için, bkz. `applyMarketFilter()`), (2) yan paneldeki (`#side-content`) "Sanal Al-Sat Takibine Ekle/Çıkar" butonunun (`#watchlist-toggle`) durumu. Bu yüzden `addToWatchlist`/`removeFromWatchlist`/`updateWatchlistToggleButton` hâlâ `index.html`'de duruyor (yan paneldeki buton oradan çalışıyor), ama `/api/paper/positions` + `/api/paper/watchlist`'i sadece bu iki Set/buton için çeken `refreshPaperState()` ile — `renderPaperSummary`/`renderPaperPositions`/`renderPaperTrades`/`renderPaperWatchlist` gibi ağır render fonksiyonları YOK, çünkü ana sayfada artık onları gösterecek bir DOM elementi yok.
- `#paper-trading` section'ının CSS'i (`position: sticky; bottom: 0`, daraltma/genişletme) HER İKİ sayfada da aynı ID ile kullanılıyor (stil tekrarı istenmedi) — `style.css`'te `body.paper-page #paper-trading` override'ı (`position: static`, ortalanmış `max-width:900px`) bu sticky/sayfa-içi davranışı yeni sayfada geçersiz kılıyor. Daraltma butonu (`#paper-toggle`) yeni sayfada hiç YOK — kendi başına tam sayfa olduğu için daraltmaya gerek kalmadı, bu yüzden `paper_trading.html`'de `initPaperToggle()` IIFE'si de yok.
- Pozisyon/işlem/takip listesi satırlarına tıklamak (`tbody tr { cursor: pointer }`) artık `selectTicker()` çağırmıyor (o sayfada grafik paneli yok) — `window.open('/#TICKER', '_blank')` ile ana sayfayı o hissenin grafiği açık halde YENİ SEKMEDE açıyor (`index.html`'in zaten desteklediği `#ticker` hash'i, bkz. "hash url added" commit).

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
- **Sanal Al-Sat alım/satım bildirimleri (2026-10-28'de eklendi):** `run_paper_trading_cycle()` artık SADECE `source='auto'` alım/satımları (`_paper_last_score` ile tetiklenen otomatik GUCLU_AL alımları ve stop/hedef/sinyal-tersi/süre-aşımı satımları) Telegram'a da bildiriyor — manuel alım/satımda (kullanıcı zaten ne yaptığını bildiği için) bildirim YOK. **Gönderim DB kilidi/commit'inden SONRA yapılır** (`trade_events` listesi `with _DB_LOCK:` bloğu İÇİNDE toplanır, bildirimler bloğun DIŞINDA gönderilir) — `log_signal_changes()`'teki aynı sebep: ağ isteği `_DB_LOCK`'u (paper trading tablolarına erişen diğer thread'leri/istekleri) gereksiz uzun tutmasın, gönderim başarısız olsa da işlem her zaman DB'ye yazılmış olsun.
- **İkon ailesi sinyal bildirimlerinden BİLİNÇLİ olarak farklı** (önce 🟢/🔴 daire denendi, kullanıcı telefon bildirim önizlemesinde sinyal mesajlarıyla karıştığını fark edince değiştirildi): alım 🛒 (sepet, giriş — henüz iyi/kötü değil), satım kâr ise 📈 zarar ise 📉 (`_send_paper_trade_notification()`'daki `pnl >= 0` kontrolü) — yukarı/aşağı grafik oku, trading botlarında kâr/zarar yönü için yaygın kullanılan ikon. Daireler (🟢/🔴) artık SADECE sinyal (GUCLU_AL/SAT) mesajlarına özel, trade mesajlarıyla şekil bazında hiç çakışmıyor.
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
