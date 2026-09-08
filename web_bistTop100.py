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
sag panelde 3 aylik mum grafigi + GUCLU_AL/GUCLU_SAT sinyal gecmisi
(signal_log_bist.csv'den) gosterilir.
"""

import csv
import json
import os
import threading
import time
from datetime import datetime

import scanner_bistTop100 as scanner
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
    """scanner_bistTop100.main() ile birebir ayni secim/siralama mantigi -
    sadece render()/play_refresh_beep() yerine sonucu _state'e yazar."""
    bist30_symbols = {t.removesuffix(".IS") for t in scanner.BIST30_TICKERS}
    gainer_tickers = scanner.fetch_top_gainer_tickers(scanner.TOP_N + scanner.CANDIDATE_BUFFER)
    candidate_tickers = list(dict.fromkeys(scanner.BIST30_TICKERS + gainer_tickers))
    rows_by_ticker, _still_missing = (
        scanner.fetch_with_retry(candidate_tickers) if candidate_tickers else ({}, [])
    )

    bist30_rows = [r for r in rows_by_ticker.values() if r["ticker"] in bist30_symbols]
    other_rows = [r for r in rows_by_ticker.values() if r["ticker"] not in bist30_symbols]
    other_rows_sorted = sorted(other_rows, key=lambda r: r["daily"], reverse=True)

    extra_slots = max(0, scanner.TOP_N - len(bist30_rows))
    top_rows = bist30_rows + other_rows_sorted[:extra_slots]
    missing_count = max(0, len(scanner.BIST30_TICKERS) - len(bist30_rows))

    def _sort_key(row):
        daily = row["daily"]
        daily_sort = -daily if daily == daily else float("inf")
        return (scanner.SCORE_ORDER.get(row["score"], 2), daily_sort)

    rows_sorted = sorted(top_rows, key=_sort_key)

    if top_rows:
        scanner.log_signal_changes(top_rows)

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
        title="BIST Momentum Scanner",
    )


@app.route("/api/rows")
def api_rows():
    with _state_lock:
        return jsonify(dict(_state))


@app.route("/api/history/<ticker>")
def api_history(ticker: str):
    """Verilen (soneksiz) ticker icin 3 aylik gunluk mum verisini dondurur.
    orn. /api/history/AKBNK -> AKBNK.IS icin OHLC."""
    yahoo_ticker = f"{ticker}.IS"
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="3mo", interval="1d", auto_adjust=False)
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
    """Verilen ticker icin signal_log_bist.csv'deki GUCLU_AL/GUCLU_SAT
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


if __name__ == "__main__":
    threading.Thread(target=_scan_loop, daemon=True).start()
    # NOT: 0.0.0.0 disaridan erisime acar - VPS'te firewall'da 5000 portuna
    # izin vermeniz gerekir. Sadece kendi makinenizden erismek yeterliyse
    # (SSH tuneli ile) bunun yerine "127.0.0.1" kullanip firewall'a hic
    # dokunmamak daha guvenlidir.
    app.run(host="0.0.0.0", port=5000, debug=False)
