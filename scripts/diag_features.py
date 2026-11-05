import json
import time

from kalshi15m.api import Kalshi
from kalshi15m.db import DB
from kalshi15m.features import _realized_sigma, _sigma_fast, build_features

db = DB()
api = Kalshi()
now = time.time()
row = db.q1("SELECT * FROM windows WHERE close_ts > ? ORDER BY open_ts LIMIT 1", (now,))
w = dict(row)
book = api.orderbook(w["market_ticker"])
market = api.market(w["market_ticker"])
trades = []
f = build_features(db, w, market, book, trades, now)

ticks = [(float(r["ts"]), float(r["spot"])) for r in db.recent_spots(now - 31 * 60)]
print("ticks in buffer:", len(ticks))
print("sigma_slow (per min):", _realized_sigma(ticks)[0])
print("sigma_fast (per min):", _sigma_fast(ticks))
for k in ("spot", "strike", "time_left", "sigma_1m", "sigma_15m", "sigma_rem", "dist", "dist_sigma", "p_model", "vol_ratio", "shock_z"):
    print(f"{k:12s}", f.get(k))
print("implied z from dist/sigma_rem:", f["dist"] / f["sigma_rem"])
print("implied sigma_15m from dist_sigma:", f["dist"] / f["dist_sigma"] / (f["time_left"] / 900) ** 0.5)
