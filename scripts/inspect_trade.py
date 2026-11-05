import sqlite3

c = sqlite3.connect(r"data/kalshi15m.db")
c.row_factory = sqlite3.Row
wid = "KXBTC15M-26NOV041800"
rows = c.execute(
    "SELECT ts, spot, yes_bid, yes_ask, p_model, time_left, dist_sigma, spread FROM snapshots "
    "WHERE window_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
    (wid, 1793832630, 1793832950),
).fetchall()
for r in rows:
    print(round(r["ts"]), r["spot"], r["yes_bid"], r["yes_ask"], round(r["p_model"], 3),
          round(r["time_left"]), round(r["dist_sigma"], 2), round(r["spread"], 3))
print("--- trades")
for t in c.execute("SELECT * FROM trades"):
    d = dict(t)
    print({k: d[k] for k in ("id", "side", "entry_price", "contracts", "status", "result", "pnl", "exit_reason", "source")})
print("--- ledger")
for l in c.execute("SELECT * FROM ledger"):
    print(dict(l))
