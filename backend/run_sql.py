#!/usr/bin/env python3
"""momentum_scanner.db'ye karsi ad-hoc SQL calistirmak icin kucuk bir yardimci -
SELECT sonuclarini tablo olarak yazdirir, INSERT/UPDATE/DELETE'i commit edip
kac satirin etkilendigini yazdirir. VS Code SQLite eklentisi calismadiginda
(ya da VPS'te SSH uzerinden rapor/duzeltme gerektiginde) sqlite3 CLI'a
alternatif olarak kullanilir - scanner_bistTop100.get_db_connection() ile
AYNI DB/WAL ayarlarini kullanir, elle bir DB yolu tanimlamaz.

Kullanim:
    python3 run_sql.py "SELECT * FROM paper_trades ORDER BY id DESC LIMIT 10"
    python3 run_sql.py "UPDATE paper_account SET cash = 1000000 WHERE id = 1"

DIKKAT: INSERT/UPDATE/DELETE hemen commit edilir, geri alma yoktur - onemli
bir degisiklikten once ilgili satirlari SELECT ile once gorup onaylayin.
"""

import sys

import scanner_bistTop100 as scanner


def main():
    if len(sys.argv) < 2:
        print('Kullanim: python3 run_sql.py "<SQL>"')
        sys.exit(1)

    sql = sys.argv[1]
    conn = scanner.get_db_connection()
    try:
        cur = conn.execute(sql)
        if sql.strip().upper().startswith("SELECT"):
            rows = cur.fetchall()
            if not rows:
                print("(sonuc yok)")
                return
            cols = rows[0].keys()
            widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
            print(" | ".join(c.ljust(w) for c, w in zip(cols, widths)))
            print("-+-".join("-" * w for w in widths))
            for r in rows:
                print(" | ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
            print(f"\n{len(rows)} satir")
        else:
            conn.commit()
            print(f"{cur.rowcount} satir etkilendi, commit edildi.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
