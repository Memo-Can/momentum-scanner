#!/usr/bin/env python3
"""BIST evrenindeki (region=tr, filtresiz) TUM hisseler icin GUNLUK bazda
GECMISE DONUK Skor gecmisi olusturan TEK SEFERLIK bir backfill script'i.
Canli tarama dongusunun (web_bistTop100.py/terminal_bistTop100.py) bir
parcasi degildir, elle calistirilir - scanner_bistTop100.py'deki mevcut
indikator/skor fonksiyonlarini (OLDUGU GIBI) yeniden kullanir.

ONEMLI SINIRLAMA: Yahoo Finance 5 dakikalik (intraday) veriyi sadece son
~60 gun geriye saklar, bu yuzden gecmis gunler icin signal_5m (5 dakikalik
EMA trend) hesaplanamaz. Bu script signal_5m'i TUM gecmis gunler icin NOTR
kabul eder (confluence_score_raw'a 0 katkida bulunur) - canli skorun 6
bilesenden biri eksik kalir, bu yuzden GUCLU_AL/SAT esikleri canli veriye
gore biraz daha zor gecilir (fiili maksimum skor 6 yerine 5). Bu, gecmis
skorlari canli skorlarla birebir karsilastirilamaz yapar ama gunluk
trend/momentum bazli bir yaklasim olarak yine de anlamlidir.

Uretilen dosyalar (repo koku, .gitignore'daki mevcut score_history_*.csv /
signal_log_*.csv desenlerine zaten uyuyor):
  score_history_bist_backfill.csv  - her (tarih, ticker) icin bir skor satiri
  signal_log_bist_backfill.csv     - sadece GUCLU_AL/SAT'A GECIS gunleri
                                      (log_signal_changes'in gecmise donuk hali)

Calistirmak icin:
    python3 backfill_daily_bist.py [GERIYE_GIDILECEK_GUN_SAYISI]
Varsayilan: bugunden geriye 365 gun (son bir yil).
"""

import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import yfinance as yf

import scanner_bistTop100 as scanner

BACKFILL_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 365
BACKFILL_START = (datetime.now() - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%d")
# Indikatorlerin (EMA21/MACD26+9/RSI14/BB20/ADX14) yakinsamasi icin
# BACKFILL_START'tan ~6 ay once baslanir - ilk gunlerin degerleri de
# saglikli olsun diye.
WARMUP_START = (datetime.now() - timedelta(days=BACKFILL_DAYS + 180)).strftime("%Y-%m-%d")
MAX_WORKERS = 12

SCORE_HISTORY_OUT = scanner.SCORE_HISTORY_BACKFILL_PATH
SIGNAL_LOG_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_log_bist_backfill.csv")
OUT_FIELDS = ["date", "ticker", "score_raw", "score", "price"]


def compute_daily_rows(ticker: str, benchmark_pct):
    """Bir hisse icin BACKFILL_START'tan bugune kadar HER islem gunu icin
    bir skor satiri (score_raw, score, price) uretir - signal_5m NOTR
    sabitlenir (bkz. dosya basi aciklamasi)."""
    try:
        hist = yf.Ticker(ticker).history(start=WARMUP_START, interval="1d", auto_adjust=False)
    except Exception:
        return []
    if hist.empty:
        return []

    closes = hist["Close"]
    ema9, ema21 = scanner.ema_series_pair(closes)
    rsi_vals = scanner.rsi_series(closes)
    _, _, macd_hist = scanner.macd_series(closes)
    bb_upper, _, bb_lower = scanner.bollinger_bands_series(closes)
    adx_vals = scanner.adx_series(hist)
    daily_pct = closes.pct_change() * 100

    rows = []
    for ts in closes.index:
        if ts.strftime("%Y-%m-%d") < BACKFILL_START:
            continue

        price = float(closes.loc[ts])
        e9, e21 = ema9.loc[ts], ema21.loc[ts]
        signal_1d = "AL" if e9 > e21 else "SAT" if e9 < e21 else "NOTR"
        hist_val = macd_hist.loc[ts]
        signal_macd = "AL" if hist_val > 0 else "SAT" if hist_val < 0 else "NOTR"
        rsi_val = float(rsi_vals.loc[ts]) if rsi_vals.loc[ts] == rsi_vals.loc[ts] else float("nan")

        upper, lower = bb_upper.loc[ts], bb_lower.loc[ts]
        if upper == upper and lower == lower and upper != lower:
            bb_percent = (price - lower) / (upper - lower) * 100.0
        else:
            bb_percent = float("nan")

        adx_val = float(adx_vals.loc[ts]) if adx_vals.loc[ts] == adx_vals.loc[ts] else float("nan")

        d_pct = daily_pct.loc[ts]
        bench = benchmark_pct.get(ts, float("nan"))
        rel_strength = (d_pct - bench) if d_pct == d_pct and bench == bench else float("nan")

        score_raw = scanner.confluence_score_raw(signal_1d, "NOTR", signal_macd, rsi_val, bb_percent, rel_strength)
        score = scanner.confluence_label(signal_1d, "NOTR", signal_macd, rsi_val, bb_percent, adx_val, rel_strength)

        rows.append({
            "date": ts.strftime("%Y-%m-%d"),
            "ticker": ticker.removesuffix(".IS"),
            "score_raw": score_raw,
            "score": score,
            "price": price,
        })
    return rows


def signal_transitions(rows):
    """Bir hissenin gunluk skor dizisinden SADECE GUCLU_AL/SAT'a GECIS
    gunlerini cikarir - log_signal_changes()'in gecmise donuk karsiligi."""
    transitions = []
    prev_score = None
    for row in rows:
        if row["score"] != prev_score and row["score"] in ("GUCLU_AL", "GUCLU_SAT"):
            transitions.append(row)
        prev_score = row["score"]
    return transitions


def main():
    print(f"BIST evreni cekiliyor (region=tr, filtresiz)...")
    tickers = scanner.fetch_all_bist_tickers()
    print(f"{len(tickers)} hisse bulundu. Gecmis veri cekiliyor ({WARMUP_START} -> bugun, {BACKFILL_START}'tan itibaren yazilacak)...")

    bench_hist = yf.Ticker(scanner.BENCHMARK_TICKER).history(start=WARMUP_START, interval="1d", auto_adjust=False)
    benchmark_pct = bench_hist["Close"].pct_change() * 100

    all_score_rows = []
    all_signal_rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(compute_daily_rows, t, benchmark_pct): t for t in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            done += 1
            try:
                rows = future.result()
            except Exception as e:
                print(f"[{done}/{len(tickers)}] {ticker}: HATA - {e}")
                continue
            if rows:
                all_score_rows.extend(rows)
                all_signal_rows.extend(signal_transitions(rows))
            if done % 50 == 0 or done == len(tickers):
                print(f"[{done}/{len(tickers)}] tamamlandi")

    all_score_rows.sort(key=lambda r: (r["date"], r["ticker"]))
    all_signal_rows.sort(key=lambda r: (r["date"], r["ticker"]))

    with open(SCORE_HISTORY_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        writer.writerows(all_score_rows)

    with open(SIGNAL_LOG_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        writer.writerows(all_signal_rows)

    print(f"Tamamlandi: {len(all_score_rows)} skor satiri -> {SCORE_HISTORY_OUT}")
    print(f"           {len(all_signal_rows)} GUCLU_AL/SAT gecisi -> {SIGNAL_LOG_OUT}")


if __name__ == "__main__":
    main()
