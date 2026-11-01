#!/usr/bin/env python3
"""scanner_bistTop100.py'nin canli verisini yerel bir web arayuzunde gosterir.

Mevcut scanner_bistTop100 modulunun tum tarama/indikator mantigini (fetch_row,
fetch_with_retry, confluence_label vb.) OLDUGU GIBI yeniden kullanir - burada
sadece bu veriyi bir arka plan thread'inde BIST_REFRESH_SECONDS'ta bir tazeleyip
basit bir JSON API + HTML sayfasi uzerinden sunuyoruz. Tarama mantigi
degismedi, sadece terminal yerine tarayicida gosteriliyor.

Calistirmak icin:
    python3 web_bistTop100.py
Sonra tarayicida: http://127.0.0.1:5000

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
import yfinance as yf
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
app = Flask(
    __name__,
    template_folder=os.path.join(_FRONTEND_DIR, "templates"),
    static_folder=os.path.join(_FRONTEND_DIR, "static"),
)
# Sadece tooltip'ler + Sanal Al-Sat bolumu icin tek-kullanicili basit giris -
# coklu kullanici/kayit sistemi YOK. Sir (hash) asla koda/repoya yazilmaz,
# Telegram bot token/Firebase kimlik dosyasiyla AYNI desen: ortam degiskeni.
# FLASK_SECRET_KEY tanimli DEGILSE (lokal gelistirme, VPS'teki systemd servisi
# bunu HER ZAMAN env ile verir) sabit bir lokal-dev anahtari kullanilir -
# aksi halde secret_key=None oldugunda Flask'in session'i (session.clear() /
# session["logged_in"]=True, _LOCAL_AUTO_LOGIN'in login()/logout()'ta
# kullandigi) RuntimeError ile patlar. Bu anahtar ASLA production'da
# kullanilmaz (VPS'te FLASK_SECRET_KEY zaten zorunlu/farkli bir deger).
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "local-dev-only-insecure-secret-key"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")
# VPS'teki systemd servisi ADMIN_PASSWORD_HASH'i HER ZAMAN env degiskeniyle
# verir (bkz. CLAUDE.md "Kullanici girisi") - lokalde bu degisken tanimli
# DEGILSE bu bir lokal gelistirme ortami sayilir ve kullanici adi/sifre hic
# sorulmadan otomatik giris yapilmis kabul edilir. Boylece lokalde her hisse
# detayini/Sanal Al-Sat'i test etmek icin sir yonetmeye gerek kalmaz.
_LOCAL_AUTO_LOGIN = not ADMIN_PASSWORD_HASH


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


def _scan_once():
    """BIST 30/100/300 sekmelerinin ortak veri havuzunu hazirlar - bkz.
    scanner_bistTop100.py modul aciklamasi: BIST100 (BIST30'u da kapsar) +
    gunluk % degisime gore ilk TOP300_SIZE hisse, cakisanlar tekrar
    cekilmez. Her satir bist30/bist100 uyelik bayraklariyla etiketlenir -
    BIST 300 sekmesi ayrica bir bayrak gerektirmez, tum havuz zaten odur."""
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

    with _state_lock:
        _state["rows"] = [_row_to_json(r) for r in top_rows]
        _state["missing_count"] = missing_count
        _state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _scan_loop():
    # BIST kapaliyken (hafta sonu / app_settings.json'daki BIST_MARKET_OPEN_*-
    # BIST_MARKET_CLOSE_* penceresi disinda) Yahoo'ya bos yere istek atmamak
    # icin _scan_once() hic cagrilmaz - dongu yine BIST_REFRESH_SECONDS'ta bir
    # "acik mi?" diye bakmaya devam eder, piyasa acilir acilmaz (en fazla
    # bir BIST_REFRESH_SECONDS gecikmeyle) tarama kendiliginden devam eder.
    # Market seridi (_market_scan_loop) buna TABI DEGIL - doviz/emtia/
    # kripto BIST disi saatlerde de islem goruyor.
    while True:
        try:
            if scanner.is_market_open():
                _scan_once()
        except Exception as e:
            print("Tarama hatasi:", e)
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
_ensure_scan_lock = threading.Lock()


def _ensure_scanned_once():
    with _state_lock:
        if _state["updated_at"] is not None:
            return
    with _ensure_scan_lock:
        with _state_lock:
            if _state["updated_at"] is not None:
                return  # bu arada baska bir istek zaten taramayi tamamladi
        _scan_once()


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
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (
            ADMIN_PASSWORD_HASH
            and username == ADMIN_USERNAME
            and check_password_hash(ADMIN_PASSWORD_HASH, password)
        ):
            session.permanent = True
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Kullanici adi veya sifre hatali."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/api/rows")
def api_rows():
    _ensure_scanned_once()
    with _state_lock:
        return jsonify(dict(_state))


@app.route("/api/market-indicators")
def api_market_indicators():
    with _market_lock:
        return jsonify(dict(_market_state))


# /api/history ve /api/indicators AYNI ticker icin ayri ayri Yahoo istegi
# atmasin diye paylasilan HAM (DataFrame) onbellek - ikisi de 6 aylik veriyi
# (indicators'in isinma penceresi, history'nin ihtiyacindan daha genis) TEK
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
        hist = yf.Ticker(yahoo_ticker).history(period="6mo", interval="1d", auto_adjust=False)
        _raw_history_cache[ticker] = (time.time(), hist)
        return hist


@app.route("/api/history/<ticker>")
def api_history(ticker: str):
    """Verilen (soneksiz) ticker icin 3 aylik gunluk mum verisini dondurur.
    orn. /api/history/AKBNK -> AKBNK.IS icin OHLC. Ham veri /api/indicators
    ile paylasilir (bkz. _get_raw_history) - ayri bir Yahoo istegi yapmaz."""
    cache_key = ("history", ticker)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        hist = _get_raw_history(ticker)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if hist.empty:
        return jsonify(_cache_set(cache_key, []))
    recent = hist.tail(65)  # ~3 aylik islem gunu (6 aylik ham veriden son kismi)
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


SCORE_HISTORY_WINDOW_DAYS = 365  # Skor Gecmisi her zaman bugunden geriye bu kadar gun gosterir


@app.route("/api/score-history/<ticker>")
def api_score_history(ticker: str):
    """Verilen ticker icin skor gecmisini dondurur: once (varsa) GUNLUK
    backfill kayitlari (canli logun BASLAMADIGI eski tarihler icin,
    source="backfill" ile isaretli), sonra HER dongudeki (5 dk) ham canli
    kayit (source="live"). Gunluk sekmesi ikisini birlikte kullanir;
    Saatlik/5-Dakikalik sekmeleri sadece canli (gercek gun-ici
    granulariteli) kayitlari kullanir - bkz. templates/index.html
    renderScoreHistory().

    SCORE_HISTORY_WINDOW_DAYS'ten (365) eski satirlar ATLANIR - boylece
    "Skor Gecmisi" her istekte bugune gore yeniden hesaplanip HER ZAMAN
    tam olarak son 1 yili gosterir.

    Veri scanner.get_score_history() uzerinden (ticker, timestamp)
    indeksli bir SQLite sorgusuyla okunur - eskiden bellek ici bir
    index'ten okunurdu (canli CSV milyonlarca satira ciktiginda dosyayi
    bastan taramak grafigi saniyelerce yavaslatiyordu), artik ne dosya
    taramasi ne de tam tabloyu belleğe yukleme gerekiyor."""
    cache_key = ("score-history", ticker)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    cutoff_ts = (datetime.now() - timedelta(days=SCORE_HISTORY_WINDOW_DAYS)).strftime("%Y-%m-%d 00:00:00")
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
    Bantlarinin TAM zaman serisini dondurur - grafikte istege bagli
    acilir/kapanir indikator katmanlari icin. 6 aylik veri cekilir (gorunen
    son ~3 aylik araligin TAMAMINDA indikatorlerin saglikli yakinsamis olmasi
    icin ~3 aylik ekstra isinma payi birakilir), yanit son ~3 aya kirpilir -
    bkz. api_history() (mum grafigi de ayni 3 aylik araligi gosterir). Ham
    veri /api/history ile paylasilir (bkz. _get_raw_history) - ayri bir
    Yahoo istegi yapmaz."""
    cache_key = ("indicators", ticker)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        hist = _get_raw_history(ticker)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if hist.empty:
        return jsonify(_cache_set(cache_key, {}))

    closes = hist["Close"]
    ema9, ema21 = scanner.ema_series_pair(closes)
    rsi_vals = scanner.rsi_series(closes)
    macd_line, macd_sig, macd_hist = scanner.macd_series(closes)
    bb_upper, bb_mid, bb_lower = scanner.bollinger_bands_series(closes)
    dates = [idx.strftime("%Y-%m-%d") for idx in hist.index]

    def _series(values):
        trimmed = values.tail(65)
        trimmed_dates = dates[-len(trimmed):]
        return [
            {"time": t, "value": None if v != v else float(v)}
            for t, v in zip(trimmed_dates, trimmed)
        ]

    return jsonify(
        _cache_set(
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
    )


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


@app.route("/api/paper/equity")
@_login_required
def api_paper_equity():
    """Sanal hesabin equity (nakit + acik pozisyon degeri) gecmisi -
    gunluk/saatlik/5dk gruplama frontend'de yapilir (Skor Gecmisi'yle ayni desen)."""
    return jsonify(scanner.get_paper_equity_history("2000-01-01 00:00:00"))


@app.route("/api/paper/cash", methods=["POST"])
@_login_required
def api_paper_set_cash():
    """Sanal nakti elle istenen tutara ayarlar - kullanici Sanal Al-Sat
    panelinden duzenleyebiliyor."""
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "gecerli bir tutar (amount) gerekli"}), 400
    if amount < 0:
        return jsonify({"error": "tutar negatif olamaz"}), 400
    scanner.set_paper_cash(amount)
    return jsonify({"status": "ok", "cash": amount})


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
        return jsonify({"error": "gecerli bir baslangic tutari (amount) gerekli"}), 400
    if amount <= 0:
        return jsonify({"error": "tutar sifirdan buyuk olmali"}), 400
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
    open_manual_tickers = {
        p["ticker"] for p in scanner.get_paper_positions() if p["source"] == "manual"
    }
    enriched = [
        {
            **w,
            "current_price": price_by_ticker.get(w["ticker"]),
            "has_open_position": w["ticker"] in open_manual_tickers,
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
    otomatik sistemin 5-slot limitine DAHIL DEGIL (bkz. scanner.manual_buy)."""
    data = request.get_json(silent=True) or {}
    ticker = (data.get("ticker") or "").strip().upper()
    try:
        allocation = float(data.get("allocation"))
    except (TypeError, ValueError):
        return jsonify({"error": "gecerli bir tutar (allocation) gerekli"}), 400
    if not ticker:
        return jsonify({"error": "ticker gerekli"}), 400

    with _state_lock:
        price_by_ticker = {r["ticker"]: r["price"] for r in _state["rows"]}
    price = price_by_ticker.get(ticker)
    if price is None or price != price:
        return jsonify({"error": f"{ticker} icin guncel fiyat yok"}), 400

    result = scanner.manual_buy(ticker, allocation, price)
    if result is None:
        return jsonify({"error": "alim basarisiz - tutar gecersiz ya da nakit yetersiz"}), 400
    return jsonify(result)


@app.route("/api/paper/manual-sell", methods=["POST"])
@_login_required
def api_paper_manual_sell():
    """Kullanicinin elle, istedigi an satmasi - sadece kendi actigi
    (source='manual') acik bir pozisyon icin calisir."""
    data = request.get_json(silent=True) or {}
    try:
        trade_id = int(data.get("trade_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "gecerli bir trade_id gerekli"}), 400

    positions = {p["id"]: p for p in scanner.get_paper_positions()}
    pos = positions.get(trade_id)
    if pos is None:
        return jsonify({"error": "pozisyon bulunamadi"}), 404

    with _state_lock:
        price_by_ticker = {r["ticker"]: r["price"] for r in _state["rows"]}
    price = price_by_ticker.get(pos["ticker"])
    if price is None or price != price:
        return jsonify({"error": f"{pos['ticker']} icin guncel fiyat yok"}), 400

    result = scanner.manual_sell(trade_id, price)
    if result is None:
        return jsonify({"error": "satis basarisiz"}), 400
    return jsonify(result)


@app.route("/api/debug/test-telegram")
@_login_required
def api_debug_test_telegram():
    """GECICI test endpoint'i (2026-10-28) - Telegram mesaj formatlarinin
    hepsini (sinyal AL/SAT + otomatik alim/kar satisi/zarar satisi) gercek
    gonderim fonksiyonlariyla (sahte ama belirgin TEST ticker'lariyla) bir
    kez tetikler, boylece telefon uzerinden formati/ikonlari kontrol etmek
    icin tarama dongusunun gercek bir sinyal/islem uretmesini beklemeye
    gerek kalmaz. Kontrol bitince bu route KALDIRILACAK."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scanner._send_push_notifications([
        {"timestamp": now_str, "ticker": "TESTAL", "score": "GUCLU_AL", "price": 123.45, "daily_pct": 3.2},
        {"timestamp": now_str, "ticker": "TESTSAT", "score": "GUCLU_SAT", "price": 67.89, "daily_pct": -2.5},
    ])
    scanner._send_paper_trade_notification({
        "kind": "buy", "ticker": "TESTBUY", "price": 50.0, "shares": 2000.0,
        "allocation": 100000.0, "timestamp": now_str,
    })
    scanner._send_paper_trade_notification({
        "kind": "sell", "ticker": "TESTKAR", "entry_price": 50.0, "exit_price": 56.0,
        "exit_reason": "target", "pnl": 12000.0, "timestamp": now_str,
    })
    scanner._send_paper_trade_notification({
        "kind": "sell", "ticker": "TESTZARAR", "entry_price": 50.0, "exit_price": 46.0,
        "exit_reason": "stop", "pnl": -8000.0, "timestamp": now_str,
    })
    return jsonify({"sent": 5})


if __name__ == "__main__":
    # scanner_bistTop100 import edilirken DB/tablolar zaten hazirlandi
    # (bkz. scanner._init_db()) - eskiden burada ayrica tum skor gecmisi
    # dosyasi bellege okunurdu (buyuk dosyada birkac saniye surebiliyordu),
    # SQLite'a gecince bu adima gerek kalmadi: her /api/score-history
    # istegi indeksli bir sorguyla dogrudan DB'den okur.
    threading.Thread(target=_scan_loop, daemon=True).start()
    threading.Thread(target=_market_scan_loop, daemon=True).start()
    # 127.0.0.1: bu Flask gelistirme sunucusu dogrudan disariya acik degil -
    # VPS'te nginx bir reverse proxy olarak 80/443'ten 5000'e yonlendiriyor
    # (bkz. /etc/nginx/sites-available/momentum-scanner), SSL sonlandirma da
    # orada yapiliyor. Port 5000'in disaridan hic erisilememesi (sadece
    # nginx uzerinden gecmesi) guvenlik icin onemli.
    # threaded=True: bu olmadan Flask'in gelistirme sunucusu istekleri TEK TEK
    # isler - bir hisseye tiklandiginda ayni anda atilan 3 istek (history/
    # score-history/indicators) birbirini bloklardi, grafik "hic yuklenmiyor"
    # gibi hissettirebilirdi (ozellikle Yahoo yavas/rate-limit yaptiginda).
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
