"""SQLite veritabaninin (momentum_scanner.db) zamanlanmis (cron) yedegini
alir - tek seferlik elle calistirma DEGIL, duzenli calismasi beklenen bir
bakim script'i (bkz. CLAUDE.md "Veritabani yedegi").

sqlite3 modulunun yerlesik Connection.backup() API'si kullanilir (dosyayi
duz kopyalamak DEGIL) - WAL modunda calisan canli bir veritabanini guvenli
(tutarli, kilitlenmeyen) sekilde yedekler, servisi durdurmaya gerek yok.

Calistirma: python3 backup_db.py  (venv'in python3'u ile, requirements.txt'teki
paketlere ihtiyac duymaz - sadece stdlib: sqlite3/gzip/shutil)
Onerilen cron: her gun 03:00'te, bkz. CLAUDE.md'deki tam crontab satiri.
"""

import gzip
import os
import shutil
import sqlite3
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "momentum_scanner.db")
BACKUP_DIR = os.path.join(DATA_DIR, "_db_backups")
KEEP_LAST = 14  # ~2 hafta gunluk yedek - daha eskisi otomatik silinir


def main() -> None:
    if not os.path.exists(DB_PATH):
        print("Veritabani bulunamadi, yedek alinamadi:", DB_PATH)
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = os.path.join(BACKUP_DIR, f"momentum_scanner_{timestamp}.db")

    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(raw_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    gz_path = raw_path + ".gz"
    with open(raw_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw_path)
    print("Yedek alindi:", gz_path, f"({os.path.getsize(gz_path) / 1024 / 1024:.1f} MB)")

    backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db.gz"))
    for old in backups[:-KEEP_LAST]:
        os.remove(os.path.join(BACKUP_DIR, old))
        print("Eski yedek silindi:", old)


if __name__ == "__main__":
    main()
