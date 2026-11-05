"""Paper execution: fees, fills, settlement and the bankroll ledger."""
from __future__ import annotations

import math
import uuid

from .util import iso, now_ts


def fee(contracts: int, price: float, rate: float = 0.07) -> float:
    """Kalshi quadratic fee: ceil(rate * C * P * (1-P)) rounded up to the cent."""
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def trade_id(prefix: str = "T") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def open_paper_trade(db, window: dict, verdict: dict, source: str, cfg: dict, debate_id: str | None = None) -> dict:
    ts = now_ts()
    contracts = int(verdict["contracts"])
    price = float(verdict["price"])
    cost = round(contracts * price, 2)
    f = fee(contracts, price, cfg["fee_rate"])
    tid = trade_id()
    trade = {
        "id": tid,
        "window_id": window["window_id"],
        "market_ticker": window["market_ticker"],
        "source": source,
        "side": verdict["side"],
        "entry_ts": ts,
        "entry_price": price,
        "contracts": contracts,
        "cost": cost,
        "entry_fee": f,
        "p_model_entry": verdict.get("p_yes"),
        "p_final": None,
        "confidence": verdict.get("confidence"),
        "edge": verdict.get("edge"),
        "debate_id": debate_id,
        "notes": verdict.get("reason"),
        "status": "open",
    }
    db.add_trade(trade)
    return trade


def close_paper_trade(db, trade: dict, exit_price: float, reason: str, cfg: dict) -> dict:
    contracts = int(trade["contracts"])
    entry_price = float(trade["entry_price"])
    entry_fee = float(trade["entry_fee"])
    exit_fee = fee(contracts, exit_price, cfg["fee_rate"])
    proceeds = contracts * exit_price
    pnl = round(proceeds - entry_fee - exit_fee - contracts * entry_price, 4)
    db.mark_exit(trade["id"], now_ts(), exit_price, exit_fee, reason, pnl)
    bal = db.add_ledger(now_ts(), "trade_exit", pnl, trade["id"])
    return {"pnl": pnl, "exit_fee": exit_fee, "balance": bal, "reason": reason}


def settle_paper_trade(db, trade: dict, result: str, final_value: float | None) -> dict:
    contracts = int(trade["contracts"])
    entry_price = float(trade["entry_price"])
    entry_fee = float(trade["entry_fee"])
    won = (trade["side"] == "YES" and result == "yes") or (trade["side"] == "NO" and result == "no")
    payout = round(contracts * 1.0, 2) if won else 0.0
    pnl = round(payout - contracts * entry_price - entry_fee, 4)
    db.settle_trade(trade["id"], result, payout, pnl, final_value)
    bal = db.add_ledger(now_ts(), "settlement", pnl, trade["id"])
    return {"won": won, "pnl": pnl, "payout": payout, "balance": bal}


def open_exposure(db) -> float:
    rows = db.open_trades()
    return round(sum(float(r["cost"]) for r in rows), 2)


def cash_available(db) -> float:
    return round(db.balance() - open_exposure(db), 2)
