import json
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
con = sqlite3.connect(os.path.join(ROOT, "data", "kalshi15m.db"))
con.row_factory = sqlite3.Row

print("windows:", con.execute("SELECT COUNT(*) FROM windows").fetchone()[0])
print("settled:", con.execute("SELECT COUNT(*) FROM windows WHERE status='settled'").fetchone()[0])
print("snapshots:", con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
print("debates:", con.execute("SELECT COUNT(*) FROM debates").fetchone()[0])
print("trades:", con.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

print("\nnet-of-fee edge distribution at each decision (model vs ask):")
vals = []
for d in con.execute("SELECT features_json, verdict_json FROM debates"):
    f = json.loads(d["features_json"])
    try:
        v = json.loads(d["verdict_json"])
    except Exception:
        continue
    ea = float(f.get("edge_yes", 0))
    en = float(f.get("edge_no", 0))
    mid = float(f.get("yes_mid", 0.5))
    # fee-adjusted, using the model (not the blended committee p) as an approximation
    fy = 0.07 * float(f.get("yes_ask", 0.5)) * (1 - float(f.get("yes_ask", 0.5)))
    fn = 0.07 * (1 - float(f.get("yes_bid", 0.5))) * float(f.get("yes_bid", 0.5))
    vals.append((max(ea - fy, en - fn), mid, v.get("action"), v.get("reason", "")[:60]))

vals.sort(reverse=True)
for e, mid, act, reason in vals[:12]:
    print(f"  net_edge {e:+.3f}  mid {mid:.2f}  {act:5s}  {reason}")
print(f"\n evals: {len(vals)}  net_edge>=0.04: {sum(1 for v in vals if v[0] >= 0.04)}  "
      f">=0.03: {sum(1 for v in vals if v[0] >= 0.03)}  >=0.02: {sum(1 for v in vals if v[0] >= 0.02)}")
