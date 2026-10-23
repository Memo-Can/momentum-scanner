#!/usr/bin/env python3
"""scanner_bistTop100.py'nin canli verisini yerel bir web arayuzunde gosterir.

Mevcut scanner_bistTop100 modulunun tum tarama/indikator mantigini (fetch_row,
fetch_with_retry, confluence_label vb.) OLDUGU GIBI yeniden kullanir - burada
sadece bu veriyi bir arka plan thread'inde REFRESH_SECONDS'ta bir tazeleyip
basit bir JSON API + HTML sayfasi uzerinden sunuyoruz. Tarama mantigi
degismedi, sadece terminal yerine tarayicida gosteriliyor.

Calistirmak icin:
    python3 web_bistTop100.py
Sonra tarayicida: http://127.0.0.1:5000

Sayfa acikken tablo REFRESH_SECONDS'a yakin bir surede kendiliginden
yenilenir (JS polling ile /api/rows'a istek atar). Bir hisseye tiklayinca
sag panelde 3 aylik mum grafigi + istege bagli indikator katmanlari
(EMA/Bollinger/MACD/RSI) + Skor Gecmisi (gunluk/saatlik/5 dakikalik) gosterilir.
"""

import os
import threading
import time
from datetime import datetime, timedelta

import scanner_bistTop100 as scanner
import yfinance as yf
from flask import Flask, jsonify, render_template, request

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
app = Flask(
    __name__,
    template_folder=os.path.join(_FRONTEND_DIR, "templates"),
    static_folder=os.path.join(_FRONTEND_DIR, "static"),
)

_state_lock = threading.Lock()
_state = {
    "rows": [],
    "missing_count": 0,
    "updated_at": None,
}

# BIST sekmelerinin sagindaki doviz/emtia/BIST100/BTC seridi - hisse
# taramasindan bagimsiz, kendi thread'inde ayni REFRESH_SECONDS ritmiyle
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
# _benchmark_cache ile AYNI desen (TTL = REFRESH_SECONDS-5). Bir hisse zaten
# secilip verisi cekildiyse, ayni hisseye kisa surede tekrar tiklandiginda
# (ya da farkli bir tarayicidan ayni hisseye bakildiginda) Yahoo'ya tekrar
# istek atmak yerine onbellekten donulur - tarama zaten 5 dakikada bir
# yenilendigi icin daha sik bir veriye ihtiyac yok.
_chart_cache = {}  # (endpoint_adi, ticker) -> (fetched_at, json'lanabilir deger)
CHART_CACHE_TTL = scanner.REFRESH_SECONDS - 5


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
    all_tickers = list(dict.fromkeys(scanner.BIST100_TICKERS + top_gainers))

    rows_by_ticker, _still_missing = scanner.fetch_with_retry(all_tickers) if all_tickers else ({}, [])
    missing_count = max(0, len(all_tickers) - len(rows_by_ticker))

    def _sort_key(row):
        daily = row["daily"]
        daily_sort = -daily if daily == daily else float("inf")
        return (scanner.SCORE_ORDER.get(row["score"], 2), daily_sort)

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
    while True:
        try:
            _scan_once()
        except Exception as e:
            print("Tarama hatasi:", e)
        time.sleep(scanner.REFRESH_SECONDS)


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
        time.sleep(scanner.REFRESH_SECONDS)


@app.route("/")
def index():
    return render_template(
        "index.html",
        refresh_seconds=scanner.REFRESH_SECONDS,
        title="BIST Momentum Scanner",
    )


@app.route("/api/rows")
def api_rows():
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
RAW_HISTORY_CACHE_TTL = scanner.REFRESH_SECONDS - 5


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


@app.route("/api/register-device", methods=["POST"])
def register_device():
    """Mobil uygulamanin (Expo push token'i aldiktan sonra) kaydolmasi icin -
    bir GUCLU_AL/SAT gecisinde bu token'a push bildirimi gonderilecek. Ayni
    token tekrar gelirse (uygulama her acildiginda oldugu gibi) sorun degil,
    register_device_token upsert yapar."""
    data = request.get_json(silent=True) or {}
    token, platform = data.get("token"), data.get("platform")
    if not token or platform not in ("android", "ios"):
        return jsonify({"error": "token and platform ('android'|'ios') required"}), 400
    scanner.register_device_token(token, platform)
    return jsonify({"status": "ok"})


@app.route("/api/paper/summary")
def api_paper_summary():
    """Sanal al-sat hesabinin ozeti: nakit, equity, toplam/gerceklesmis/
    gerceklesmemis K-Z, kazanma orani. bkz. scanner.run_paper_trading_cycle()."""
    return jsonify(scanner.get_paper_summary())


@app.route("/api/paper/positions")
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
def api_paper_trades():
    """Kapanmis sanal islemler (en yeni once)."""
    return jsonify(scanner.get_paper_trades())


@app.route("/api/paper/equity")
def api_paper_equity():
    """Sanal hesabin equity (nakit + acik pozisyon degeri) gecmisi -
    gunluk/saatlik/5dk gruplama frontend'de yapilir (Skor Gecmisi'yle ayni desen)."""
    return jsonify(scanner.get_paper_equity_history("2000-01-01 00:00:00"))


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
