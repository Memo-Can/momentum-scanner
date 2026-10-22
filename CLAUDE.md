# CLAUDE.md

Bu dosya, bu repo üzerinde çalışan Claude Code oturumları için kalıcı talimat/bağlam dosyasıdır. Koddan zaten çıkarılabilecek şeyleri (fonksiyon isimleri, satır numaraları vb.) tekrarlamaz — onun yerine mimariyi, kuralları ve tekrar tekrar aynı hataya düşülmesini engelleyecek dersleri içerir.

## Proje nedir

Borsa İstanbul (BIST) hisseleri için canlı momentum/sinyal tarayıcısı. Web arayüzü (Flask) `https://momentumscanner.com.tr` adresinde VPS üzerinde çalışıyor; ayrıca bir terminal arayüzü ve bir Android mobil companion app (push bildirimli) var.

## Katman mimarisi (önemli — her değişiklikte doğru dosyaya dokun)

```
scanner_bistTop100.py    → İŞ KATMANI (sunum içermez). Veri çekme, indikatör/skor
                            hesaplama, CSV loglama, push bildirim, piyasa göstergeleri.
                            Doğrudan çalıştırılmaz.
web_bistTop100.py        → WEB sunum katmanı (Flask). AKTİF OLARAK DEPLOY EDİLEN budur.
terminal_bistTop100.py   → Terminal sunum katmanı. scanner'ı import edip kullanır.
templates/index.html     → Tek sayfalık web UI (tablo + sekmeler + grafik + skor geçmişi
                            + piyasa şeridi). Tüm JS/CSS bu dosyanın içinde (inline).
backfill_daily_bist.py   → Tek seferlik, elle çalıştırılan geçmişe dönük günlük skor
                            backfill script'i (bkz. aşağıda "Skor geçmişi").
```

`scanner_bistTop100.py` + `web_bistTop100.py` + `terminal_bistTop100.py` aynı iş mantığını paylaşır — biri değişince diğerlerinde de tutarlılık kontrolü yap.

**`diger-piyasalar/` klasörü (US/Crypto scanner'lar) KAPSAM DIŞI.** Kullanıcı sadece BIST'e odaklanmaya karar verdi, bu dosyalara dokunma, aktif geliştirme orada yapılmıyor.

## Hisse evreni: BIST 30 / BIST 100 / BIST 300 sekmeleri

- **`BIST30_TICKERS`** ve **`BIST100_TICKERS`** (`scanner_bistTop100.py`): resmi endeks üyeliğinin **sabit, elle girilmiş** listeleri (Midas + Yahoo `region=tr` evrenine çapraz kontrol edilerek doğrulandı, 2026-09). BIST30 ⊆ BIST100 (doğrulandı) — bu yüzden BIST100 taraması otomatik olarak BIST30'u da kapsar, ayrı istek gerekmez. **Endeks çeyreklik yeniden dengeleniyor, bu listelerin zamanla elle güncellenmesi gerekebilir.**
- **BIST 300 sekmesi resmi bir endeks DEĞİL** — günlük % değişime göre (yüksekten düşüğe) dinamik ilk `TOP300_SIZE=300` hisse (`fetch_top_gainers()`).
- Her tarama döngüsünde tek bir **birleşik havuz** çekilir: `BIST100_TICKERS ∪ fetch_top_gainers(300)`, çakışanlar tekrar çekilmez (`web_bistTop100.py: _scan_once()`). Her satır `bist30`/`bist100` bayraklarıyla etiketlenir; BIST 300 sekmesi bu havuzun tamamından türetilir (frontend'de `applyMarketFilter()`).
- **Bunu TÜM piyasayı (~628 hisse) taramaya GENİŞLETME** — bu zaten denendi ve Yahoo'nun resmi olmayan API'sini `YFRateLimitError` ile tetikledi (bkz. "Bilinen tuzaklar" altında). Birleşik havuz (~300-400 hisse) bu riski kabul edilebilir seviyede tutmak için bilinçli bir tasarım kararı.

## Skor sistemi

Confluence Score: `confluence_score_raw()` (-6..+6) + `confluence_label()` (GUCLU_AL/AL/NOTR/SAT/GUCLU_SAT). `ADX_TREND_THRESHOLD=20` ve `RS_THRESHOLD=0.5` sadece **etiketi** etkiler, hiçbir hisseyi listeden ELEMEZ — liste filtresi değil, güven eşiğidir.

Varsayılan tablo sıralaması: önce Skor gücü (GUCLU_AL üstte), aynı grup içinde günlük % (yüksekten düşüğe). Bu, `templates/index.html`'deki `applySort()`'un `sortState.key === null` dalında ve backend'in `_sort_key()`'inde **birebir aynı** tutulmalı.

## Skor geçmişi (performans kritik — DİKKAT)

`score_history_bist.csv` canlı sunucuda **sürekli büyüyor** (her 5 dakikada bir havuzdaki her hisse için bir satır) — Ekim 2026 itibariyla 130MB+/3.2M+ satır. **Bu dosyayı asla per-request olarak baştan tarama** — `web_bistTop100.py` bunu bellek içi bir index'te tutuyor (`_live_score_index`, uygulama başlarken bir kez okunur, sonra her tarama döngüsünde sadece yeni satırlar eklenir). Bu deseni bozacak bir değişiklik yaparsan grafik açılışı yeniden saniyeler sürecek şekilde yavaşlar (bu gerçekten yaşandı, Ekim 2026).

`score_history_bist_backfill.csv` (statik, `backfill_daily_bist.py` ile üretildi) + canlı log, `SCORE_HISTORY_WINDOW_DAYS=365` penceresiyle birleştirilip "her zaman son 1 yıl" gösterir — backfill script'i tekrar çalıştırmaya gerek yok, pencere otomatik kayıyor.

## Deploy süreci (VPS)

- VPS: `77.245.146.216`, SSH portu **22667** (standart 22 DEĞİL — port değişmiş durumda, `ssh -i ~/.ssh/momentum_vps_key -p 22667 root@77.245.146.216`).
- Dosyalar: `scp -P 22667 <dosya> root@77.245.146.216:/root/momentum-scanner/...`
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
