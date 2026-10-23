#!/usr/bin/env python3
"""TEK SEFERLIK migrasyon script'i: backend/data/ altindaki eski CSV
dosyalarini (score_history_bist[_backfill].csv, signal_log_bist[_backfill].csv)
scanner_bistTop100.py'nin SQLite veritabanina (backend/data/momentum_scanner.db)
aktarir. Her iki ortamda (yerel + VPS) BIR KEZ calistirilir.

Ne yapar:
  1. scanner_bistTop100 import edilir (DB/tablolar otomatik olusur).
  2. Hedef tablolarda zaten satir varsa DURUR (--force verilmeden tekrar
     calistirip veri cakismasini/duplikasyonunu onlemek icin).
  3. 4 CSV dosyasini okur, source='live'/'backfill' etiketiyle DB'ye yazar.
  4. CSV satir sayisi ile DB'ye yazilan satir sayisini KARSILASTIRIR - fark
     varsa HATA verir, DB'yi geri alir (hicbir satir commit edilmez).
  5. Basarili olursa eski CSV'leri SILMEZ, ".migrated" uzantisi ekleyerek
     yeniden adlandirir (rollback icin yedek olarak).

Calistirmak icin:
    python3 migrate_csv_to_sqlite.py [--force]
"""

import csv
import os
import sys

import scanner_bistTop100 as scanner

FORCE = "--force" in sys.argv


def _existing_row_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _migrate_score_history(conn, path, source):
    if not os.path.isfile(path):
        print(f"  (atlandi, dosya yok: {path})")
        return 0
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            ts = row["timestamp"] if "timestamp" in row else f"{row['date']} 00:00:00"
            rows.append(
                {
                    "timestamp": ts,
                    "ticker": row["ticker"],
                    "score_raw": float(row["score_raw"]) if row["score_raw"] else None,
                    "score": row["score"],
                    "price": float(row["price"]) if row["price"] else None,
                    "source": source,
                }
            )
    conn.executemany(
        """INSERT INTO score_history (timestamp, ticker, score_raw, score, price, source)
           VALUES (:timestamp, :ticker, :score_raw, :score, :price, :source)""",
        rows,
    )
    return len(rows)


def _migrate_signal_log(conn, path, source):
    if not os.path.isfile(path):
        print(f"  (atlandi, dosya yok: {path})")
        return 0
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            ts = row["timestamp"] if "timestamp" in row else f"{row['date']} 00:00:00"
            rows.append(
                {
                    "timestamp": ts,
                    "ticker": row["ticker"],
                    "score": row["score"],
                    "price": float(row["price"]) if row.get("price") else None,
                    # Backfill CSV'sinde bu 4 sutun yok (sadece score_raw vardi,
                    # signal_log tablosunda karsiligi yok, dusuruluyor).
                    "daily_pct": float(row["daily_pct"]) if row.get("daily_pct") else None,
                    "rsi": float(row["rsi"]) if row.get("rsi") else None,
                    "adx": float(row["adx"]) if row.get("adx") else None,
                    "rel_strength": float(row["rel_strength"]) if row.get("rel_strength") else None,
                    "source": source,
                }
            )
    conn.executemany(
        """INSERT INTO signal_log
               (timestamp, ticker, score, price, daily_pct, rsi, adx, rel_strength, source)
           VALUES (:timestamp, :ticker, :score, :price, :daily_pct, :rsi, :adx, :rel_strength, :source)""",
        rows,
    )
    return len(rows)


def main():
    conn = scanner.get_db_connection()
    existing = _existing_row_count(conn, "score_history") + _existing_row_count(conn, "signal_log")
    if existing and not FORCE:
        print(f"DB'de zaten {existing} satir var - migrasyon muhtemelen daha once yapildi.")
        print("Yine de calistirmak icin: python3 migrate_csv_to_sqlite.py --force")
        conn.close()
        sys.exit(1)

    paths = {
        "score_history_live": os.path.join(scanner.DATA_DIR, "score_history_bist.csv"),
        "score_history_backfill": os.path.join(scanner.DATA_DIR, "score_history_bist_backfill.csv"),
        "signal_log_live": os.path.join(scanner.DATA_DIR, "signal_log_bist.csv"),
        "signal_log_backfill": os.path.join(scanner.DATA_DIR, "signal_log_bist_backfill.csv"),
    }

    try:
        print("score_history (live)...")
        n1 = _migrate_score_history(conn, paths["score_history_live"], "live")
        print(f"  {n1} satir eklendi")

        print("score_history (backfill)...")
        n2 = _migrate_score_history(conn, paths["score_history_backfill"], "backfill")
        print(f"  {n2} satir eklendi")

        print("signal_log (live)...")
        n3 = _migrate_signal_log(conn, paths["signal_log_live"], "live")
        print(f"  {n3} satir eklendi")

        print("signal_log (backfill)...")
        n4 = _migrate_signal_log(conn, paths["signal_log_backfill"], "backfill")
        print(f"  {n4} satir eklendi")

        db_score_count = _existing_row_count(conn, "score_history")
        db_signal_count = _existing_row_count(conn, "signal_log")

        if db_score_count != n1 + n2 or db_signal_count != n3 + n4:
            raise RuntimeError(
                f"Satir sayisi tutarsizligi: score_history DB={db_score_count} "
                f"beklenen={n1 + n2}, signal_log DB={db_signal_count} beklenen={n3 + n4}"
            )

        conn.commit()
        print(f"\nBasarili: score_history={db_score_count} satir, signal_log={db_signal_count} satir.")
    except Exception:
        conn.rollback()
        conn.close()
        print("\nHATA - hicbir satir commit edilmedi, DB migrasyon oncesi haliyle kaldi.")
        raise
    finally:
        conn.close()

    backup_dir = os.path.join(scanner.DATA_DIR, "_backup")
    os.makedirs(backup_dir, exist_ok=True)
    print(f"\nEski CSV'ler siliniyor degil, '{backup_dir}' altina tasiniyor:")
    for path in paths.values():
        if os.path.isfile(path):
            new_path = os.path.join(backup_dir, os.path.basename(path))
            os.rename(path, new_path)
            print(f"  {path} -> {new_path}")


if __name__ == "__main__":
    main()
