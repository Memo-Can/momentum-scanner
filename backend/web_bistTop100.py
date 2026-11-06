#!/usr/bin/env python3
"""scanner_bistTop100.py'nin canli verisini yerel bir web arayuzunde gosterir.

Mevcut scanner_bistTop100 modulunun tum tarama/indikator mantigini (fetch_row,
fetch_with_retry, confluence_label vb.) OLDUGU GIBI yeniden kullanir - burada
sadece bu veriyi bir arka plan thread'inde BIST_REFRESH_SECONDS'ta bir tazeleyip
basit bir JSON API + HTML sayfasi uzerinden sunuyoruz. Tarama mantigi
degismedi, sadece terminal yerine tarayicida gosteriliyor.

Lokal gelistirme icin calistirmak:
    python3 web_bistTop100.py
Sonra tarayicida: http://127.0.0.1:5000

Production'da (VPS) artik gunicorn kullaniliyor (bkz. CLAUDE.md "Deploy
sureci" - TEK worker VE yuksek timeout ZORUNLU, nedeni orada aciklaniyor):
    gunicorn -w 1 --worker-class gthread --threads 4 --timeout 180 -b 127.0.0.1:5000 web_bistTop100:app

Sayfa acikken tablo BIST_REFRESH_SECONDS'a yakin bir surede kendiliginden
yenilenir (JS polling ile /api/rows'a istek atar). Bir hisseye tiklayinca
sag panelde 3 aylik mum grafigi + istege bagli indikator katmanlari
(EMA/Bollinger/MACD/RSI) + Skor Gecmisi (gunluk/saatlik/5 dakikalik) gosterilir.
"""

import functools
import os
import threading
import time
from datetime import datetime, timedelta

import scanner_bistTop100 as scanner
import settings
import yfinance as yf
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
app = Flask(
    __name__,
    template_folder=os.path.join(_FRONTEND_DIR, "templates"),
    static_folder=os.path.join(_FRONTEND_DIR, "static"),
)
# nginx reverse proxy X-Real-IP/X-Forwarded-For/X-Forwarded-Proto gonderiyor
# (bkz. /etc/nginx/sites-available) - bu olmadan request.remote_addr HER ZAMAN
# nginx'in kendi IP'sini (127.0.0.1) gorur, asagidaki login rate-limit'i de
# gercek istemciyi degil tek bir "sahte" IP'yi sinirlardi.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# Sadece tooltip'ler + Sanal Al-Sat bolumu icin tek-kullanicili basit giris -
# coklu kullanici/kayit sistemi YOK. Sir (hash) asla koda/repoya yazilmaz,
# Telegram bot token/Firebase kimlik dosyasiyla AYNI desen: ortam degiskeni.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")
# VPS'teki systemd servisi ADMIN_PASSWORD_HASH'i HER ZAMAN env degiskeniyle
# verir (bkz. CLAUDE.md "Kullanici girisi") - lokalde bu degisken tanimli
# DEGILSE bu bir lokal gelistirme ortami sayilir ve kullanici adi/sifre hic
# sorulmadan otomatik giris yapilmis kabul edilir. Boylece lokalde her hisse
# detayini/Sanal Al-Sat'i test etmek icin sir yonetmeye gerek kalmaz.
_LOCAL_AUTO_LOGIN = not ADMIN_PASSWORD_HASH

# FLASK_SECRET_KEY tanimliysa HER ZAMAN o kullanilir. Tanimli DEGILSE, SADECE
# _LOCAL_AUTO_LOGIN (yani ADMIN_PASSWORD_HASH da tanimsiz - lokal gelistirme)
# durumunda sabit bir lokal-dev anahtarina dusulur. ADMIN_PASSWORD_HASH
# tanimliyken (= "production" sayilan her ortam, VPS'teki systemd dahil)
# FLASK_SECRET_KEY eksikse uygulama ANINDA (import sirasinda) patlar - bu
# BILINCLI bir fail-fast: eskiden boyle bir durumda sessizce bu sabit (git'te
# herkese acik) anahtara dusuluyordu, bu da session cookie'lerinin sahte
# imzalanip login'in tamamen atlanabilmesi anlamina gelirdi (2026-11-02'de
# guvenlik taramasinda bulundu, bkz. CLAUDE.md). O sessiz dusus artik YASAK.
_FLASK_SECRET_KEY_ENV = os.environ.get("FLASK_SECRET_KEY")
if _FLASK_SECRET_KEY_ENV:
    app.secret_key = _FLASK_SECRET_KEY_ENV
elif _LOCAL_AUTO_LOGIN:
    app.secret_key = "local-dev-only-insecure-secret-key"
else:
    raise RuntimeError(
        "FLASK_SECRET_KEY ortam degiskeni tanimli degil. ADMIN_PASSWORD_HASH "
        "tanimli oldugu icin bu ortam production sayiliyor ve sabit/bilinen "
        "bir anahtara sessizce dusulmesine izin verilmiyor - once FLASK_SECRET_KEY'i "
        "export edin."
    )

# Sanal Al-Sat gibi para-benzeri verilere iliskin session cookie'si, production'da
# (ADMIN_PASSWORD_HASH tanimliyken) SADECE HTTPS uzerinden gonderilsin diye
# Secure isaretleniyor - lokal gelistirmede (http://127.0.0.1) bu True olsaydi
# tarayici cookie'yi hic göndermez, login hemen kirilirdi, bu yuzden SADECE
# production'da acik.
app.config["SESSION_COOKIE_SECURE"] = not _LOCAL_AUTO_LOGIN


def _is_logged_in():
    return _LOCAL_AUTO_LOGIN or bool(session.get("logged_in"))


