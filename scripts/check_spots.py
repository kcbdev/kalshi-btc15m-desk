import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
con = sqlite3.connect(os.path.join(ROOT, "data", "kalshi15m.db"))
con.row_factory = sqlite3.Row

rows = list(con.execute("SELECT ts, spot FROM spot_ticks WHERE ts > ? ORDER BY ts", (1793836800,)))
print(f"spot ticks since 00:00: {len(rows)}")
prev = None
big = 0
for r in rows:
    s = float(r["spot"])
    if prev is not None:
        d = s - prev
        if abs(d) > 25:
            big += 1
            print(f"  jump {d:+7.2f} at {r['ts']:.0f} -> {s:.2f} (prev {prev:.2f})")
    prev = s
print(f"jumps > $25: {big} of {len(rows)} ticks")
vals = [float(r["spot"]) for r in rows]
print(f"min {min(vals):.2f} max {max(vals):.2f} range {max(vals) - min(vals):.2f}")
