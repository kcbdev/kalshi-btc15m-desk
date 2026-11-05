import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
con = sqlite3.connect(os.path.join(ROOT, "data", "kalshi15m.db"))
con.row_factory = sqlite3.Row

print("windows:", con.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"])
print("settled:", con.execute("SELECT COUNT(*) c FROM windows WHERE status='settled'").fetchone()["c"])
print("snapshots:", con.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"])
print("trades:", con.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"])
print("ledger:", con.execute("SELECT COUNT(*) c FROM ledger").fetchone()["c"])
print("balance:", con.execute("SELECT balance FROM ledger ORDER BY id DESC LIMIT 1").fetchone()["balance"])

row = con.execute("SELECT ts, spot FROM spot_ticks ORDER BY ts DESC LIMIT 1").fetchone()
if row:
    import datetime
    print("last spot tick:", datetime.datetime.utcfromtimestamp(row["ts"]).isoformat(), row["spot"])
row = con.execute("SELECT ts, window_id FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
if row:
    import datetime
    print("last snapshot:", datetime.datetime.utcfromtimestamp(row["ts"]).isoformat(), row["window_id"])

print("\nlast 8 trades:")
for t in con.execute("SELECT window_id, side, entry_price, contracts, status, result, pnl, source, exit_reason FROM trades ORDER BY entry_ts DESC LIMIT 8"):
    print(" ", dict(t))

print("\ntrade status counts:")
for r in con.execute("SELECT status, COUNT(*) c, ROUND(SUM(pnl),2) pnl FROM trades GROUP BY status"):
    print(" ", dict(r))

print("\nlast 5 windows:")
for w in con.execute("SELECT window_id, result, final_value, strike, status FROM windows ORDER BY open_ts DESC LIMIT 5"):
    print(" ", dict(w))
