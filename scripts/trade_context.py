import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
con = sqlite3.connect(os.path.join(ROOT, "data", "kalshi15m.db"))
con.row_factory = sqlite3.Row

for t in con.execute("SELECT * FROM trades ORDER BY entry_ts"):
    print(f"\nTRADE {t['id']} {t['side']} {t['contracts']} @ {t['entry_price']} pnl {t['pnl']} status {t['status']}")
    d = con.execute("SELECT * FROM debates WHERE id=?", (t["debate_id"],)).fetchone()
    if not d:
        print("  no debate")
        continue
    f = json.loads(d["features_json"])
    keys = ["spot", "strike", "time_left", "quote_move_60", "ret_1m", "ret_3m", "ret_5m",
            "ob_imbalance", "taker_flow", "vol_ratio", "shock_z", "p_model", "yes_bid", "yes_ask"]
    print("  " + "  ".join(f"{k}={round(float(f.get(k, 0)), 4)}" for k in keys))
    v = json.loads(d["verdict_json"])
    print(f"  verdict p_yes={v.get('p_yes')} p_raw={v.get('p_yes_raw')} edge={v.get('edge')} conf={v.get('confidence')}")
