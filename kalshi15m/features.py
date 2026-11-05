"""Feature engineering for the BTC15M committee.

Every number here is derived only from data that existed at decision time:
Kalshi market state, the public order book, recent prints and BTC spot ticks.
"""
from __future__ import annotations

import math
import time

from .api import book_depth
from .util import clamp, mean, norm_cdf, parse_kalshi_ts, safe_float, stdev

MIN_SIGMA_1M = 0.00040  # 0.04% per minute floor (~0.15% per 15m)


def _resample_1m(ticks, max_minutes: int = 30):
    """ticks: [(ts, price)] sorted. Returns [(minute_index, close)]."""
    buckets: dict[int, float] = {}
    for ts, px in ticks:
        if px is None or px <= 0:
            continue
        m = int(ts // 60)
        buckets[m] = float(px)  # last print in the minute wins
    if not buckets:
        return []
    mins = sorted(buckets)[-max_minutes:]
    return [(m, buckets[m]) for m in mins]


def _realized_sigma(ticks):
    series = _resample_1m(ticks, 30)
    closes = [c for _, c in series]
    if len(closes) < 4:
        return None, 0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    rets = rets[-20:]
    if len(rets) < 3:
        return None, len(rets)
    return max(MIN_SIGMA_1M, stdev(rets)), len(rets)


def _sigma_fast(ticks, lookback_s=600, step_s=15):
    """Fast per-minute vol from 15s buckets over the last 10 minutes (reacts to bursts)."""
    if not ticks:
        return None
    cut = ticks[-1][0] - lookback_s
    buckets: dict[int, float] = {}
    for ts, px in ticks:
        if ts >= cut and px and px > 0:
            buckets[int(ts // step_s)] = float(px)
    keys = sorted(buckets)
    if len(keys) < 10:
        return None
    closes = [buckets[k] for k in keys]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 8:
        return None
    return stdev(rets) * math.sqrt(60.0 / step_s)


def _ago(ticks, seconds):
    cut = ticks[-1][0] - seconds
    val = None
    for ts, px in ticks:
        if ts <= cut:
            val = px
        else:
            break
    return val


def build_features(db, window: dict, market: dict, book: dict, trades: list, now: float) -> dict:
    window_id = window["window_id"]
    close_ts = float(window["close_ts"])
    open_ts = float(window["open_ts"])
    strike = float(window["strike"] or 0.0)
    time_left = max(0.0, close_ts - now)
    elapsed = max(0.0, now - open_ts)

    ticks_rows = db.recent_spots(now - 31 * 60)
    ticks = [(float(r["ts"]), float(r["spot"])) for r in ticks_rows]
    if len(ticks) < 2:
        # Cold start or feed outage: skip this cycle instead of crashing the loop.
        return {}
    spot = ticks[-1][1]
    if spot <= 0:
        return {}

    # The fair-value model runs on a short median (last ~3 ticks, 10-15s) instead of
    # the raw last print: a single noisy quote should not swing P(yes) by 0.3.
    model_ticks = [px for _, px in ticks[-3:]] or [spot]
    spot_model = sorted(model_ticks)[len(model_ticks) // 2]

    rets = {}
    for label, secs in (("1m", 60), ("3m", 180), ("5m", 300), ("10m", 600)):
        prev = _ago(ticks, secs)
        rets[label] = math.log(spot / prev) if prev and prev > 0 else 0.0

    sigma_slow, n_ret = _realized_sigma(ticks)
    sigma_fast = _sigma_fast(ticks)
    if sigma_slow and sigma_fast:
        sigma_1m = 0.55 * sigma_fast + 0.45 * sigma_slow
    else:
        sigma_1m = sigma_fast or sigma_slow or MIN_SIGMA_1M
    sigma_1m = max(MIN_SIGMA_1M, sigma_1m)
    vol_ratio = (sigma_fast / sigma_slow) if (sigma_fast and sigma_slow) else 1.0
    sigma_15m = sigma_1m * math.sqrt(15.0)
    horizon_frac = max(1.0 / 60.0, time_left / 900.0)
    sigma_rem = max(1e-6, sigma_15m * math.sqrt(horizon_frac))

    dist = math.log(spot_model / strike) if strike > 0 else 0.0
    # Very small momentum drift. It used to be 0.15, which let extrapolated
    # 5-minute momentum supply most of the "edge" - momentum is already its own
    # desk, so double counting it produced phantom edges against extreme prices.
    drift_w = 0.05
    mu_1m = drift_w * rets.get("5m", 0.0) / 5.0
    mu_rem = mu_1m * (time_left / 60.0)
    z = (dist + mu_rem) / sigma_rem
    p_model = norm_cdf(z)
    p_model_pure = norm_cdf(dist / sigma_rem)
    shock_z = abs(rets["1m"]) / sigma_1m if sigma_1m > 0 else 0.0

    # --- market microstructure ---
    yes_bid = safe_float(market.get("yes_bid_dollars"), 0.0)
    yes_ask = safe_float(market.get("yes_ask_dollars"), 0.0)
    if yes_ask <= 0 and yes_bid > 0:
        yes_ask = min(0.99, yes_bid + 0.01)
    if yes_bid <= 0 and yes_ask > 0:
        yes_bid = max(0.01, yes_ask - 0.01)
    mid = (yes_bid + yes_ask) / 2.0 if (yes_bid > 0 and yes_ask > 0) else 0.5
    spread = max(0.0, yes_ask - yes_bid)

    yes_lv, no_lv = book_depth(book, 5)
    bid_sz = sum(s for _, s in yes_lv)
    ask_sz = sum(s for _, s in no_lv)
    denom = bid_sz + ask_sz
    ob_imbalance = (bid_sz - ask_sz) / denom if denom > 0 else 0.0

    # taker flow over the last 2 minutes
    flow_cut = now - 120
    yes_flow = no_flow = 0.0
    for t in trades or []:
        ts = parse_kalshi_ts(t.get("created_time"))
        if ts is None or ts < flow_cut:
            continue
        c = safe_float(t.get("count_fp"))
        side = (t.get("taker_side") or "").lower()
        if side == "yes":
            yes_flow += c
        elif side == "no":
            no_flow += c
    flow_total = yes_flow + no_flow
    taker_flow = (yes_flow - no_flow) / flow_total if flow_total > 0 else 0.0

    volume = safe_float(market.get("volume_fp"))
    oi = safe_float(market.get("open_interest_fp"))

    # volume acceleration versus ~60s ago
    volume_rate = 0.0
    prev = db.q1(
        "SELECT ts, volume FROM snapshots WHERE window_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (window_id, now - 55),
    )
    if prev and prev["volume"] is not None:
        dt = max(1.0, now - float(prev["ts"]))
        volume_rate = max(0.0, volume - float(prev["volume"])) / dt

    series = _resample_1m(ticks, 15)
    closes = [c for _, c in series]
    mean_10m = mean(closes[-10:]) if closes else spot
    dev_from_mean = math.log(spot / mean_10m) if mean_10m > 0 else 0.0
    z_meanrev = dev_from_mean / max(1e-9, sigma_1m * math.sqrt(10.0))

    # depth available to fill on each side (contracts at best level)
    yes_ask_depth = 0.0
    for p, s in no_lv:
        if abs((1.0 - p) - yes_ask) < 0.011:
            yes_ask_depth = s
            break
    no_ask_price = 1.0 - yes_bid
    no_ask_depth = 0.0
    for p, s in yes_lv:
        if abs(p - (1.0 - no_ask_price)) < 0.011:
            no_ask_depth = s
            break

    # how far the market quote itself has moved in the last ~60s
    prev_q = db.q1(
        "SELECT yes_mid FROM snapshots WHERE window_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (window_id, now - 55),
    )
    quote_move_60 = (mid - float(prev_q["yes_mid"])) if prev_q and prev_q["yes_mid"] is not None else 0.0

    return {
        "window_id": window_id,
        "ts": now,
        "market_ticker": market.get("ticker"),
        "spot": round(spot, 2),
        "spot_model": round(spot_model, 2),
        "strike": strike,
        "time_left": time_left,
        "elapsed": elapsed,
        "quote_move_60": quote_move_60,
        "ret_1m": rets["1m"],
        "ret_3m": rets["3m"],
        "ret_5m": rets["5m"],
        "ret_10m": rets["10m"],
        "sigma_1m": sigma_1m,
        "sigma_15m": sigma_15m,
        "sigma_rem": sigma_rem,
        "sigma_fast": sigma_fast or 0.0,
        "sigma_slow": sigma_slow or 0.0,
        "vol_ratio": vol_ratio,
        "shock_z": shock_z,
        "n_ret": n_ret,
        "dist": dist,
        "dist_pct": (spot / strike - 1.0) if strike > 0 else 0.0,
        "dist_sigma": z,
        "dist_sigma_pure": dist / sigma_rem,
        "p_model": p_model,
        "p_model_pure": p_model_pure,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_mid": mid,
        "spread": spread,
        "no_ask": max(0.0, 1.0 - yes_bid),
        "volume": volume,
        "oi": oi,
        "volume_rate": volume_rate,
        "ob_imbalance": ob_imbalance,
        "yes_depth": yes_ask_depth,
        "no_depth": no_ask_depth,
        "taker_flow": taker_flow,
        "mean_10m": mean_10m,
        "z_meanrev": z_meanrev,
        "p_implied": mid,
        "edge_yes": p_model - yes_ask,
        "edge_no": (1.0 - p_model) - (1.0 - yes_bid),
    }


def snapshot_row(f: dict) -> dict:
    return {
        "ts": f["ts"],
        "window_id": f["window_id"],
        "spot": f["spot"],
        "yes_bid": f["yes_bid"],
        "yes_ask": f["yes_ask"],
        "yes_mid": f["yes_mid"],
        "spread": f["spread"],
        "last": f.get("last", f["yes_mid"]),
        "volume": f["volume"],
        "oi": f["oi"],
        "ob_imbalance": f["ob_imbalance"],
        "taker_flow": f["taker_flow"],
        "time_left": f["time_left"],
        "p_model": f["p_model"],
        "dist_sigma": f["dist_sigma"],
    }
