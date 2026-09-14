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
sag panelde 6 aylik mum grafigi + istege bagli indikator katmanlari
(EMA/Bollinger/MACD/RSI) + Skor Gecmisi (gunluk/saatlik/5 dakikalik) gosterilir.
"""

import csv
import os
import threading
import time
from datetime import datetime, timedelta

import scanner_bistTop100 as scanner
import yfinance as yf
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

_state_lock = threading.Lock()
_state = {
    "rows": [],
    "missing_count": 0,
    "updated_at": None,
}


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


@app.route("/api/history/<ticker>")
def api_history(ticker: str):
    """Verilen (soneksiz) ticker icin 6 aylik gunluk mum verisini dondurur.
    orn. /api/history/AKBNK -> AKBNK.IS icin OHLC."""
    cache_key = ("history", ticker)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    yahoo_ticker = f"{ticker}.IS"
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="6mo", interval="1d", auto_adjust=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if hist.empty:
        return jsonify(_cache_set(cache_key, []))
    candles = [
        {
            "time": idx.strftime("%Y-%m-%d"),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        }
        for idx, row in hist.iterrows()
    ]
    return jsonify(_cache_set(cache_key, candles))


SCORE_HISTORY_WINDOW_DAYS = 365  # Skor Gecmisi her zaman bugunden geriye bu kadar gun gosterir


@app.route("/api/score-history/<ticker>")
def api_score_history(ticker: str):
    """Verilen ticker icin skor gecmisini dondurur: once (varsa)
    score_history_bist_backfill.csv'deki GUNLUK backfill kayitlari (canli
    logun BASLAMADIGI eski tarihler icin, source:"backfill" ile isaretli),
    sonra score_history_bist.csv'deki HER dongudeki (5 dk) ham canli kayit.
    Gunluk sekmesi ikisini birlikte kullanir; Saatlik/5-Dakikalik sekmeleri
    sadece canli (gercek gun-ici granulariteli) kayitlari kullanir - bkz.
    templates/index.html renderScoreHistory().

    Her iki kaynak da SCORE_HISTORY_WINDOW_DAYS'ten (365) eski satirlari
    ATLAR - boylece "Skor Gecmisi" her istekte bugune gore yeniden hesaplanip
    HER ZAMAN tam olarak son 1 yili gosterir, backfill dosyasi tekrar
    calistirilmasa bile (canli log zaten surekli buyuyerek yeni ucu
    doldurur, bu filtre sadece eski ucu bugune gore kirpar)."""
    cache_key = ("score-history", ticker)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    cutoff_date = (datetime.now() - timedelta(days=SCORE_HISTORY_WINDOW_DAYS)).strftime("%Y-%m-%d")

    live_events = []
    if os.path.isfile(scanner.SCORE_HISTORY_PATH):
        with open(scanner.SCORE_HISTORY_PATH, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("ticker") == ticker and row["timestamp"][:10] >= cutoff_date:
                    live_events.append(
                        {
                            "time": row["timestamp"],
                            "score_raw": float(row["score_raw"]),
                            "score": row["score"],
                            "price": row["price"],
                        }
                    )

    backfill_events = []
    if os.path.isfile(scanner.SCORE_HISTORY_BACKFILL_PATH):
        # Canli log hangi tarihten itibaren basliyorsa, backfill sadece ONDAN
        # ONCEKI gunleri doldurur - ayni gun icin iki kaynak cakismasin diye.
        live_start_date = min((e["time"][:10] for e in live_events), default=None)
        with open(scanner.SCORE_HISTORY_BACKFILL_PATH, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("ticker") != ticker:
                    continue
                if row["date"] < cutoff_date:
                    continue
                if live_start_date and row["date"] >= live_start_date:
                    continue
                backfill_events.append(
                    {
                        "time": f"{row['date']} 00:00:00",
                        "score_raw": float(row["score_raw"]),
                        "score": row["score"],
                        "price": row["price"],
                        "source": "backfill",
                    }
                )

    events = backfill_events + live_events
    return jsonify(_cache_set(cache_key, events))


@app.route("/api/indicators/<ticker>")
def api_indicators(ticker: str):
    """Verilen (soneksiz) ticker icin EMA9/21, MACD, RSI ve Bollinger
    Bantlarinin TAM zaman serisini dondurur - grafikte istege bagli
    acilir/kapanir indikator katmanlari icin. 1 yillik veri cekilir (gorunen
    son ~6 aylik araligin TAMAMINDA indikatorlerin saglikli yakinsamis olmasi
    icin ~6 aylik ekstra isinma payi birakilir), yanit son ~6 aya kirpilir -
    bkz. api_history() (mum grafigi de ayni 6 aylik araligi gosterir)."""
    cache_key = ("indicators", ticker)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    yahoo_ticker = f"{ticker}.IS"
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="1y", interval="1d", auto_adjust=False)
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
        trimmed = values.tail(130)
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


if __name__ == "__main__":
    threading.Thread(target=_scan_loop, daemon=True).start()
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
