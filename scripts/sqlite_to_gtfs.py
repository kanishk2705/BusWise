"""Describe a GTFS-in-SQLite file and export GTFS tables it contains to CSV.

Usage: python sqlite_to_gtfs.py <db> <out_dir>
"""

import csv
import os
import sqlite3
import sys

db, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
con = sqlite3.connect(db)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
for t in tables:
    cols = [r[1] for r in con.execute(f"pragma table_info('{t}')")]
    n = con.execute(f"select count(*) from '{t}'").fetchone()[0]
    print(f"table {t}: {n:,} rows, columns {cols}")
    print("   sample:", con.execute(f"select * from '{t}' limit 2").fetchall())
    name = t.lower()
    if name in {"stops", "routes", "trips", "stop_times", "calendar", "agency"}:
        with open(os.path.join(out, f"{name}.txt"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows(con.execute(f"select * from '{t}'"))
print("exported:", sorted(os.listdir(out)))
