#!/usr/bin/env python3
"""scanner_usTop100.py'nin canli verisini yerel bir web arayuzunde gosterir.

Mevcut scanner_usTop100 modulunun tum tarama/indikator mantigini (fetch_row,
fetch_with_retry, confluence_label vb.) OLDUGU GIBI yeniden kullanir - burada
sadece bu veriyi bir arka plan thread'inde REFRESH_SECONDS'ta bir tazeleyip
basit bir JSON API + HTML sayfasi uzerinden sunuyoruz. Tarama mantigi
degismedi, sadece terminal yerine tarayicida gosteriliyor.

Calistirmak icin:
    python3 web_usTop100.py
Sonra tarayicida: http://127.0.0.1:5001

NOT: Port 5000 degil 5001 kullanilir - boylece web_bistTop100.py ile ayni
makinede aynı anda calisabilirler, cakisma olmaz.

Sayfa acikken tablo REFRESH_SECONDS'a yakin bir surede kendiliginden
yenilenir (JS polling ile /api/rows'a istek atar). Bir hisseye tiklayinca
sag panelde 3 aylik mum grafigi + GUCLU_AL/GUCLU_SAT sinyal gecmisi
(signal_log_us.csv'den) gosterilir.
"""

import csv
import json
import os
import threading
import time
from datetime import datetime

import scanner_usTop100 as scanner
import yfinance as yf
from flask import Flask, Response, jsonify, render_template

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


def _scan_once():
    """scanner_usTop100.main() ile birebir ayni secim/siralama mantigi -
    sadece render()/play_refresh_beep() yerine sonucu _state'e yazar."""
    pinned_symbols = set(scanner.TOP_US_TICKERS)
    gainer_tickers = scanner.fetch_top_gainer_tickers(scanner.TOP_N + scanner.CANDIDATE_BUFFER)
    candidate_tickers = list(dict.fromkeys(scanner.TOP_US_TICKERS + gainer_tickers))
    rows_by_ticker, _still_missing = (
        scanner.fetch_with_retry(candidate_tickers) if candidate_tickers else ({}, [])
    )

    pinned_rows = [r for r in rows_by_ticker.values() if r["ticker"] in pinned_symbols]
    other_rows = [r for r in rows_by_ticker.values() if r["ticker"] not in pinned_symbols]
    other_rows_sorted = sorted(other_rows, key=lambda r: r["daily"], reverse=True)

    extra_slots = max(0, scanner.TOP_N - len(pinned_rows))
    top_rows = pinned_rows + other_rows_sorted[:extra_slots]
    missing_count = max(0, len(scanner.TOP_US_TICKERS) - len(pinned_rows))

    def _sort_key(row):
        daily = row["daily"]
        daily_sort = -daily if daily == daily else float("inf")
        return (scanner.SCORE_ORDER.get(row["score"], 2), daily_sort)

    rows_sorted = sorted(top_rows, key=_sort_key)

    if top_rows:
        scanner.log_signal_changes(top_rows)
        scanner.log_score_history(top_rows)

    with _state_lock:
        _state["rows"] = [_row_to_json(r) for r in rows_sorted]
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
        title="ABD Momentum Scanner",
    )


@app.route("/api/rows")
def api_rows():
    with _state_lock:
        return jsonify(dict(_state))


@app.route("/api/history/<ticker>")
def api_history(ticker: str):
    """Verilen ticker icin 3 aylik gunluk mum verisini dondurur. ABD
    hisselerinde BIST'in aksine sonek (.IS gibi) yok, ticker dogrudan
    Yahoo sembolu."""
    try:
        hist = yf.Ticker(ticker).history(period="3mo", interval="1d", auto_adjust=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if hist.empty:
        return jsonify([])
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
    return jsonify(candles)


@app.route("/api/signals/<ticker>")
def api_signals(ticker: str):
    """Verilen ticker icin signal_log_us.csv'deki GUCLU_AL/GUCLU_SAT
    gecmisini dondurur - grafikte isaretci olarak gosterilir."""
    if not os.path.isfile(scanner.SIGNAL_LOG_PATH):
        return jsonify([])
    events = []
    with open(scanner.SIGNAL_LOG_PATH, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("ticker") == ticker:
                events.append(
                    {
                        "time": row["timestamp"],
                        "score": row["score"],
                        "price": row["price"],
                    }
                )
    return jsonify(events)


@app.route("/api/score-history/<ticker>")
def api_score_history(ticker: str):
    """Verilen ticker icin score_history_us.csv'deki HER dongudeki (5 dk)
    ham skor kaydini dondurur. Gunluk/saatlik ortalama ve 5 dakikalik ham
    veri sekmeleri bu tek listeden istemci tarafinda hesaplanir."""
    if not os.path.isfile(scanner.SCORE_HISTORY_PATH):
        return jsonify([])
    events = []
    with open(scanner.SCORE_HISTORY_PATH, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("ticker") == ticker:
                events.append(
                    {
                        "time": row["timestamp"],
                        "score_raw": float(row["score_raw"]),
                        "score": row["score"],
                        "price": row["price"],
                    }
                )
    return jsonify(events)


if __name__ == "__main__":
    threading.Thread(target=_scan_loop, daemon=True).start()
    # NOT: 0.0.0.0 disaridan erisime acar - VPS'te firewall'da 5001 portuna
    # izin vermeniz gerekir. Sadece kendi makinenizden erismek yeterliyse
    # (SSH tuneli ile) bunun yerine "127.0.0.1" kullanip firewall'a hic
    # dokunmamak daha guvenlidir.
    app.run(host="0.0.0.0", port=5001, debug=False)
