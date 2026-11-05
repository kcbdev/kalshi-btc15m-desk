import json
import os
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
recs = json.load(open(os.path.join(ROOT, "data", "backtest", "records.json"), encoding="utf-8"))
print("records", len(recs))

print("\nrealised outcome by MARKET price bucket:")
edges = [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.01]
for lo, hi in zip(edges, edges[1:]):
    ch = [r for r in recs if lo <= r["price"] < hi]
    if ch:
        print(f"  price {lo:.1f}-{hi:.1f}  n={len(ch):3d}  avg price {statistics.mean(r['price'] for r in ch):.3f}"
              f"  realised {statistics.mean(r['y'] for r in ch):.3f}")

print("\nrealised outcome by MODEL p bucket:")
for lo, hi in zip(edges, edges[1:]):
    ch = [r for r in recs if lo <= r["p_model"] < hi]
    if ch:
        print(f"  p     {lo:.1f}-{hi:.1f}  n={len(ch):3d}  avg p {statistics.mean(r['p_model'] for r in ch):.3f}"
              f"  avg price {statistics.mean(r['price'] for r in ch):.3f}  realised {statistics.mean(r['y'] for r in ch):.3f}")

print("\nprofit decomposition for thr=0.04 trades:")
tot_yes = tot_no = 0.0
n_yes = n_no = 0
win_yes = win_no = 0
for r in recs:
    p, px = r["p_model"], r["price"]
    fy = 0.07 * px * (1 - px)
    fn = fy
    if p - px - fy >= 0.04:
        side, price, fee = "Y", px, fy
    elif (1 - p) - (1 - px) - fn >= 0.04:
        side, price, fee = "N", 1 - px, fn
    else:
        continue
    contracts = max(1, int(15 // price))
    win = (side == "Y" and r["y"] == 1.0) or (side == "N" and r["y"] == 0.0)
    pnl = contracts * ((1.0 if win else 0.0) - price) - contracts * fee
    if side == "Y":
        n_yes += 1
        tot_yes += pnl
        win_yes += 1 if win else 0
    else:
        n_no += 1
        tot_no += pnl
        win_no += 1 if win else 0
print(f"  YES trades {n_yes}  win {win_yes / max(1, n_yes) * 100:.1f}%  pnl {tot_yes:+.2f}")
print(f"  NO  trades {n_no}  win {win_no / max(1, n_no) * 100:.1f}%  pnl {tot_no:+.2f}")
