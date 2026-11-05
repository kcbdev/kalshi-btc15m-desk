"""Calibration study: was the model or the market better at pricing settled windows?

For every settled BTC15M window, take the intra-window snapshots and compare
  - the committee model's P(yes)      (p_model)
  - the market's implied P(yes)       (yes_mid)
against the actual outcome. Prints Brier scores and calibration bins.
"""
from __future__ import annotations

import json
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi15m.db")


def brier(rows, key):
    return sum((r[key] - r["y"]) ** 2 for r in rows) / len(rows) if rows else 0.0


def bins(rows, key, edges=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01)):
    out = []
    for lo, hi in zip(edges, edges[1:]):
        chunk = [r for r in rows if lo <= r[key] < hi]
        if not chunk:
            continue
        out.append((lo, hi, len(chunk), sum(r[key] for r in chunk) / len(chunk), sum(r["y"] for r in chunk) / len(chunk)))
    return out


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    settled = {w["window_id"]: w for w in con.execute("SELECT * FROM windows WHERE result IN ('yes','no')")}
    rows = []
    for wid, w in settled.items():
        for s in con.execute(
            "SELECT ts, time_left, p_model, yes_mid FROM snapshots WHERE window_id=? ORDER BY ts", (wid,)
        ):
            if s["p_model"] is None or s["yes_mid"] is None:
                continue
            if not (180 <= float(s["time_left"]) <= 720):
                continue
            rows.append({"y": 1.0 if w["result"] == "yes" else 0.0,
                         "p_model": float(s["p_model"]), "yes_mid": float(s["yes_mid"])})

    print(f"settled windows: {len(settled)}  usable snapshots: {len(rows)}")
    if len(rows) < 10:
        print("not enough data yet - let the desk run")
        return 0

    print(f"Brier model : {brier(rows, 'p_model'):.4f}")
    print(f"Brier market: {brier(rows, 'yes_mid'):.4f}")
    print()
    print("calibration (model):")
    print("  bin            n    predicted  realised")
    for lo, hi, n, p, a in bins(rows, "p_model"):
        print(f"  {lo:.1f}-{hi:.1f}   {n:4d}      {p:.3f}     {a:.3f}")
    print()
    print("calibration (market):")
    print("  bin            n    implied    realised")
    for lo, hi, n, p, a in bins(rows, "yes_mid"):
        print(f"  {lo:.1f}-{hi:.1f}   {n:4d}      {p:.3f}     {a:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
