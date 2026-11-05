"""Parameter sweep on top of the candle backtest.

Builds (and caches) a minute-by-minute timeline for every settled window - bid,
ask, model fair value, time left - then replays entry/exit rule variants over the
same data so configs can be compared without re-fetching anything.

Usage:
  python scripts/sweep.py --build            # refresh timelines from the cache
  python scripts/sweep.py                    # run the sweep
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest as bt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "backtest")
CACHE_TIMELINES = os.path.join(CACHE, "timelines_400.json")


def build_timelines(windows=400):
    markets = bt.fetch_markets(windows)
    start = min(bt.parse_ts(m["open_time"]) for m in markets) - 3600
    end = max(bt.parse_ts(m["close_time"]) for m in markets) + 60
    btc = bt.fetch_btc_candles(start, end)
    out = []
    for i, m in enumerate(markets):
        if i % 50 == 0:
            print(f"  timeline {i}/{len(markets)}", flush=True)
        mkt = bt.fetch_market_candles(m)
        if not mkt:
            continue
        open_ts = int(bt.parse_ts(m["open_time"]))
        close_ts = int(bt.parse_ts(m["close_time"]))
        strike = float(m["floor_strike"])
        minutes = sorted(int(k) for k in mkt)
        steps = []
        for t in range(open_ts + 60, close_ts - 30, 60):
            avail = [x for x in minutes if x <= t]
            if not avail:
                continue
            c = mkt[str(avail[-1])]
            p = bt.fair_value(btc, t, strike, close_ts)
            if p is None:
                continue
            steps.append({
                "t": t, "bid": c["bid"], "ask": c["ask"], "p": round(p, 4),
                "tl": close_ts - t,
            })
        if len(steps) < 6:
            continue
        out.append({
            "w": m["ticker"], "open": open_ts, "close": close_ts, "strike": strike,
            "y": 1.0 if m["result"] == "yes" else 0.0, "steps": steps,
        })
    json.dump(out, open(CACHE_TIMELINES, "w", encoding="utf-8"))
    print(f"timelines: {len(out)}")
    return out


def simulate(w, prm, bankroll_start=1000.0):
    steps = w["steps"]
    pos = None
    for s in steps:
        t, bid, ask, p, tl = s["t"], s["bid"], s["ask"], s["p"], s["tl"]
        if ask <= 0.001 or ask >= 0.999:
            continue
        if pos:
            mark = bid if pos["side"] == "YES" else 1.0 - ask
            reason = None
            if prm["stop"] and mark <= pos["entry"] * prm["stop"]:
                reason = "stop"
            elif prm["flip"] and pos["side"] == "YES" and p < prm["flip"] and tl > 90:
                reason = "flip"
            elif prm["flip"] and pos["side"] == "NO" and p > 1 - prm["flip"] and tl > 90:
                reason = "flip"
            elif prm["timestop"] and tl <= 90 and mark < pos["entry"]:
                reason = "time"
            if prm["tp"] and mark >= prm["tp"]:
                reason = "tp"
            if reason:
                pnl = pos["qty"] * (mark - pos["price"]) - pos["qty"] * (pos["fee"] + bt.fee_pc(mark))
                return {"pnl": pnl, "status": reason, "side": pos["side"], "entry_tl": pos["tl"]}
            continue

        if tl <= prm["min_tl"] or t - w["open"] < prm["start"]:
            continue
        if not (prm["band"][0] <= (bid + ask) / 2 <= prm["band"][1]):
            continue
        fee_y = bt.fee_pc(ask)
        no_ask = 1.0 - bid
        fee_n = bt.fee_pc(no_ask)
        ey = p - ask - fee_y
        en = (1.0 - p) - no_ask - fee_n
        if prm["min_edge"] <= ey <= prm["max_edge"] and ey >= en:
            side, price, fee, edge = "YES", ask, fee_y, ey
        elif prm["min_edge"] <= en <= prm["max_edge"]:
            side, price, fee, edge = "NO", no_ask, fee_n, en
        else:
            continue
        qty = max(1, int(min(prm["max_stake"], bankroll_start * prm["risk"]) // price))
        pos = {"side": side, "price": price, "fee": fee, "qty": qty, "entry": price, "tl": tl}
    if not pos:
        return None
    won = (pos["side"] == "YES" and w["y"] == 1.0) or (pos["side"] == "NO" and w["y"] == 0.0)
    pnl = pos["qty"] * ((1.0 if won else 0.0) - pos["price"]) - pos["qty"] * pos["fee"]
    return {"pnl": pnl, "status": "settled", "side": pos["side"], "entry_tl": pos["tl"]}


def evaluate(timelines, prm):
    res = [simulate(w, prm) for w in timelines]
    res = [r for r in res if r]
    if not res:
        return None
    pnls = [r["pnl"] for r in res]
    bal = peak = 1000.0
    mdd = 0.0
    for p in pnls:
        bal += p
        peak = max(peak, bal)
        mdd = max(mdd, peak - bal)
    return {
        "n": len(res),
        "net": sum(pnls),
        "win": sum(1 for p in pnls if p > 0) / len(pnls),
        "avg": statistics.mean(pnls),
        "mdd": mdd,
        "final": bal,
    }


BASE = {
    "min_edge": 0.04, "max_edge": 0.18, "band": (0.10, 0.90), "start": 75,
    "min_tl": 120, "stop": 0.55, "flip": 0.30, "timestop": True, "tp": 0.0,
    "risk": 0.015, "max_stake": 25.0,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    args = ap.parse_args()

    if args.build or not os.path.exists(CACHE_TIMELINES):
        timelines = build_timelines(400)
    else:
        timelines = json.load(open(CACHE_TIMELINES, encoding="utf-8"))
    print(f"timelines: {len(timelines)}")

    rows = []
    for min_edge, stop, flip, band in itertools.product(
        (0.03, 0.04, 0.06, 0.08, 0.10),
        (0.55, 0.0),
        (0.30, 0.0),
        ((0.10, 0.90), (0.20, 0.80)),
    ):
        prm = dict(BASE)
        prm.update({"min_edge": min_edge, "stop": stop, "flip": flip, "band": band})
        m = evaluate(timelines, prm)
        if m:
            rows.append((min_edge, stop, flip, band, m))
    rows.sort(key=lambda r: -r[4]["net"])
    print(f"\n{'edge':>5s} {'stop':>5s} {'flip':>5s} {'band':>10s} {'n':>4s} {'net':>9s} {'win':>6s} {'mdd':>8s}")
    for e, s, f, b, m in rows[:15]:
        print(f"{e:5.2f} {s:5.2f} {f:5.2f} {str(b):>10s} {m['n']:4d} {m['net']:+9.2f} "
              f"{m['win'] * 100:5.1f}% {m['mdd']:8.2f}")

    # hold-to-settlement baseline for reference
    prm = dict(BASE)
    prm.update({"stop": 0.0, "flip": 0.0, "timestop": False})
    m = evaluate(timelines, prm)
    print(f"\nhold-to-settlement: n={m['n']} net {m['net']:+.2f} win {m['win'] * 100:.1f}% mdd {m['mdd']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
