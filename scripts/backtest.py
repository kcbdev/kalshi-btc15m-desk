"""Backtest the BTC15M desk against Kalshi's own history.

For every settled 15-minute window we fetch:
  - the market's 1-minute candlesticks (real yes_bid / yes_ask at each minute),
  - the BTC 1-minute candle history (for spot and realized vol),
then replay the live desk's rules minute by minute: fee-adjusted edge vs the ask
you could actually pay, entry limits, hard stop, model-flip exit and time stop.

Usage:  python scripts/backtest.py [--windows 300] [--refresh]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "backtest")
os.makedirs(CACHE, exist_ok=True)

BASE = "https://api.elections.kalshi.com/trade-api/v2"
CB = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
HDRS = {"User-Agent": "kalshi15m-paperdesk/0.1", "Accept": "application/json"}

SERIES = "KXBTC15M"
MIN_SIGMA_1M = 0.00040
FEE_RATE = 0.07

# mirrors kalshi15m/config.json
MIN_EDGE = 0.04
MAX_EDGE = 0.18
MIN_TIME_LEFT = 120
DECISION_START = 75
DECISION_EVERY = 60
STOP_FRAC = 0.55
EXIT_MIN_TIME = 90
RISK_PCT = 0.015
MAX_STAKE = 25.0


def _get(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HDRS, timeout=30)
            if r.status_code == 429:
                time.sleep(1.2 + i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(0.4 * (i + 1))
    return None


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_cache(name, refresh=False):
    p = os.path.join(CACHE, name)
    if not refresh and os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def save_cache(name, obj):
    p = os.path.join(CACHE, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def fetch_markets(limit=600, refresh=False):
    cache = load_cache(f"markets_{limit}.json", refresh)
    if cache:
        return cache
    out, cursor = [], ""
    while len(out) < limit:
        params = {"series_ticker": SERIES, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = _get(BASE + "/markets", params)
        if not d:
            break
        out.extend(d.get("markets", []))
        cursor = d.get("cursor") or ""
        if not cursor:
            break
        time.sleep(0.15)
    out = [m for m in out if m.get("result") in ("yes", "no") and m.get("floor_strike")]
    out.sort(key=lambda m: m["close_time"])
    save_cache(f"markets_{limit}.json", out)
    return out


def fetch_btc_candles(start_ts, end_ts, refresh=False):
    cache = load_cache("btc_1m.json", refresh) or {}
    lo, hi = int(start_ts // 60 * 60), int(end_ts // 60 * 60)
    missing = [m for m in range(lo, hi, 60) if str(m) not in cache]
    if missing:
        cur = lo
        while cur < hi:
            a, b = cur, min(cur + 299 * 60, hi)
            d = _get(
                CB,
                {
                    "granularity": 60,
                    "start": datetime.fromtimestamp(a, tz=timezone.utc).isoformat(),
                    "end": datetime.fromtimestamp(b, tz=timezone.utc).isoformat(),
                },
            )
            for row in d or []:
                cache[str(int(row[0]))] = float(row[4])
            cur = b
            time.sleep(0.1)
        save_cache("btc_1m.json", cache)
    return cache


def fetch_market_candles(market, refresh=False):
    """1-minute candles for one 15m market: bid/ask at each minute end."""
    ticker = market["ticker"]
    p = os.path.join(CACHE, "mkt", f"{ticker}.json")
    if not refresh and os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    open_ts = int(parse_ts(market["open_time"]))
    close_ts = int(parse_ts(market["close_time"])) + 120
    d = _get(
        f"{BASE}/series/{SERIES}/markets/{ticker}/candlesticks",
        {"start_ts": open_ts, "end_ts": close_ts, "period_interval": 1},
    )
    rows = {}
    for c in (d or {}).get("candlesticks", []):
        try:
            rows[str(int(c["end_period_ts"]))] = {
                "bid": float(c["yes_bid"]["close_dollars"]),
                "ask": float(c["yes_ask"]["close_dollars"]),
                "last": float(c["price"].get("close_dollars") or c["yes_bid"]["close_dollars"]),
            }
        except Exception:
            continue
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(rows, fh)
    return rows


def spot_at(btc, ts):
    """Close of the last COMPLETED 1-minute Coinbase candle at or before ts.

    Coinbase keys candles by bucket start, so the candle starting at m closes at
    m+60. Using the bucket that merely contains ts would leak up to 60s of future
    prices into the model (which produced a fake +195% backtest).
    """
    minute = ((int(ts) - 60) // 60) * 60
    for m in range(minute, minute - 900, -60):
        v = btc.get(str(m))
        if v:
            return v
    return None


def realized_sigma(btc, ts, lookback_min=30):
    minute = ((int(ts) - 60) // 60) * 60
    closes = [btc[str(m)] for m in range(minute - lookback_min * 60, minute + 1, 60) if str(m) in btc]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    rets = rets[-20:]
    if len(rets) < 3:
        return None
    return max(MIN_SIGMA_1M, statistics.stdev(rets))


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_value(btc, ts, strike, close_ts):
    spot = spot_at(btc, ts)
    sig = realized_sigma(btc, ts)
    if not spot or not sig:
        return None
    sigma_rem = sig * math.sqrt(15.0) * math.sqrt(max(1 / 60.0, (close_ts - ts) / 900.0))
    return norm_cdf(math.log(spot / strike) / sigma_rem)


def fee_pc(price):
    return FEE_RATE * price * (1.0 - price)


def simulate_window(window, btc, mkt, bankroll=1000.0):
    """Replay one window with the live desk's entry/exit rules."""
    open_ts = parse_ts(window["open_time"])
    close_ts = parse_ts(window["close_time"])
    strike = float(window["floor_strike"])
    outcome = 1.0 if window["result"] == "yes" else 0.0
    minutes = sorted(int(k) for k in mkt)
    if not minutes:
        return None

    position = None
    for t in range(int(open_ts) + DECISION_START, int(close_ts), DECISION_EVERY):
        avail = [m for m in minutes if m <= t]
        if not avail:
            continue
        c = mkt[str(avail[-1])]
        bid, ask = c["bid"], c["ask"]
        if ask <= 0.001 or ask >= 0.999:
            continue
        p = fair_value(btc, t, strike, close_ts)
        if p is None:
            continue
        time_left = close_ts - t

        if position:
            side = position["side"]
            mark = bid if side == "YES" else 1.0 - ask
            reason = None
            if mark <= position["entry"] * STOP_FRAC:
                reason = "hard stop"
            elif side == "YES" and p < 0.30 and time_left > EXIT_MIN_TIME:
                reason = "model flip"
            elif side == "NO" and p > 0.70 and time_left > EXIT_MIN_TIME:
                reason = "model flip"
            elif time_left <= EXIT_MIN_TIME and mark < position["entry"]:
                reason = "time stop"
            if reason:
                contracts, price, fee = position["contracts"], position["price"], position["fee"]
                pnl = contracts * (mark - price) - contracts * (fee + fee_pc(mark))
                return {
                    "window": window["ticker"], "side": side, "entry_ts": position["ts"],
                    "entry": price, "exit": mark, "contracts": contracts, "pnl": pnl,
                    "status": reason, "edge": position["edge"],
                    "time_left_entry": position["time_left"], "offset": position["offset"],
                    "y": outcome,
                }
            continue

        if time_left <= MIN_TIME_LEFT:
            continue
        fee_y = fee_pc(ask)
        no_ask = 1.0 - bid
        fee_n = fee_pc(no_ask)
        ey = p - ask - fee_y
        en = (1.0 - p) - no_ask - fee_n
        if MIN_EDGE <= ey <= MAX_EDGE and ey >= en:
            side, price, fee, edge = "YES", ask, fee_y, ey
        elif MIN_EDGE <= en <= MAX_EDGE:
            side, price, fee, edge = "NO", no_ask, fee_n, en
        else:
            continue
        budget = min(MAX_STAKE, bankroll * RISK_PCT)
        position = {
            "side": side, "ts": t, "price": price, "fee": fee,
            "contracts": max(1, int(budget // price)), "entry": price,
            "edge": edge, "time_left": time_left, "offset": t - int(open_ts),
        }

    if not position:
        return None
    contracts, price, fee = position["contracts"], position["price"], position["fee"]
    won = (position["side"] == "YES" and outcome == 1.0) or (position["side"] == "NO" and outcome == 0.0)
    pnl = contracts * ((1.0 if won else 0.0) - price) - contracts * fee
    return {
        "window": window["ticker"], "side": position["side"], "entry_ts": position["ts"],
        "entry": price, "exit": 1.0 if won else 0.0, "contracts": contracts, "pnl": pnl,
        "status": "settled", "edge": position["edge"], "time_left_entry": position["time_left"],
        "offset": position["offset"], "y": outcome,
    }


def run(args):
    markets = fetch_markets(args.windows, args.refresh)
    if not markets:
        print("no markets fetched")
        return 1
    start = min(parse_ts(m["open_time"]) for m in markets) - 3600
    end = max(parse_ts(m["close_time"]) for m in markets) + 60
    btc = fetch_btc_candles(start, end, args.refresh)
    print(f"markets: {len(markets)}  btc candles: {len(btc)}")

    trades = []
    for i, m in enumerate(markets):
        if i % 25 == 0:
            print(f"  replaying {i}/{len(markets)}", flush=True)
        mkt = fetch_market_candles(m, args.refresh)
        if not mkt:
            continue
        tr = simulate_window(m, btc, mkt)
        if tr:
            trades.append(tr)
        time.sleep(0.05)

    print(f"\nwindows: {len(markets)}  trades: {len(trades)}")
    if not trades:
        return 0
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    print(f"net pnl: {sum(pnls):+.2f}  win rate: {len(wins) / len(trades) * 100:.1f}%  "
          f"avg: {statistics.mean(pnls):+.3f}  best {max(pnls):+.2f}  worst {min(pnls):+.2f}")
    by_side = {}
    for t in trades:
        by_side.setdefault(t["side"], []).append(t["pnl"])
    for side, vals in by_side.items():
        print(f"  {side}: {len(vals)} trades  net {sum(vals):+.2f}  win {sum(1 for v in vals if v > 0) / len(vals) * 100:.0f}%")
    exits = {}
    for t in trades:
        exits[t["status"]] = exits.get(t["status"], 0) + 1
    print("  exits:", exits)
    save_cache("trades.json", trades)

    bal = 1000.0
    peak = bal
    mdd = 0.0
    for p in pnls:
        bal += p
        peak = max(peak, bal)
        mdd = max(mdd, peak - bal)
    print(f"  final bankroll {bal:.2f}  max drawdown {mdd:.2f}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=300)
    ap.add_argument("--refresh", action="store_true")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
