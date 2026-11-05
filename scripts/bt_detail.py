"""Detailed P&L decomposition for the chosen backtest config."""
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
timelines = json.load(open(os.path.join(ROOT, "data", "backtest", "timelines_400.json"), encoding="utf-8"))

base = dict(sweep.BASE)
base.update({"min_edge": 0.04, "stop": 0.0, "flip": 0.0, "timestop": False, "band": (0.15, 0.85)})
res = []
for w in timelines:
    r = sweep.simulate(w, base)
    if r:
        res.append({**r, "w": w["w"]})

pnls = sorted(r["pnl"] for r in res)
print(f"n={len(res)}  net={sum(pnls):+.2f}  avg={statistics.mean(pnls):+.3f}  median={statistics.median(pnls):+.3f}")
print(f"wins {sum(1 for p in pnls if p > 0)}  losses {sum(1 for p in pnls if p <= 0)}")
print(f"top5 wins: {[round(x, 2) for x in pnls[-5:]]}  sum {sum(pnls[-5:]):+.2f}")
print(f"net ex-top5: {sum(pnls[:-5]):+.2f}")
print(f"worst5: {[round(x, 2) for x in pnls[:5]]}")
by = {}
for r in res:
    by.setdefault(r["side"], []).append(r["pnl"])
for s, v in by.items():
    print(f"  {s}: n={len(v)} net {sum(v):+.2f} win {sum(1 for x in v if x > 0) / len(v) * 100:.1f}%")
print("\nby entry time-left bucket:")
for lo, hi in ((700, 900), (500, 700), (300, 500), (120, 300)):
    ch = [r["pnl"] for r in res if lo <= r["entry_tl"] < hi]
    if ch:
        print(f"  tl {lo}-{hi}: n={len(ch)} net {sum(ch):+.2f} win {sum(1 for x in ch if x > 0) / len(ch) * 100:.1f}%")
