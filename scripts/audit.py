"""Independent audit of the paper-trading journal.

Re-derives every number from raw records and checks it against what the engine
wrote. Exits non-zero if any check fails.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "kalshi15m.db")
CFG = json.load(open(os.path.join(ROOT, "kalshi15m", "config.json"), encoding="utf-8"))


def fee(contracts, price, rate=0.07):
    return math.ceil(rate * contracts * price * (1.0 - price) * 100.0 - 1e-9) / 100.0


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    failures = []
    checks = 0

    # 1. ledger arithmetic
    ledger = list(con.execute("SELECT * FROM ledger ORDER BY id"))
    running = 0.0
    for row in ledger:
        running += float(row["amount"])
        checks += 1
        if abs(running - float(row["balance"])) > 0.005:
            failures.append(f"ledger row {row['id']}: running {running:.4f} != stored {row['balance']:.4f}")
    balance = running
    start = float(CFG["bankroll_start"])
    checks += 1
    if abs(balance - (start + sum(float(r["amount"]) for r in ledger[1:]))) > 0.005:
        failures.append("bankroll != start + sum of non-genesis ledger entries")

    # 2. per-trade arithmetic
    trades = list(con.execute("SELECT * FROM trades ORDER BY entry_ts"))
    for t in trades:
        c, p = int(t["contracts"]), float(t["entry_price"])
        checks += 1
        if abs(float(t["cost"]) - round(c * p, 2)) > 0.011:
            failures.append(f"{t['id']}: cost {t['cost']} != contracts*price {c * p:.2f}")
        checks += 1
        if abs(float(t["entry_fee"]) - fee(c, p, CFG["fee_rate"])) > 0.011:
            failures.append(f"{t['id']}: entry_fee {t['entry_fee']} != schedule {fee(c, p, CFG['fee_rate'])}")
        if t["status"] == "settled":
            won = (t["side"] == "YES" and t["result"] == "yes") or (t["side"] == "NO" and t["result"] == "no")
            payout = c * 1.0 if won else 0.0
            pnl = payout - c * p - float(t["entry_fee"])
            checks += 1
            if abs(float(t["payout"]) - payout) > 0.011:
                failures.append(f"{t['id']}: payout {t['payout']} != {payout}")
            checks += 1
            if abs(float(t["pnl"]) - pnl) > 0.011:
                failures.append(f"{t['id']}: pnl {t['pnl']} != recomputed {pnl:.2f}")
        elif t["status"] == "closed":
            proceeds = c * float(t["exit_price"])
            pnl = proceeds - float(t["exit_fee"]) - c * p - float(t["entry_fee"])
            checks += 1
            if abs(float(t["pnl"]) - pnl) > 0.011:
                failures.append(f"{t['id']}: exit pnl {t['pnl']} != recomputed {pnl:.2f}")

    # 3. settlement rule: YES iff final reference >= strike
    for w in con.execute("SELECT * FROM windows WHERE status='settled'"):
        if w["result"] not in ("yes", "no") or w["final_value"] is None or w["strike"] is None:
            continue
        expected = "yes" if float(w["final_value"]) >= float(w["strike"]) else "no"
        checks += 1
        if expected != w["result"]:
            failures.append(
                f"{w['window_id']}: Kalshi says {w['result']} but final {w['final_value']} vs strike {w['strike']} implies {expected}"
            )

    # 4. every trade belongs to a window, and settled trades only on settled windows
    wins = {w["window_id"]: w for w in con.execute("SELECT * FROM windows")}
    for t in trades:
        checks += 1
        if t["window_id"] not in wins:
            failures.append(f"{t['id']}: unknown window {t['window_id']}")
        if t["status"] == "settled" and wins[t["window_id"]]["result"] not in ("yes", "no"):
            failures.append(f"{t['id']}: settled but window has no result")

    # 5. no overlapping open positions (one-per-window rule)
    open_trades = [t for t in trades if t["status"] == "open"]
    checks += 1
    if len(open_trades) > int(CFG["max_open_positions"]):
        failures.append(f"{len(open_trades)} open positions exceeds max_open_positions")

    # 6. journal files exist and match the DB counts
    jmd = os.path.join(ROOT, "journal", "JOURNAL.md")
    checks += 1
    if not os.path.exists(jmd):
        failures.append("journal/JOURNAL.md missing")
    else:
        text = open(jmd, encoding="utf-8").read()
        if str(len(trades)) and trades and f"over {len(trades)} trades" not in text:
            failures.append("JOURNAL.md trade count does not match DB")

    # summary
    settled = [t for t in trades if t["status"] == "settled"]
    closed_early = [t for t in trades if t["status"] == "closed"]
    net = sum(float(t["pnl"] or 0) for t in trades if t["pnl"] is not None)
    print(f"trades: {len(trades)}  settled: {len(settled)}  early-exit: {len(closed_early)}  open: {len(open_trades)}")
    print(f"bankroll: {balance:.2f} (start {start:.2f}, net {balance - start:+.2f}, ledger net {net:+.2f})")
    print(f"checks run: {checks}  failures: {len(failures)}")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
