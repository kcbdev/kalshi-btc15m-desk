import json
import time

from kalshi15m.committee import run_committee

cfg = json.load(open("kalshi15m/config.json"))
now = time.time()
base = {
    "window_id": "TEST", "ts": now, "market_ticker": "T",
    "spot": 80400.0, "strike": 80389.19, "time_left": 600.0, "elapsed": 300.0,
    "quote_move_60": 0.01,
    "ret_1m": 0.0002, "ret_3m": 0.0003, "ret_5m": 0.0004, "ret_10m": 0.0005,
    "sigma_1m": 0.0005, "sigma_15m": 0.0019, "sigma_rem": 0.0016,
    "sigma_fast": 0.0006, "sigma_slow": 0.0004, "vol_ratio": 1.5, "shock_z": 0.4, "n_ret": 12,
    "dist": 0.00013, "dist_pct": 0.00013, "dist_sigma": 0.08,
    "p_model": 0.53, "yes_bid": 0.44, "yes_ask": 0.45, "yes_mid": 0.445, "spread": 0.01,
    "no_ask": 0.56, "volume": 100000, "oi": 50000, "volume_rate": 500.0,
    "ob_imbalance": 0.1, "yes_depth": 500, "no_depth": 400, "taker_flow": 0.2,
    "mean_10m": 80390.0, "z_meanrev": 0.2, "p_implied": 0.445,
    "edge_yes": 0.0, "edge_no": 0.0,
}
scenarios = [
    ("coin flip, no edge", {}),
    ("model sees 62c", {"p_model": 0.62}),
    ("cheap YES longshot", {"p_model": 0.115, "yes_bid": 0.095, "yes_ask": 0.10, "yes_mid": 0.0975,
                            "dist": -0.0016, "dist_sigma": -1.2, "spot": 80260.0}),
    ("rich market vs model", {"p_model": 0.80, "yes_bid": 0.70, "yes_ask": 0.71, "yes_mid": 0.705}),
    ("collapsing quote", {"quote_move_60": -0.09}),
    ("vol shock", {"shock_z": 4.0, "ret_1m": 0.002}),
    ("thin book", {"yes_depth": 3, "no_depth": 2}),
]
for label, patch in scenarios:
    f = dict(base)
    f.update(patch)
    out = run_committee(f, {"cfg": cfg, "bankroll": 1000.0, "day_pnl": 0.0, "open_position": None}, cfg)
    v = out["verdict"]
    print(f"{label:22s} {v['action']:5s} {str(v['side']):5s} p={v['p_yes']:.3f} edge={v['edge']:+.3f} k={v['contracts']:3d} | {v['reason'][:95]}")