def _login_required(view):
    """API endpoint'lerini login'e baglar - HTML tarafinda bir bolum
    gizlenmis olsa da, o bolumun API'sine DOGRUDAN istek atilarak veri
    cekilemesin diye gercek bir koruma (sadece gorsel gizleme degil)."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not _is_logged_in():
            return jsonify({"error": "login required"}), 401
        return view(*args, **kwargs)

    return wrapped

_state_lock = threading.Lock()
_state = {
    "rows": [],
    "missing_count": 0,
    "updated_at": None,
}

# Gercek taramayi (Yahoo'ya giden tum istekler) hem _scan_loop() hem
# _ensure_scanned_once() cagirabilir - bu ikisinin AYNI ANDA, PARALEL
# calismasini onlemek icin TEK bir kilit (2026-11-03'te eklendi, bkz.
# _run_scan()/_scan_once()/_ensure_scanned_once()). Eskiden bu ikisi
# birbirinden habersizdi: piyasa acikken bir restart olduğunda
# _scan_loop()'un ilk dongusu ile (sayfayi hemen acan birinin tetikledigi)
# _ensure_scanned_once() AYNI ANDA calisip Yahoo'ya iki KAT (her biri
# ~700-800 istek) yuk bindirebiliyordu - gercek bir risk, cunku VPS'e her
# deploy/restart tam olarak bu senaryoyu olusturuyor.
_scan_lock = threading.Lock()

# BIST sekmelerinin sagindaki doviz/emtia/BIST100/BTC seridi - hisse
# taramasindan bagimsiz, kendi thread'inde ayni BIST_REFRESH_SECONDS ritmiyle
# calisir (ikisi de ayni yavaslikta ama farkli veri kumeleri cekiyor,
# birbirini bloklamasin diye ayri thread/lock).
_market_lock = threading.Lock()
_market_state = {"indicators": [], "updated_at": None}



def _sanitize(value):
    """NaN, JSON'da gecerli bir deger degildir (JS'te JSON.parse patlar) -
    bu yuzden tum float NaN degerleri None'a (JSON'da null) cevrilir."""
    if isinstance(value, float) and value != value:
        return None
    return value


def _row_to_json(row: dict) -> dict:
    return {k: _sanitize(v) for k, v in row.items()}


# Skor/MACD/Hacim/GunPoz/RSI/BB%/RS - "sinyal" bilgisi sayilan alanlar,
# 2026-11-04'te kullanici istegiyle SADECE login olana gonderiliyor (login
# olmayan kullanici sinyaller hakkinda bilgi alamamali). Gercek sunucu-tarafli
# gizleme - /api/rows'a/api/indicators'a DOGRUDAN istek atan biri de bu
# alanlari goremez, sadece frontend'de sutunlari gizlemek YETERSIZ olurdu
# (_login_required'daki ayni gerekce - bkz. yukarida). _state["rows"]/
# _chart_cache HER ZAMAN TAM veriyi tutar (login olanlar icin) - filtreleme
# SADECE yanit anında, bu iki fonksiyonla, bir KOPYA uzerinde yapilir.
# score_since (2026-11-05'te eklendi, bkz. CLAUDE.md "Skor'un yanina sinyal
# zamani sutunu") da buraya eklendi - Skor'un KENDISI gibi dogrudan sinyal
# bilgisi sayilir (hangi skorun NE ZAMANDAN BERI aktif oldugunu gosterir),
# ayni politika. day_stop/day_target/swing_stop/swing_target (2026-11-06'da
# BIST tablosuna GERI eklendi, bkz. CLAUDE.md) de buraya eklendi - stop/hedef
# seviyeleri Skor'un yonunu (AL/SAT) dolayli olarak aciga cikardigi icin
# (stop fiyatin ALTINDAYSA AL, USTUNDEYSE SAT yonu demektir) bu da dogrudan
# sinyal bilgisi sayilir - bu sutunlar zaten eskiden (2026-11-02'de
# kaldirilmadan once) de login-only idi.
_SIGNAL_ONLY_ROW_FIELDS = (
    "score", "score_since", "signal_macd", "volume_ratio", "day_range_pos",
    "rsi", "bb_percent", "rel_strength",
    "day_stop", "day_target", "swing_stop", "swing_target",
)
_SIGNAL_ONLY_INDICATOR_FIELDS = ("rsi", "macd", "macd_signal", "macd_hist")


def _strip_signal_fields_from_row(row: dict) -> dict:
    if _is_logged_in():
        return row
    return {k: (None if k in _SIGNAL_ONLY_ROW_FIELDS else v) for k, v in row.items()}


def _strip_signal_fields_from_indicators(data: dict) -> dict:
    if _is_logged_in() or not data:
        return data
    return {k: v for k, v in data.items() if k not in _SIGNAL_ONLY_INDICATOR_FIELDS}


# Grafik/indikator/skor-gecmisi endpoint'leri icin onbellek - scanner_bistTop100.py'deki
# _benchmark_cache ile AYNI desen (TTL = BIST_REFRESH_SECONDS-5). Bir hisse zaten
# secilip verisi cekildiyse, ayni hisseye kisa surede tekrar tiklandiginda
# (ya da farkli bir tarayicidan ayni hisseye bakildiginda) Yahoo'ya tekrar
# istek atmak yerine onbellekten donulur - tarama zaten 5 dakikada bir
# yenilendigi icin daha sik bir veriye ihtiyac yok.
_chart_cache = {}  # (endpoint_adi, ticker) -> (fetched_at, json'lanabilir deger)
CHART_CACHE_TTL = scanner.BIST_REFRESH_SECONDS - 5


def _cache_get(cache_key):
    cached = _chart_cache.get(cache_key)
    if cached and time.time() - cached[0] < CHART_CACHE_TTL:
        return cached[1]
    return None


def _cache_set(cache_key, value):
    _chart_cache[cache_key] = (time.time(), value)
    return value


def _skeleton_row(ticker: str, price, daily_pct) -> dict:
    """Birlesik havuzun (BIST100+ilk 300 kazandiran) DISINDA kalan, sadece
    fetch_all_bist_quotes()'un ucuz toplu taramasindan gorulen hisseler icin
    "iskelet" bir satir - fetch_row()'un pahali indikator hesaplamasi
    calismadigi icin skor/indikator alanlari YOK, SADECE fiyat/gunluk%
    doldurulur. `score="BEKLIYOR"` (confluence_label()'in asla uretmedigi,
    frontend'de SCORE_SYMBOLS'ta eslesmesi olmayan bir deger) secildi -
    None/null KULLANILMADI cunku frontend'deki `SCORE_SYMBOLS[r.score] ||
    r.score` deseni null icin literal "null" metni basardi; sayisal NaN
    alanlar ise zaten _row_to_json()'daki _sanitize() ile null'a cevrilip
    mevcut fmtPct/fmtRsi/vb. yardimcilarinca '-' olarak gosteriliyor (ayrica
    bir frontend degisikligi GEREKMEDI). SCORE_ORDER.get(..., 3) ile NOTR
    gibi sira ortasinda bir yere duser. 2026-11-03'te kullanici istegiyle
    eklendi - bkz. CLAUDE.md: "tum hisseler gorunsun, skoru eksik olanlarda
    islem yapilmasin."""
    nan = float("nan")
    return {
        "ticker": ticker,
        "sector": "-",
        "price": price if price is not None else nan,
        "daily": daily_pct if daily_pct is not None else nan,
        "d5": nan, "m1": nan, "y1": nan,
        "signal_1d": "NOTR", "signal_macd": "NOTR",
        "score": "BEKLIYOR", "score_raw": None, "score_since": None,
        "rsi": nan, "bb_percent": nan, "adx": nan, "rel_strength": nan,
        "day_stop": None, "day_target": None,
        "swing_stop": None, "swing_target": None,
        "volume_ratio": nan, "day_range_pos": nan, "vol20": nan,
    }


def _run_scan():
    """_scan_once()/_ensure_scanned_once()'in PAYLASTIGI gercek tarama
    govdesi - _scan_lock ZATEN alinmis olarak cagrilmali (bkz. cagiran
    taraflardaki yorumlar), burada TEKRAR kilit ALINMAZ (threading.Lock
    yeniden-girilebilir/reentrant DEGIL, ayni thread'de ikinci kez
    acquire() etmek kilitlenir/deadlock olur).

    BIST 30/100/300 sekmelerinin ortak veri havuzunu hazirlar - bkz.
    scanner_bistTop100.py modul aciklamasi: BIST100 (BIST30'u da kapsar) +
    gunluk % degisime gore ilk TOP300_SIZE hisse, cakisanlar tekrar
    cekilmez. Her satir bist30/bist100 uyelik bayraklariyla etiketlenir -
    BIST 300 sekmesi ayrica bir bayrak gerektirmez, tum havuz zaten odur.

    Birlesik havuzun DISINDA kalan BIST hisseleri de (2026-11-03'ten
    itibaren) tabloda GORUNUR - scanner.fetch_all_bist_quotes() ile ucuza
    (~3 istek, 628 hisse icin TEK TEK istek ATILMADAN) cekilen sadece
    fiyat/gunluk% bilgisiyle bir "iskelet" satir olarak (bkz. _skeleton_row).
    Bu iskelet satirlar SADECE _state["rows"] / /api/rows'ta GORUNUR -
    log_signal_changes/log_score_history/run_paper_trading_cycle'a
    GONDERILMEZ (skoru olmayan bir satir uzerinde islem/log mantigi
    calistirmanin anlami yok, ayrica score_history'yi gereksiz buyutmemek
    icin bilincli bir ayrim)."""
    bist30_symbols = {t.removesuffix(".IS") for t in scanner.BIST30_TICKERS}
    bist100_symbols = {t.removesuffix(".IS") for t in scanner.BIST100_TICKERS}

    top_gainers = scanner.fetch_top_gainers(scanner.TOP300_SIZE)
    # Acik sanal al-sat pozisyonlarinin ticker'lari HER ZAMAN havuza eklenir -
    # bunlar BIST100'de olmayabilir ve gunluk kazandiran listesinden (dinamik
    # BIST300) her an dusebilir (bir hisse sonsuza kadar "gunun en cok
    # kazandirani" olamaz). Eklenmezse o pozisyon bir sonraki dongude hic
    # fiyat/skor alamaz - run_paper_trading_cycle() o ticker'i rows icinde
    # bulamadigi icin stop/hedef/GUCLU_SAT kontrolu YAPILAMAZ, pozisyon
    # sonsuza kadar acik kalir (gercekten yasandi - bkz. USHOL, 2026-10-23).
    open_paper_tickers = [f"{p['ticker']}.IS" for p in scanner.get_paper_positions()]
    # Manuel takip listesindeki ticker'lar da AYNI sebeple havuza eklenir -
    # kullanici henuz almadigi bir hisseyi izlemeye aldiginda, o hisse
    # BIST100/gunun kazandiranlari disindaysa bile fiyat gorebilsin diye.
    watchlist_tickers = [f"{w['ticker']}.IS" for w in scanner.get_paper_watchlist()]
    all_tickers = list(
        dict.fromkeys(scanner.BIST100_TICKERS + top_gainers + open_paper_tickers + watchlist_tickers)
    )

    rows_by_ticker, _still_missing = scanner.fetch_with_retry(all_tickers) if all_tickers else ({}, [])
    missing_count = max(0, len(all_tickers) - len(rows_by_ticker))

    def _sort_key(row):
        daily = row["daily"]
        daily_sort = -daily if daily == daily else float("inf")
        return (scanner.SCORE_ORDER.get(row["score"], 3), daily_sort)

    top_rows = sorted(rows_by_ticker.values(), key=_sort_key)
    for row in top_rows:
        row["bist30"] = row["ticker"] in bist30_symbols
        row["bist100"] = row["ticker"] in bist100_symbols

    if top_rows:
        scanner.log_signal_changes(top_rows)
        scanner.log_score_history(top_rows)
        try:
            scanner.run_paper_trading_cycle(top_rows)
        except Exception as e:
            # Sanal al-sat mantigindaki bir hata ana tarama/skor loglama
            # akisini ASLA bozmamali - kendi try/except'i icinde izole.
            print("Sanal al-sat (paper trading) hatasi:", e)
        _maybe_send_hourly_paper_report(top_rows)

    # Birlesik havuzun DISINDAKI hisseler - sadece GORUNUR olmalari icin
    # ucuz bir toplu fiyat taramasiyla (~3 istek) iskelet satirlar
    # eklenir. Hata/zaman asimi durumunda (bos liste donerse) tablo yine
    # eskisi gibi SADECE top_rows'u gosterir - bu katman BASARISIZ olsa
    # da ana tarama akisi bozulmaz.
    try:
        known_tickers = {r["ticker"] for r in top_rows}
        skeleton_rows = [
            _skeleton_row(q["ticker"], q["price"], q["daily"])
            for q in scanner.fetch_all_bist_quotes()
            if q["ticker"] not in known_tickers
        ]
        for row in skeleton_rows:
            row["bist30"] = row["ticker"] in bist30_symbols
            row["bist100"] = row["ticker"] in bist100_symbols
    except Exception as e:
        print("Tam piyasa listesi (iskelet satirlar) hatasi:", e)
        skeleton_rows = []

    display_rows = sorted(top_rows + skeleton_rows, key=_sort_key)

    with _state_lock:
        _state["rows"] = [_row_to_json(r) for r in display_rows]
        _state["missing_count"] = missing_count
        _state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _scan_once():
    """_run_scan()'i _scan_lock ile sarar - _scan_loop()'un normal 5
    dakikalik dongusu bunu cagirir. _ensure_scanned_once() ise AYNI kilidi
    kendi tarafinda alip _run_scan()'i DOGRUDAN cagirir (bkz. orada) - bu
    fonksiyonu DEGIL, re-entrant olmayan kilidi iki kez almamak icin."""
    with _scan_lock:
        _run_scan()


# Sanal Al-Sat'in acik pozisyon K/Z durumunu saat basina BIR KEZ Telegram'a
# raporlamak icin - hangi saate ait rapor son gonderildi (ayni saat icinde
# BIST_REFRESH_SECONDS'ta bir tekrar tekrar gonderilmesin diye). Restart'ta
# None'a doner - bu yuzden her restart sonrasi ILK tarama dongusu (BIST
# aciksa) hemen bir rapor gonderir, bu BEKLENEN/zararsiz bir davranis (saat
# sayacini "kacirmamak" icin tekrar tekrar gondermekten cok daha az riskli).
_last_paper_report_hour_key = None


def _maybe_send_hourly_paper_report(rows) -> None:
    """BIST acik saatlerinde (scanner.is_market_open() - _ensure_scanned_once()'in
    piyasa kapaliyken yaptigi bir-kerelik kurtarma taramasinda rapor
    GONDERILMEZ, bu KASITLI) saat basina BIR KEZ scanner.send_paper_hourly_report()
    cagirir. _scan_once()'tan cagrilir, kendi try/except'i icinde - bir hata
    ana tarama/sanal al-sat akisini bozmamali."""
    global _last_paper_report_hour_key
    if not scanner.is_market_open():
        return
    hour_key = datetime.now().strftime("%Y-%m-%d %H")
    if hour_key == _last_paper_report_hour_key:
        return
    _last_paper_report_hour_key = hour_key
    try:
        scanner.send_paper_hourly_report(rows)
    except Exception as e:
        print("Saatlik Sanal Al-Sat raporu hatasi:", e)


# Ardisik kac basarisiz _scan_once() denemesinden sonra Telegram'a bir
# saglik uyarisi gonderilsin - 3 x BIST_REFRESH_SECONDS (varsayilan 5dk) =
# ~15 dakika. Tek seferlik/gecici bir hata (ornegin anlik bir Yahoo
# zaman asimi) BUNU TETIKLEMEZ, sadece UST USTE basarisizlik tetikler -
# amac "servis sessizce veri cekmeyi durdurdu" durumunu yakalamak.
_SCAN_FAILURE_ALERT_THRESHOLD = 3
_scan_failure_count = 0
_scan_health_alert_sent = False  # esik asildiktan sonra TEK bir uyari gonderilsin, her dongude tekrar DEGIL


def _scan_loop():
    # BIST kapaliyken (hafta sonu / app_settings.json'daki BIST_MARKET_OPEN_*-
    # BIST_MARKET_CLOSE_* penceresi disinda) Yahoo'ya bos yere istek atmamak
    # icin _scan_once() hic cagrilmaz - dongu yine BIST_REFRESH_SECONDS'ta bir
    # "acik mi?" diye bakmaya devam eder, piyasa acilir acilmaz (en fazla
    # bir BIST_REFRESH_SECONDS gecikmeyle) tarama kendiliginden devam eder.
    # Market seridi (_market_scan_loop) buna TABI DEGIL - doviz/emtia/
    # kripto BIST disi saatlerde de islem goruyor.
    global _scan_failure_count, _scan_health_alert_sent
    while True:
        try:
            if scanner.is_market_open():
                _scan_once()
            _scan_failure_count = 0
            _scan_health_alert_sent = False
        except Exception as e:
            print("Tarama hatasi:", e)
            _scan_failure_count += 1
            if _scan_failure_count >= _SCAN_FAILURE_ALERT_THRESHOLD and not _scan_health_alert_sent:
                scanner.send_health_alert(
                    f"BIST tarama döngüsü {_scan_failure_count} kez üst üste başarısız oldu.\n"
                    f"Son hata: {e}"
                )
                _scan_health_alert_sent = True
        time.sleep(scanner.BIST_REFRESH_SECONDS)


# _scan_loop() piyasa kapaliyken (hafta sonu vb.) hic _scan_once() cagirmadigi
# icin, servis TAM OLARAK piyasa kapaliyken (re)baslatilirsa _state["rows"]
# ilk tarama yapilana kadar BOS kalirdi - piyasa acilana kadar (saatlerce)
# sayfada hic hisse gorunmezdi. Bunun yerine: ilk istek geldiginde (henuz
# hic tarama yapilmamissa) BIR KERELIK, piyasa saatinden BAGIMSIZ, anlik bir
# tarama tetiklenir - kullanici o an Yahoo'nun elindeki (piyasa kapaliysa
# son kapanis) veriyi gorur. _scan_loop() piyasa acilinca normal akisina
# (BIST_REFRESH_SECONDS'ta bir tazeleme) devam eder - bu fonksiyon SADECE
# "hic taranmamis" durumunu bir kez kurtarir, surekli calisan bir mekanizma
# DEGIL.
#
# 2026-11-03'te _scan_loop() ile PAYLASILAN _scan_lock kullanacak sekilde
# degistirildi (eskiden kendine ozel _ensure_scan_lock vardi - bu, SADECE
# birden fazla _ensure_scanned_once() cagrisini birbirine karsi korurdu,
# _scan_loop()'un KENDI ilk taramasina karsi HICBIR koruma saglamiyordu).
# Artik: _scan_lock halihazirda _scan_loop tarafindan tutuluyorsa, buradaki
# "with _scan_lock" BLOKE olup bekler - lock serbest kalinca (_scan_loop'un
# taramasi BITMIS olur) tekrar updated_at kontrolu yapilip YENIDEN tarama
# YAPILMAZ. Ayni anda PARALEL iki tarama (iki KAT Yahoo yuku) artik
# MUMKUN DEGIL.
def _ensure_scanned_once():
    with _state_lock:
        if _state["updated_at"] is not None:
            return
    with _scan_lock:
        with _state_lock:
            if _state["updated_at"] is not None:
                return  # lock'u beklerken (_scan_loop ya da baska bir istek) zaten tarandi
        _run_scan()


def _market_scan_once():
    indicators = scanner.fetch_market_indicators()
    with _market_lock:
        _market_state["indicators"] = indicators
        _market_state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _market_scan_loop():
    while True:
        try:
            _market_scan_once()
        except Exception as e:
            print("Piyasa gostergesi tarama hatasi:", e)
        time.sleep(scanner.BIST_REFRESH_SECONDS)


@app.route("/")
def index():
    return render_template(
        "index.html",
        refresh_seconds=scanner.BIST_REFRESH_SECONDS,
        title="BIST Momentum Scanner",
        logged_in=_is_logged_in(),
    )


_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 dakika
_login_attempts_lock = threading.Lock()
_login_attempts = {}  # ip -> (pencere_baslangici, basarisiz_deneme_sayisi)


def _login_rate_limited(ip: str) -> bool:
    """Tek admin hesabina karsi sinirsiz sifre denemesini (online brute-force)
    engellemek icin basit, bagimsizlik gerektirmeyen bir pencereli sayac -
    2026-11-02'deki guvenlik taramasinda eklendi. gunicorn tek worker (-w 1)
    ile calistigi icin bu bellek-ici dict process'ler arasinda bolunmuyor."""
    now = time.time()
    with _login_attempts_lock:
        entry = _login_attempts.get(ip)
        if entry is None:
            return False
        window_start, count = entry
        if now - window_start > _LOGIN_WINDOW_SECONDS:
            del _login_attempts[ip]
            return False
        return count >= _LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip: str) -> None:
    now = time.time()
    with _login_attempts_lock:
        entry = _login_attempts.get(ip)
        if entry is None or now - entry[0] > _LOGIN_WINDOW_SECONDS:
            _login_attempts[ip] = (now, 1)
        else:
            _login_attempts[ip] = (entry[0], entry[1] + 1)


def _clear_login_failures(ip: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)


@app.route("/login", methods=["GET", "POST"])
def login():
    # Lokalde ADMIN_PASSWORD_HASH tanimli degilse (_LOCAL_AUTO_LOGIN) bu
    # sayfaya hic gelinmeden _is_logged_in() zaten True doner - ama direkt
    # /login'e gidilirse (ya da POST atilirsa, bos alanlarla dahi) yine
    # kullanici adi/sifre sormadan girisi tamamlar.
    if _LOCAL_AUTO_LOGIN:
        session.permanent = True
        session["logged_in"] = True
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _login_rate_limited(ip):
            error = "Çok fazla başarısız deneme yapıldı - birkaç dakika sonra tekrar deneyin."
            return render_template("login.html", error=error), 429
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (
            ADMIN_PASSWORD_HASH
            and username == ADMIN_USERNAME
            and check_password_hash(ADMIN_PASSWORD_HASH, password)
        ):
            _clear_login_failures(ip)
            session.permanent = True
            session["logged_in"] = True
            return redirect(url_for("index"))
        _record_login_failure(ip)
        error = "Kullanıcı adı veya şifre hatalı."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/api/rows")
def api_rows():
    _ensure_scanned_once()
    with _state_lock:
        state_copy = dict(_state)
        rows = list(state_copy["rows"])
    if not _is_logged_in():
        rows = [_strip_signal_fields_from_row(r) for r in rows]
    state_copy["rows"] = rows
    return jsonify(state_copy)


@app.route("/api/market-indicators")
def api_market_indicators():
    with _market_lock:
        return jsonify(dict(_market_state))


# /api/history ve /api/indicators AYNI ticker icin ayri ayri Yahoo istegi
# atmasin diye paylasilan HAM (DataFrame) onbellek - ikisi de 1 yillik veriyi
# (asagidaki CHART_RANGE_TRADING_DAYS'in en genis araligi olan "1y" DAHIL,
# indikatorlerin isinma penceresi de bu sayede her aralik icin yeterli) TEK
# bir Yahoo cagrisindan paylasir. Frontend ikisini neredeyse AYNI ANDA
# (Promise.all) istedigi icin per-ticker kilit de var: ilk istek Yahoo'ya
# gider, es zamanli ikinci istek kilidi bekleyip onbellekten okur - ikisi de
# ayri ayri Yahoo'ya gitmesin diye.
_raw_history_cache = {}  # ticker -> (fetched_at, DataFrame)
_raw_history_locks = {}
_raw_history_locks_guard = threading.Lock()
RAW_HISTORY_CACHE_TTL = scanner.BIST_REFRESH_SECONDS - 5


def _get_raw_history(ticker: str):
    cached = _raw_history_cache.get(ticker)
    if cached and time.time() - cached[0] < RAW_HISTORY_CACHE_TTL:
        return cached[1]

    with _raw_history_locks_guard:
        lock = _raw_history_locks.setdefault(ticker, threading.Lock())

    with lock:
        cached = _raw_history_cache.get(ticker)
        if cached and time.time() - cached[0] < RAW_HISTORY_CACHE_TTL:
            return cached[1]
        yahoo_ticker = f"{ticker}.IS"
        hist = yf.Ticker(yahoo_ticker).history(period="1y", interval="1d", auto_adjust=False)
        _raw_history_cache[ticker] = (time.time(), hist)
        return hist


# Grafik zaman araligi secici (1A/3A/6A/1Y, 2026-11-04'te eklendi) - mum
# COZUNURLUGU hep gunluk kalir, SADECE gorunen pencere genisler/daralir
# (TradingView/Yahoo'nun standart "1A 3A 6A 1Y" butonlari gibi - gercek
# aylik/ceyreklik RESAMPLE edilmis mum DEGIL, kullaniciyla konusulup
# netlestirildi). Islem gunu sayilari scanner_bistTop100.py'deki
# MONTHLY_LOOKBACK_DAYS/YEARLY_LOOKBACK_DAYS'ten ("~1 ay = 21 islem gunu"
# kurali) turetildi - ayri bir sabit uydurulmadi, tutarlilik icin oradaki
# tanimlar carpilarak kullanildi. _get_raw_history() artik HER ZAMAN 1
# yillik veri cektigi icin (yukarida) bu dort aralik da EK bir Yahoo istegi
# GEREKTIRMEZ - sadece ayni DataFrame'den farkli uzunlukta bir dilim alinir.
CHART_RANGE_TRADING_DAYS = {
    "1m": scanner.MONTHLY_LOOKBACK_DAYS,
    "3m": scanner.MONTHLY_LOOKBACK_DAYS * 3,
    "6m": scanner.MONTHLY_LOOKBACK_DAYS * 6,
    "1y": scanner.YEARLY_LOOKBACK_DAYS,
}
CHART_RANGE_DEFAULT = "3m"


def _resolve_chart_range() -> str:
    """?range= query parametresini okur - gecersiz/eksik bir deger (veya hic
    gonderilmemesi, eski/once yuklenmis bir sayfa) sessizce varsayilana (3A)
    duser, hataya yol acmaz. Sadece bir dict key kontrolu - enjeksiyon riski
    yok."""
    range_key = request.args.get("range", CHART_RANGE_DEFAULT)
    return range_key if range_key in CHART_RANGE_TRADING_DAYS else CHART_RANGE_DEFAULT


@app.route("/api/history/<ticker>")
def api_history(ticker: str):
    """Verilen (soneksiz) ticker icin gunluk mum verisini dondurur - ?range=
    ile 1A/3A/6A/1Y araliklari secilebilir (varsayilan 3A, bkz.
    CHART_RANGE_TRADING_DAYS). orn. /api/history/AKBNK?range=1y -> AKBNK.IS
    icin 1 yillik OHLC. Ham veri /api/indicators ile paylasilir (bkz.
    _get_raw_history) - ayri bir Yahoo istegi yapmaz."""
    range_key = _resolve_chart_range()
    cache_key = ("history", ticker, range_key)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        hist = _get_raw_history(ticker)
    except Exception as e:
        # Ham exception metni (Yahoo/yfinance ic detaylari) istemciye
        # sizdirilmiyor - sadece sunucu logunda tutulur (2026-11-02, guvenlik
        # taramasi bulgusu).
        print(f"api_history({ticker}) hatasi:", e)
        return jsonify({"error": "veri alınamadı"}), 502
    if hist.empty:
        return jsonify(_cache_set(cache_key, []))
    recent = hist.tail(CHART_RANGE_TRADING_DAYS[range_key])
    candles = [
        {
            "time": idx.strftime("%Y-%m-%d"),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        }
        for idx, row in recent.iterrows()
    ]
    return jsonify(_cache_set(cache_key, candles))


# Skor Gecmisi de bir "sinyal" bilgisi kaynagi sayilir (2026-11-04'te
# kullanici istegiyle netlestirildi, bkz. CLAUDE.md "Sinyal bilgisi artik
# login olmayan..."): LOGIN OLANA gun siniri YOK (TUM gecmis, DB'de ne kadar
# varsa - pratikte backfill_daily_bist.py'nin BACKFILL_DAYS'ina, varsayilan
# 365, kadar). LOGIN OLMAYANA ise SADECE son SCORE_HISTORY_WINDOW_DAYS_GUEST
# (5) gun gosterilir.
SCORE_HISTORY_WINDOW_DAYS_GUEST = 5
_SCORE_HISTORY_NO_LIMIT_TS = "1970-01-01 00:00:00"  # login olana "tum gecmis" icin alt sinir


@app.route("/api/score-history/<ticker>")
def api_score_history(ticker: str):
    """Verilen ticker icin skor gecmisini dondurur: once (varsa) GUNLUK
    backfill kayitlari (canli logun BASLAMADIGI eski tarihler icin,
    source="backfill" ile isaretli), sonra HER dongudeki (5 dk) ham canli
    kayit (source="live"). Gunluk sekmesi ikisini birlikte kullanir;
    Saatlik/5-Dakikalik sekmeleri sadece canli (gercek gun-ici
    granulariteli) kayitlari kullanir - bkz. templates/_view_graphic.html
    renderScoreHistory().

    Login durumuna gore PENCERE FARKLI (yukaridaki SCORE_HISTORY_WINDOW_DAYS_GUEST
    aciklamasina bkz.) - bu yuzden cache_key login durumunu da icerir, aksi
    halde login olan/olmayan AYNI onbellek girdisini paylasip biri digerinin
    (yanlis pencereli) sonucunu CHART_CACHE_TTL suresince gorebilirdi.

    Veri scanner.get_score_history() uzerinden (ticker, timestamp)
    indeksli bir SQLite sorgusuyla okunur - eskiden bellek ici bir
    index'ten okunurdu (canli CSV milyonlarca satira ciktiginda dosyayi
    bastan taramak grafigi saniyelerce yavaslatiyordu), artik ne dosya
    taramasi ne de tam tabloyu belleğe yukleme gerekiyor."""
    logged_in = _is_logged_in()
    cache_key = ("score-history", ticker, logged_in)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    if logged_in:
        cutoff_ts = _SCORE_HISTORY_NO_LIMIT_TS
    else:
        cutoff_ts = (datetime.now() - timedelta(days=SCORE_HISTORY_WINDOW_DAYS_GUEST)).strftime("%Y-%m-%d 00:00:00")
    rows = scanner.get_score_history(ticker, cutoff_ts)

    live_rows = [r for r in rows if r["source"] == "live"]
    backfill_rows = [r for r in rows if r["source"] == "backfill"]

    # Canli log hangi tarihten itibaren basliyorsa, backfill sadece ONDAN
    # ONCEKI gunleri doldurur - ayni gun icin iki kaynak cakismasin diye.
    live_start_date = min((r["timestamp"][:10] for r in live_rows), default=None)
    if live_start_date:
        backfill_rows = [r for r in backfill_rows if r["timestamp"][:10] < live_start_date]

    events = [
        {"time": r["timestamp"], "score_raw": r["score_raw"], "score": r["score"], "price": r["price"], "source": r["source"]}
        for r in (backfill_rows + live_rows)
    ]
    return jsonify(_cache_set(cache_key, events))


@app.route("/api/indicators/<ticker>")
def api_indicators(ticker: str):
    """Verilen (soneksiz) ticker icin EMA9/21, MACD, RSI ve Bollinger
    Bantlarinin zaman serisini dondurur - grafikte istege bagli acilir/kapanir
    indikator katmanlari icin. ?range= ile api_history() ile AYNI 1A/3A/6A/1Y
    araligi secilebilir (varsayilan 3A) - indikatorler HER ZAMAN 1 yillik ham
    veriden (bkz. _get_raw_history) hesaplanir, SADECE yanit secilen aralaga
    kirpilir; boylece en genis aralikta (1Y) bile indikatorlerin basinda
    isinma payi eksikligi olmaz. Ham veri /api/history ile paylasilir - ayri
    bir Yahoo istegi yapmaz."""
    range_key = _resolve_chart_range()
    cache_key = ("indicators", ticker, range_key)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(_strip_signal_fields_from_indicators(cached))

    try:
        hist = _get_raw_history(ticker)
    except Exception as e:
        print(f"api_indicators({ticker}) hatasi:", e)
        return jsonify({"error": "veri alınamadı"}), 502
    if hist.empty:
        return jsonify(_cache_set(cache_key, {}))

    closes = hist["Close"]
    ema9, ema21 = scanner.ema_series_pair(closes)
    rsi_vals = scanner.rsi_series(closes)
    macd_line, macd_sig, macd_hist = scanner.macd_series(closes)
    bb_upper, bb_mid, bb_lower = scanner.bollinger_bands_series(closes)
    dates = [idx.strftime("%Y-%m-%d") for idx in hist.index]
    range_days = CHART_RANGE_TRADING_DAYS[range_key]

    def _series(values):
        trimmed = values.tail(range_days)
        trimmed_dates = dates[-len(trimmed):]
        return [
            {"time": t, "value": None if v != v else float(v)}
            for t, v in zip(trimmed_dates, trimmed)
        ]

    full_data = _cache_set(
        cache_key,
        {
            "ema9": _series(ema9),
            "ema21": _series(ema21),
            "rsi": _series(rsi_vals),
            "macd": _series(macd_line),
            "macd_signal": _series(macd_sig),
            "macd_hist": _series(macd_hist),
            "bb_upper": _series(bb_upper),
            "bb_mid": _series(bb_mid),
            "bb_lower": _series(bb_lower),
        },
    )
    return jsonify(_strip_signal_fields_from_indicators(full_data))


@app.route("/api/paper/summary")
@_login_required
def api_paper_summary():
    """Sanal al-sat hesabinin ozeti: nakit, equity, toplam/gerceklesmis/
    gerceklesmemis K-Z, kazanma orani. bkz. scanner.run_paper_trading_cycle()."""
    return jsonify(scanner.get_paper_summary())


@app.route("/api/paper/positions")
@_login_required
def api_paper_positions():
    """Su an acik olan sanal pozisyonlar - guncel fiyat/K-Z, son taramanin
    _state['rows']'u ile (ekstra Yahoo istegi olmadan) zenginlestirilir."""
    positions = scanner.get_paper_positions()
    with _state_lock:
        price_by_ticker = {r["ticker"]: r["price"] for r in _state["rows"]}

    enriched = []
    for pos in positions:
        current_price = price_by_ticker.get(pos["ticker"])
        pnl = None
        pnl_pct = None
        if current_price is not None and current_price == current_price:
            pnl = (current_price - pos["entry_price"]) * pos["shares"]
            if pos["allocation"]:
                pnl_pct = pnl / pos["allocation"] * 100.0
        enriched.append(
            {**pos, "current_price": current_price, "unrealized_pnl": pnl, "unrealized_pnl_pct": pnl_pct}
        )
    return jsonify(enriched)


@app.route("/api/paper/trades")
@_login_required
def api_paper_trades():
    """Kapanmis sanal islemler (en yeni once)."""
    return jsonify(scanner.get_paper_trades())


@app.route("/api/paper/trades/<int:trade_id>/note", methods=["POST"])
@_login_required
def api_paper_trade_note(trade_id: int):
    """Kapanan bir islemin aciklama notunu gunceller (2026-11-05'te eklendi)
    - body {note}, en fazla scanner.PAPER_TRADE_NOTE_MAX_LEN (250) karakter.
    Bos string/eksik alan notu SILER (kullanici onceden yazdigi notu
    kaldirmak isteyebilir)."""
    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()
    if len(note) > scanner.PAPER_TRADE_NOTE_MAX_LEN:
        return jsonify({"error": f"açıklama en fazla {scanner.PAPER_TRADE_NOTE_MAX_LEN} karakter olabilir"}), 400
    updated = scanner.set_paper_trade_note(trade_id, note or None)
    if not updated:
        return jsonify({"error": "işlem bulunamadı (ya da henüz açık)"}), 404
    return jsonify({"status": "ok", "note": note})


# Equity grafigi zaman araligi secici (1A/3A/6A/1Y, 2026-11-04'te eklendi) -
# mum grafiginin CHART_RANGE_TRADING_DAYS'inden FARKLI olarak burada ISLEM
# GUNU degil TAKVIM GUNU sayiliyor - equity verisi gun-ici (5 dakikalik)
# granularitede surekli biriktigi icin "islem gunu" kavrami burada anlamli
# degil (candlestick'te oldugu gibi "tail(N)" ile satir kesmek de islemiyor,
# cunku piyasa kapaliyken/hafta sonu HIC satir eklenmiyor - bu yuzden tarih
# bazli bir cutoff kullanilir, bkz. SCORE_HISTORY_WINDOW_DAYS_GUEST ile AYNI
# desen). Varsayilan "1A" (3A DEGIL) - kullanici istegiyle, ana grafikten
# BILEREK farkli secildi.
PAPER_EQUITY_RANGE_DAYS = {"1m": 30, "3m": 90, "6m": 180, "1y": 365}
PAPER_EQUITY_RANGE_DEFAULT = "1m"


def _resolve_paper_equity_range() -> str:
    """?range= query parametresini okur - gecersiz/eksikse sessizce
    varsayilana (1A) duser, hataya yol acmaz."""
    range_key = request.args.get("range", PAPER_EQUITY_RANGE_DEFAULT)
    return range_key if range_key in PAPER_EQUITY_RANGE_DAYS else PAPER_EQUITY_RANGE_DEFAULT


@app.route("/api/paper/equity")
@_login_required
def api_paper_equity():
    """Sanal hesabin equity (nakit + acik pozisyon degeri) gecmisi - Sanal
    Al-Sat panelindeki equity egrisi grafigi icin (2026-11-04'te GERI
    EKLENDI, bkz. CLAUDE.md "Olu kod taramasi #2" - bu route + scanner.
    get_paper_equity_history() daha once hic kullanilmadigi icin
    kaldirilmisti, simdi grafik ekleniyor). ?range= ile 1A/3A/6A/1Y araligi
    secilebilir (varsayilan 1A, bkz. PAPER_EQUITY_RANGE_DAYS). Gunluk/saatlik
    gruplama (Skor Gecmisi'yle AYNI desen) YAPILMAZ - veri zaten gun-ici
    granularitede, ham olarak donulur."""
    range_key = _resolve_paper_equity_range()
    days = PAPER_EQUITY_RANGE_DAYS[range_key]
    cutoff_ts = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(scanner.get_paper_equity_history(cutoff_ts))


@app.route("/api/paper/cash", methods=["POST"])
@_login_required
def api_paper_set_cash():
    """Sanal nakti elle istenen tutara ayarlar - kullanici Sanal Al-Sat
    panelinden duzenleyebiliyor."""
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "geçerli bir tutar (amount) gerekli"}), 400
    if amount < 0:
        return jsonify({"error": "tutar negatif olamaz"}), 400
    scanner.set_paper_cash(amount)
    return jsonify({"status": "ok", "cash": amount})


@app.route("/api/paper/max-positions", methods=["POST"])
@_login_required
def api_paper_set_max_positions():
    """Maksimum acik pozisyon slotunu elle degistirir (2026-11-06'da
    eklendi, bkz. CLAUDE.md) - eskiden sabit 5'ti. scanner.set_paper_max_positions()
    degeri PAPER_MAX_POSITIONS_MIN..MAX (1..50) araligina sinirlar."""
    data = request.get_json(silent=True) or {}
    try:
        n = int(data.get("max_positions"))
    except (TypeError, ValueError):
        return jsonify({"error": "geçerli bir pozisyon sayısı (max_positions) gerekli"}), 400
    if not (scanner.PAPER_MAX_POSITIONS_MIN <= n <= scanner.PAPER_MAX_POSITIONS_MAX):
        return jsonify({
            "error": f"pozisyon sayısı {scanner.PAPER_MAX_POSITIONS_MIN} ile {scanner.PAPER_MAX_POSITIONS_MAX} arasında olmalı"
        }), 400
    scanner.set_paper_max_positions(n)
    return jsonify({"status": "ok", "max_positions": n})


@app.route("/api/settings/criteria", methods=["GET", "POST"])
@_login_required
def api_settings_criteria():
    """Ayarlar ekraninin (frontend/templates/_view_settings.html) backend
    ucu - otomatik alim/satimi VE (BAGIMSIZ, bkz. CLAUDE.md) Telegram SKOR
    sinyali bildirimini tetikleyen kriter kumelerini okur/yazar. Is mantigi
    settings.py'de (bkz. o dosyanin modul docstring'i - scanner_bistTop100.py'den
    BILEREK AYRI).

    GET: settings.get_paper_criteria()'yi dondurur.
    POST: Body {"buy": [...], "sell": [...], "notify": [...]} - her liste
    kendi gecerli kumesine (settings.PAPER_*_CRITERIA_OPTIONS) gore
    dogrulanir, gecersiz bir etiket iceren liste 400 doner (settings.py'nin
    kendi SESSIZCE elemesinden farkli olarak burada SIKI dogrulama -
    kullanici yazim hatasini fark etsin). "notify" ALANI OPSIYONEL - hic
    gonderilmezse (None) notify_criteria'ya dokunulmaz, SADECE buy/sell
    guncellenir. Bos liste (hicbir etiket secilmemis) HER UCU icin GECERLI
    bir secim - o yonde otomatik islem/bildirim tamamen durur, bu
    kullanicinin kendi karari."""
    if request.method == "GET":
        return jsonify(settings.get_paper_criteria())

    data = request.get_json(silent=True) or {}
    buy = data.get("buy")
    sell = data.get("sell")
    notify = data.get("notify")
    if not isinstance(buy, list) or not isinstance(sell, list):
        return jsonify({"error": "buy ve sell alanları liste olmalı"}), 400
    if notify is not None and not isinstance(notify, list):
        return jsonify({"error": "notify alanı liste olmalı"}), 400
    if any(b not in settings.PAPER_BUY_CRITERIA_OPTIONS for b in buy):
        return jsonify({
            "error": f"buy için geçerli değerler: {', '.join(settings.PAPER_BUY_CRITERIA_OPTIONS)}"
        }), 400
    if any(s not in settings.PAPER_SELL_CRITERIA_OPTIONS for s in sell):
        return jsonify({
            "error": f"sell için geçerli değerler: {', '.join(settings.PAPER_SELL_CRITERIA_OPTIONS)}"
        }), 400
    if notify is not None and any(n not in settings.PAPER_NOTIFY_CRITERIA_OPTIONS for n in notify):
        return jsonify({
            "error": f"notify için geçerli değerler: {', '.join(settings.PAPER_NOTIFY_CRITERIA_OPTIONS)}"
        }), 400
    settings.set_paper_buy_criteria(buy)
    settings.set_paper_sell_criteria(sell)
    if notify is not None:
        settings.set_paper_notify_criteria(notify)
    return jsonify({"status": "ok", "buy": buy, "sell": sell, "notify": notify})


@app.route("/api/paper/reset", methods=["POST"])
@_login_required
def api_paper_reset():
    """Sanal al-sat'i tamamen sifirlar - body {amount} ile baslangic nakti
    kullanici tarafindan belirlenir (sabit 1.000.000 degil). Onay (emin
    misin) frontend'de zaten alinmis olmali, bu endpoint dogrudan uygular."""
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "geçerli bir başlangıç tutarı (amount) gerekli"}), 400
    if amount <= 0:
        return jsonify({"error": "tutar sıfırdan büyük olmalı"}), 400
    scanner.reset_paper_trading(amount)
    return jsonify({"status": "ok", "cash": amount})


@app.route("/api/paper/watchlist", methods=["GET", "POST"])
@_login_required
def api_paper_watchlist():
    """GET: manuel takip listesini guncel fiyatla doner. POST: body
    {ticker} ile listeye ekler."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        ticker = (data.get("ticker") or "").strip().upper()
        if not ticker:
            return jsonify({"error": "ticker gerekli"}), 400
        scanner.add_to_paper_watchlist(ticker)
        return jsonify({"status": "ok"})

    watchlist = scanner.get_paper_watchlist()
    with _state_lock:
        price_by_ticker = {r["ticker"]: r["price"] for r in _state["rows"]}
    # Kaynak farki YOK (auto ya da manual) - bir hisse ZATEN acik bir
    # pozisyondaysa (hangi yoldan acilmis olursa olsun) takip listesinde
    # "Al" butonu yerine "Pozisyon acik" gosterilmeli (2026-11-02'de
    # duzeltildi - eskiden SADECE manual pozisyonlara bakiliyordu, auto
    # tarafindan zaten alinmis bir hisse icin hala "Al" butonu gorunup
    # tekrar manuel alim yapilabiliyordu, bkz. CLAUDE.md).
    open_tickers = {p["ticker"] for p in scanner.get_paper_positions()}
    enriched = [
        {
            **w,
            "current_price": price_by_ticker.get(w["ticker"]),
            "has_open_position": w["ticker"] in open_tickers,
        }
        for w in watchlist
    ]
    return jsonify(enriched)


@app.route("/api/paper/watchlist/<ticker>", methods=["DELETE"])
@_login_required
def api_paper_watchlist_remove(ticker: str):
    scanner.remove_from_paper_watchlist(ticker.strip().upper())
    return jsonify({"status": "ok"})


@app.route("/api/paper/manual-buy", methods=["POST"])
@_login_required
def api_paper_manual_buy():
    """Kullanicinin elle, istedigi an, istedigi tutarla alim yapmasi -
    5-slot limitine artik auto/manual farki olmadan DAHIL (2026-11-03'te
    degistirildi, bkz. scanner.manual_buy)."""
    data = request.get_json(silent=True) or {}
    ticker = (data.get("ticker") or "").strip().upper()
    try:
        allocation = float(data.get("allocation"))
    except (TypeError, ValueError):
        return jsonify({"error": "geçerli bir tutar (allocation) gerekli"}), 400
    if not ticker:
        return jsonify({"error": "ticker gerekli"}), 400

    with _state_lock:
        price_by_ticker = {r["ticker"]: r["price"] for r in _state["rows"]}
    price = price_by_ticker.get(ticker)
    if price is None or price != price:
        return jsonify({"error": f"{ticker} için güncel fiyat yok"}), 400

    result = scanner.manual_buy(ticker, allocation, price)
    if result is None:
        return jsonify({"error": "alım başarısız - tutar geçersiz, nakit yetersiz, bu hissede zaten açık pozisyon var ya da 5 pozisyon limiti dolu"}), 400
    return jsonify(result)


@app.route("/api/paper/manual-sell", methods=["POST"])
@_login_required
def api_paper_manual_sell():
    """Kullanicinin elle, istedigi an satmasi - acik olan HERHANGI bir
    pozisyon icin calisir, kaynagi (auto/manual) farketmez (2026-11-02'de
    genisletildi, bkz. scanner.manual_sell())."""
    data = request.get_json(silent=True) or {}
    try:
        trade_id = int(data.get("trade_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "geçerli bir trade_id gerekli"}), 400

    positions = {p["id"]: p for p in scanner.get_paper_positions()}
    pos = positions.get(trade_id)
    if pos is None:
        return jsonify({"error": "pozisyon bulunamadı"}), 404

    with _state_lock:
        price_by_ticker = {r["ticker"]: r["price"] for r in _state["rows"]}
    price = price_by_ticker.get(pos["ticker"])
    if price is None or price != price:
        return jsonify({"error": f"{pos['ticker']} için güncel fiyat yok"}), 400

    result = scanner.manual_sell(trade_id, price)
    if result is None:
        return jsonify({"error": "satış başarısız"}), 400
    return jsonify(result)


@app.route("/api/paper/report-test", methods=["GET", "POST"])
@_login_required
def api_paper_report_test():
    """Saatlik Sanal Al-Sat Telegram raporunu (_maybe_send_hourly_paper_report()'un
    saat basina bir kez gonderdigi AYNI rapor) saat sayacini BEKLEMEDEN
    aninda gonderir - format/metin degisikliklerini test etmek icin. GET'e
    de acik (tarayicidan login olduktan sonra dogrudan URL olarak
    ziyaret edilebilsin diye) - yan etkisi sadece bir Telegram mesaji
    gondermek, kalici bir state degisikligi YOK, bu yuzden GET olmasi
    sakincali degil. KALICI bir ozellik (2026-10-28'de eklenip sonra
    "unutulmus debug endpoint'i" olarak kaldirilan /api/debug/test-telegram
    GIBI DEGIL - login korumali, gecici DEGIL, raporun KENDISI). Telegram
    token/chat_id tanimli olmasa da calisir - `text` alaninda rapor metni
    donar, boylece Telegram kurulu olmadan da formati gorebilirsin;
    `telegram_configured` gercekten gonderilip gonderilmedigini soyler."""
    with _state_lock:
        rows = list(_state["rows"])
    text = scanner.send_paper_hourly_report(rows)
    return jsonify({
        "telegram_configured": bool(scanner.TELEGRAM_BOT_TOKEN and scanner.TELEGRAM_CHAT_ID),
        "text": text,
    })


# scanner_bistTop100 import edilirken DB/tablolar zaten hazirlandi (bkz.
# scanner._init_db()). Arka plan tarama thread'leri modul SEVIYESINDE
# baslatiliyor (fonksiyon/if __name__ icinde DEGIL) - boylece hem
# "python3 web_bistTop100.py" ile dogrudan calistirmada (asagidaki
# __main__ bloğu) HEM DE "gunicorn web_bistTop100:app" ile (2026-11-02'de
# Flask'in gelistirme sunucusu yerine gecildi, bkz. CLAUDE.md "Deploy
# sureci" - Werkzeug dev sunucusu production icin tasarlanmamis) import
# edildiginde thread'ler calismaya baslar - gunicorn __main__ bloğunu HIC
# calistirmaz, modul import'u tek tetikleyicidir. gunicorn TEK worker
# (-w 1) ile calistirilmali: birden fazla worker, her biri kendi
# _scan_loop/_market_scan_loop kopyasini baslatip Yahoo'ya KAT KAT istek
# atar VE her worker'in kendi bellek-ici _state/_chart_cache/_paper_last_score
# kopyasi birbirinden habersiz kalir (bkz. scanner_bistTop100.py'deki
# modul-seviyesi state'ler) - bu TEK process/TEK worker varsayimiyla
# tasarlandi, degistirilmemeli.
threading.Thread(target=_scan_loop, daemon=True).start()
threading.Thread(target=_market_scan_loop, daemon=True).start()

if __name__ == "__main__":
    # Sadece LOKAL gelistirme icin (python3 web_bistTop100.py) - production'da
    # (VPS) artik gunicorn kullaniliyor, bu satir hic calismaz (bkz. yukarida).
    # 127.0.0.1: bu gelistirme sunucusu dogrudan disariya acik degil.
    # threaded=True: bir hisseye tiklandiginda ayni anda atilan 3 istek
    # (history/score-history/indicators) birbirini bloklamasin diye.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
