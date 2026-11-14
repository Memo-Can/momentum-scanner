#!/usr/bin/env python3
"""BIST evrenindeki (region=tr, filtresiz) TUM hisseler icin GUNLUK bazda
GECMISE DONUK Skor gecmisi olusturan TEK SEFERLIK bir backfill script'i.
Canli tarama dongusunun (bist_web.py/bist_terminal.py) bir
parcasi degildir, elle calistirilir - bist_scanner.py'deki mevcut
indikator/skor fonksiyonlarini (OLDUGU GIBI) yeniden kullanir.

NOT (2026-10-31): Confluence skoru artik 5 bilesenden olusuyor (signal_5m
kaldirildi, bkz. CLAUDE.md "5 dakikalik bilesen kaldirildi") - bu script'in
daha once signal_5m'i TUM gecmis gunler icin NOTR sabitlemesi (Yahoo'nun 5
dakikalik veriyi sadece ~60 gun geriye saklamasi yuzunden) gerekiyordu;
simdi canli hesaplamayla ZATEN birebir aynisi, ayrica bir sinirlama/fark YOK.

NOT (2026-11-01): Script artik DESTRUCTIVE (once sil, sonra yeniden yaz)
DEGIL, EKLEMELI (additive) - her hisse icin BACKFILL_DAYS (varsayilan 365,
"son 1 yil") kadar gecmis cekilip, SADECE score_history'de (herhangi bir
source - 'live' ya da 'backfill') HENUZ OLMAYAN tarihler icin satir yazilir
("eksik gunleri doldur" mantigi). Tekrar tekrar calistirmak ne veri kaybeder
ne de satir biriktirir - zaten dolu olan gunlere DOKUNULMUYOR, sadece
pencere icindeki eksik gunler (varsa) ekleniyor. Boylece orn. once 265 gunluk
bir calistirma yapilmissa, sonra 365 gunle calistirinca SADECE aradaki eksik
~100 gun (+ varsa yeni islem gunleri) eklenir, 265 gunluk kisim YENIDEN
hesaplanip YAZILMAZ.

Yazilan hedef (backend/data/momentum_scanner.db, bist_scanner.py'nin
SQLite veritabani - eskiden iki ayri backfill CSV'si idi):
  score_history tablosu, source='backfill'  - eksik olan her (tarih, ticker) icin bir skor satiri
  signal_log tablosu, source='backfill'     - yeni eklenen gunler icindeki GUCLU_AL/SAT/MUTLAK
                                               gecisleri (log_signal_changes'in gecmise donuk hali)

Calistirmak icin (backend/bist/ dizininden, 2026-11-14'te buraya tasindi -
bkz. CLAUDE.md "Dosyalar bist/ klasorune tasindi"):
    python3 bist_backfill_daily.py [GERIYE_GIDILECEK_GUN_SAYISI]
Varsayilan: bugunden geriye 365 gun (son bir yil).
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import yfinance as yf

import bist_indicators as indicators
import bist_market_data as market_data
import bist_scanner as scanner

BACKFILL_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 365
BACKFILL_START = (datetime.now() - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%d")
# Indikatorlerin (EMA21/MACD26+9/RSI14/BB20/ADX14) yakinsamasi icin
# pencerenin ~6 ay oncesinden baslanir - ilk gunlerin degerleri de saglikli
# olsun diye (bu gunler YAZILMAZ, sadece hesaplama icin kullanilir).
WARMUP_START = (datetime.now() - timedelta(days=BACKFILL_DAYS + 180)).strftime("%Y-%m-%d")
MAX_WORKERS = 12


def load_existing_dates() -> dict:
    """score_history'deki (source farki YOK, 'live' + 'backfill' birlikte)
    her ticker icin HALIHAZIRDA var olan tarihlerin kumesini TEK bir sorguyla
    belleğe yukler - 628 hisse icin 628 ayri sorgu yerine."""
    conn = scanner.get_db_connection()
    try:
        cur = conn.execute("SELECT ticker, substr(timestamp, 1, 10) AS d FROM score_history")
        existing: dict = {}
        for ticker, d in cur.fetchall():
            existing.setdefault(ticker, set()).add(d)
        return existing
    finally:
        conn.close()


def compute_daily_rows(ticker: str, benchmark_pct, existing_dates: set):
    """Bir hisse icin WARMUP_START'tan bugune kadar skor hesaplar, ama SADECE
    (a) BACKFILL_DAYS penceresi icindeki VE (b) existing_dates'te (score_history'de
    zaten var olan tarihler) OLMAYAN gunleri dondurur - "son 1 yilin eksik
    gunlerini doldur" mantigi."""
    try:
        hist = yf.Ticker(ticker).history(start=WARMUP_START, interval="1d", auto_adjust=False)
    except Exception:
        return []
    if hist.empty:
        return []

    closes = hist["Close"]
    ema9, ema21 = indicators.ema_series_pair(closes)
    rsi_vals = indicators.rsi_series(closes)
    _, _, macd_hist = indicators.macd_series(closes)
    bb_upper, _, bb_lower = indicators.bollinger_bands_series(closes)
    adx_vals = indicators.adx_series(hist)
    daily_pct = closes.pct_change() * 100

    rows = []
    for ts in closes.index:
        date_str = ts.strftime("%Y-%m-%d")
        if date_str < BACKFILL_START:
            continue  # sadece warmup/indikator hesaplamasi icin cekildi, pencere disinda - yazilmaz
        if date_str in existing_dates:
            continue  # bu gun zaten DB'de var (live ya da onceki backfill) - dokunma

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

        score_raw = indicators.confluence_score_raw(signal_1d, signal_macd, rsi_val, bb_percent, rel_strength)
        score = indicators.confluence_label(signal_1d, signal_macd, rsi_val, bb_percent, adx_val, rel_strength)

        rows.append({
            "date": date_str,
            "ticker": ticker.removesuffix(".IS"),
            "score_raw": score_raw,
            "score": score,
            "price": price,
        })
    return rows


