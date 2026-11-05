"""Snapshot the live paper-trading database into seed/ for shipping in the image.

Uses the sqlite backup API so it is consistent even while the engine is writing.
Run: python scripts/make_seed.py
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "kalshi15m.db")
OUT_DIR = os.path.join(ROOT, "seed")
OUT = os.path.join(OUT_DIR, "kalshi15m.db")

os.makedirs(OUT_DIR, exist_ok=True)
if os.path.exists(OUT):
    os.remove(OUT)

src = sqlite3.connect(SRC, timeout=30)
dst = sqlite3.connect(OUT)
with dst:
    src.backup(dst)
dst.close()
src.close()

size = os.path.getsize(OUT)
con = sqlite3.connect(OUT)
con.row_factory = sqlite3.Row
stats = {
    "windows": con.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"],
    "trades": con.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"],
    "snapshots": con.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"],
    "balance": con.execute("SELECT balance FROM ledger ORDER BY id DESC LIMIT 1").fetchone()["balance"],
}
con.close()
print(f"seed written: {OUT} ({size / 1024:.0f} KB) {stats}")
sys.exit(0)
