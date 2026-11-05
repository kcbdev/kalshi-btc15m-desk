import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
con = sqlite3.connect(os.path.join(ROOT, "data", "kalshi15m.db"))
con.row_factory = sqlite3.Row

for wid in ("KXBTC15M-26NOV041900",):
    w = con.execute("SELECT * FROM windows WHERE window_id=?", (wid,)).fetchone()
    if not w:
        print("no window", wid)
        continue
    print(f"window {wid} strike {w['strike']} open {w['open_ts']} close {w['close_ts']}")
    for s in con.execute("SELECT * FROM snapshots WHERE window_id=? ORDER BY ts", (wid,)):
        t = float(s["ts"]) - float(w["open_ts"])
        print(f"  +{t:6.0f}s spot {float(s['spot']):9.2f} yes {float(s['yes_bid']):.3f}/{float(s['yes_ask']):.3f} "
              f"pm {float(s['p_model'] or 0):.3f} left {float(s['time_left']):5.0f}")