def signal_transitions(rows):
    """Bir hissenin (sadece YENI eklenen, kronolojik) gunluk skor dizisinden
    SADECE MUTLAK_AL/GUCLU_AL/GUCLU_SAT/MUTLAK_SAT'a GECIS gunlerini cikarir -
    log_signal_changes()'in gecmise donuk karsiligi (bkz.
    scanner._STRONG_SIGNAL_LABELS). rows zaten sadece YENI eklenen (onceden
    DB'de olmayan) gunleri icerdigi icin, bu gecisler SADECE o yeni blok
    icinde tespit edilir - DB'de zaten var olan bitisik gunlerle olan sinirda
    (eski gun -> yeni blokun ilk gunu) bir gecis atlanabilir, kabul edilen
    kucuk bir sinir durumu."""
    transitions = []
    prev_score = None
    for row in rows:
        if row["score"] != prev_score and row["score"] in scanner._STRONG_SIGNAL_LABELS:
            transitions.append(row)
        prev_score = row["score"]
    return transitions


def main():
    print(f"BIST evreni cekiliyor (region=tr, filtresiz)...")
    tickers = market_data.fetch_all_bist_tickers()
    print(f"{len(tickers)} hisse bulundu. Mevcut score_history tarihleri yukleniyor...")
    existing_by_ticker = load_existing_dates()

    print(f"Gecmis veri cekiliyor ({WARMUP_START} -> bugun, {BACKFILL_START}'tan itibaren yazilacak, sadece eksik gunler)...")
    bench_hist = yf.Ticker(market_data.BENCHMARK_TICKER).history(start=WARMUP_START, interval="1d", auto_adjust=False)
    benchmark_pct = bench_hist["Close"].pct_change() * 100

    all_score_rows = []
    all_signal_rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(
                compute_daily_rows, t, benchmark_pct,
                existing_by_ticker.get(t.removesuffix(".IS"), set()),
            ): t
            for t in tickers
        }
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

    with scanner._DB_LOCK:
        conn = scanner.get_db_connection()
        try:
            # ARTIK DESTRUCTIVE DEGIL - mevcut 'backfill'/'live' satirlarina
            # DOKUNULMUYOR, sadece eksik (yeni) gunler EKLENIYOR.
            conn.executemany(
                """INSERT INTO score_history (timestamp, ticker, score_raw, score, price, source)
                   VALUES (:timestamp, :ticker, :score_raw, :score, :price, 'backfill')""",
                [{**r, "timestamp": f"{r['date']} 00:00:00"} for r in all_score_rows],
            )
            conn.executemany(
                """INSERT INTO signal_log
                       (timestamp, ticker, score, price, daily_pct, rsi, adx, rel_strength, source)
                   VALUES (:timestamp, :ticker, :score, :price, NULL, NULL, NULL, NULL, 'backfill')""",
                [{**r, "timestamp": f"{r['date']} 00:00:00"} for r in all_signal_rows],
            )
            conn.commit()
        finally:
            conn.close()

    print(f"Tamamlandi: {len(all_score_rows)} YENI (eksik) skor satiri -> score_history (source='backfill')")
    print(f"           {len(all_signal_rows)} YENI MUTLAK/GUCLU_AL/SAT gecisi -> signal_log (source='backfill')")


if __name__ == "__main__":
    main()
