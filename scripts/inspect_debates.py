import json
import sqlite3

c = sqlite3.connect(r"data/kalshi15m.db")
c.row_factory = sqlite3.Row
for d in c.execute("SELECT * FROM debates WHERE window_id='KXBTC15M-26NOV041800' ORDER BY ts"):
    f = json.loads(d["features_json"])
    v = json.loads(d["verdict_json"])
    print("ts", round(float(d["ts"])), "spot", f.get("spot"), "yb", f.get("yes_bid"), "ya", f.get("yes_ask"),
          "p_model", round(float(f.get("p_model") or 0), 3), "time_left", round(float(f.get("time_left") or 0)))
    print("   verdict", v.get("action"), v.get("side"), v.get("price"), v.get("edge"), v.get("reason"))
