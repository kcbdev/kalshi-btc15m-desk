"""Estimate the basis between our spot composite and Kalshi's settlement reference.

Kalshi settles each 15m window on the 60-second average of CF Benchmarks BRTI
before the close. That value is published as `final_value`. Comparing it with
our own 60-second spot average just before the close gives a direct estimate of
any systematic basis (exchange mix, USDT vs USD, feed latency).
"""
from __future__ import annotations

import os
import sqlite3
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi15m.db")

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

rows = list(con.execute("SELECT * FROM windows WHERE status='settled' ORDER BY open_ts"))
print(f"{'window':28s} {'strike':>10s} {'final':>10s} {'our60s':>10s} {'point':>10s} {'basis':>8s} {'n':>3s}")
bases = []
for w in rows:
    close_ts = float(w["close_ts"])
    ticks = [float(r["spot"]) for r in con.execute(
        "SELECT spot FROM spot_ticks WHERE ts BETWEEN ? AND ? ORDER BY ts", (close_ts - 60, close_ts))]
    if not ticks or not w["final_value"]:
        continue
    our_avg = statistics.mean(ticks)
    point = ticks[-1]
    basis = our_avg - float(w["final_value"])
    bases.append(basis)
    print(f"{w['window_id']:28s} {float(w['strike']):10.2f} {float(w['final_value']):10.2f} "
          f"{our_avg:10.2f} {point:10.2f} {basis:+8.2f} {len(ticks):3d}")

if bases:
    print(f"\nbasis mean {statistics.mean(bases):+.2f}  median {statistics.median(bases):+.2f}  "
          f"stdev {statistics.pstdev(bases):.2f}  n={len(bases)}")
