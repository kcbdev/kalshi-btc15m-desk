"""Minimal Kalshi public REST client (market data only - no auth required)."""
from __future__ import annotations

import time

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiError(RuntimeError):
    pass


class Kalshi:
    def __init__(self, base: str = BASE, timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "kalshi15m-paperdesk/0.1 (research; contact: local)",
                "Accept": "application/json",
            }
        )

    def _get(self, path: str, params: dict | None = None, retries: int = 3):
        last_err = None
        for attempt in range(retries):
            try:
                r = self.session.get(self.base + path, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(1.0 + attempt)
                    last_err = KalshiError("429 rate limited")
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(0.5 * (attempt + 1))
        raise KalshiError(f"GET {path} failed: {last_err}")

    # ---------- endpoints ----------
    def exchange_status(self):
        return self._get("/exchange/status")

    def open_btc15m_markets(self, series_ticker: str = "KXBTC15M"):
        data = self._get("/markets", {"series_ticker": series_ticker, "status": "open", "limit": 100})
        return data.get("markets", [])

    def market(self, ticker: str):
        return self._get(f"/markets/{ticker}")["market"]

    def orderbook(self, ticker: str):
        return self._get(f"/markets/{ticker}/orderbook").get("orderbook_fp", {})

    def trades(self, ticker: str, limit: int = 200, min_ts: int | None = None):
        params = {"ticker": ticker, "limit": limit}
        if min_ts:
            params["min_ts"] = int(min_ts)
        return self._get("/markets/trades", params).get("trades", [])

    def event(self, event_ticker: str):
        return self._get(f"/events/{event_ticker}")["event"]


def book_depth(book: dict, levels: int = 5):
    """Return (yes_bid_levels, no_bid_levels) as [(price, size), ...] best first."""
    yes = [(float(p), float(s)) for p, s in (book.get("yes_dollars") or []) if float(s) > 0]
    no = [(float(p), float(s)) for p, s in (book.get("no_dollars") or []) if float(s) > 0]
    yes.sort(key=lambda x: -x[0])
    no.sort(key=lambda x: -x[0])
    return yes[:levels], no[:levels]
