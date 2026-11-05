"""BTC spot feed: robust consensus of Coinbase / Kraken / Binance USD(S) last prices.

Binance quotes BTCUSDT and can carry a persistent premium/discount versus the
USD venues, so a plain median silently shifts by tens of dollars whenever one
venue drops out. That shift used to move the model's fair value by 0.5 sigma and
whipped the desk in and out of positions within seconds. We now cluster the
quotes and only average venues that agree, preferring the USD venues.
"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

SOURCES = {
    "coinbase": "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
    "kraken": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
    "binance": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
}
PRIORITY = ["coinbase", "kraken", "binance"]
AGREE_USD = 20.0  # venues further apart than this are treated as different markets

CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


class SpotFeed:
    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi15m-paperdesk/0.1"})
        self.last_quotes: dict[str, float] = {}

    def _coinbase(self):
        r = self.session.get(SOURCES["coinbase"], timeout=self.timeout)
        r.raise_for_status()
        return float(r.json()["price"])

    def _kraken(self):
        r = self.session.get(SOURCES["kraken"], timeout=self.timeout)
        r.raise_for_status()
        res = r.json()["result"]
        key = next(iter(res))
        return float(res[key]["c"][0])

    def _binance(self):
        r = self.session.get(SOURCES["binance"], timeout=self.timeout)
        r.raise_for_status()
        return float(r.json()["price"])

    def fetch(self):
        """Return (price, per_source_dict) or (None, {}) if every source failed."""
        quotes: dict[str, float] = {}
        fns = {"coinbase": self._coinbase, "kraken": self._kraken, "binance": self._binance}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(fn): name for name, fn in fns.items()}
            try:
                for fut in as_completed(futures, timeout=self.timeout + 1.0):
                    name = futures[fut]
                    try:
                        quotes[name] = fut.result()
                    except Exception:
                        continue
            except Exception:
                pass
        if not quotes:
            return None, {}
        self.last_quotes = quotes

        # largest cluster of venues that agree with each other
        best: list[str] = []
        for name, v in quotes.items():
            cluster = [n for n, x in quotes.items() if abs(x - v) <= AGREE_USD]
            if len(cluster) > len(best):
                best = cluster
        if len(best) >= 2:
            return float(statistics.median(quotes[n] for n in best)), quotes
        # venues disagree: trust the highest-quality single source that answered
        for name in PRIORITY:
            if name in quotes:
                return float(quotes[name]), quotes
        return float(statistics.median(quotes.values())), quotes


def backfill_coinbase(db, minutes: int = 40) -> int:
    """Seed spot history with 1-minute Coinbase candles so features work on a cold start."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes)
    try:
        r = requests.get(
            CANDLES,
            params={"granularity": 60, "start": start.isoformat(), "end": now.isoformat()},
            headers={"User-Agent": "kalshi15m-paperdesk/0.1"},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception:
        return 0

    existing = {int(float(row["ts"]) // 60) for row in db.q("SELECT ts FROM spot_ticks WHERE ts>=?", (start.timestamp(),))}
    added = 0
    clock = time.time()
    for candle in rows:
        try:
            ts = float(candle[0])
            close = float(candle[4])
        except (TypeError, ValueError, IndexError):
            continue
        minute = int(ts // 60)
        if minute in existing or close <= 0:
            continue
        if ts + 59.0 >= clock:
            # skip the still-forming candle: a future timestamp would poison features
            continue
        db.add_spot(ts + 59.0, close, "coinbase_candle")
        existing.add(minute)
        added += 1
    return added


if __name__ == "__main__":
    f = SpotFeed()
    for _ in range(3):
        print(time.strftime("%H:%M:%S"), f.fetch())
        time.sleep(2)
