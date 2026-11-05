"""Committee orchestration: debate -> chair ruling -> trade instruction."""
from __future__ import annotations

import math

from . import agents
from .util import clamp

_AGENT_ORDER = ["momentum", "meanrev", "quant", "micro", "risk"]


def _plan(entry_price: float, side: str, cfg: dict) -> dict:
    return {
        "mode": "hold_to_settlement",
        "hard_stop_price": round(entry_price * cfg["stop_loss_frac"], 4),
        "take_profit_price": round(min(0.98, entry_price + cfg["take_profit_price"] * entry_price), 4),
        "flip_exit_p": 0.35 if side == "YES" else 0.65,
        "time_stop_s": cfg["exit_min_time_left_s"],
        "note": (
            "Exit early if the mark falls to the hard stop, if the model flips below "
            f"{0.35 if side == 'YES' else 0.65:.2f} with time left, or if fewer than "
            f"{cfg['exit_min_time_left_s']}s remain. Otherwise settle at window close."
        ),
    }


def run_committee(f: dict, ctx: dict, cfg: dict) -> dict:
    weights = cfg["weights"]
    votes = agents.opening_statements(f, ctx)
    rebut = agents.rebuttals(votes, weights, f)

    # merge round-2 conviction back onto the desks
    conf_by = {r["agent"]: r["confidence"] for r in rebut}
    final_votes = []
    for v in votes:
        v = dict(v)
        v["confidence"] = conf_by.get(v["agent"], v["confidence"])
        final_votes.append(v)

    chair = agents.chair_rule(final_votes, weights)
    risk_vote = next(v for v in votes if v["agent"] == "risk")
    vetoes = list(risk_vote.get("vetoes", []))
    p_quant = next(v["p_yes"] for v in votes if v["agent"] == "quant")

    # Anchor on the quant fair value and let the discretionary desks *tilt* it.
    # A plain probability average would drag every extreme market toward 0.5 and
    # manufacture fake longshot edge, which is exactly how the 22:52 trade lost.
    tilt = 0.0
    for v in final_votes:
        if v["agent"] == "quant" or v["p_yes"] is None:
            continue
        tilt += weights.get(v["agent"], 0.0) * (v["p_yes"] - 0.5)
    p_raw = clamp(p_quant + 0.40 * tilt, 0.01, 0.99)

    # shrink fair value toward 0.5 to account for model error (never over-trust one estimate)
    p = 0.5 + 0.95 * (p_raw - 0.5)

    ask_yes = float(f["yes_ask"])
    ask_no = float(1.0 - f["yes_bid"])
    quote_move = float(f.get("quote_move_60", 0.0))

    def fee_pc(px: float) -> float:
        return cfg["fee_rate"] * px * (1.0 - px)

    # edge measured after the fee you actually pay for the contract you actually buy
    edge_yes = p - ask_yes - fee_pc(ask_yes)
    edge_no = (1.0 - p) - ask_no - fee_pc(ask_no)

    max_edge = cfg.get("max_edge", 0.18)
    action, side, price, edge = "HOLD", None, 0.0, 0.0
    reason = ""
    best_edge = max(edge_yes, edge_no)
    knife_yes = quote_move < -0.06
    knife_no = quote_move > 0.06

    if vetoes:
        reason = "risk veto: " + "; ".join(vetoes)
    elif edge_yes > max_edge and edge_yes >= edge_no:
        reason = (
            f"YES edge {edge_yes:+.3f} is above the {max_edge:.2f} sanity cap - "
            "a gap that large is a stale quote or a broken vol input, not free money"
        )
    elif edge_no > max_edge and edge_no > edge_yes:
        reason = (
            f"NO edge {edge_no:+.3f} is above the {max_edge:.2f} sanity cap - "
            "a gap that large is a stale quote or a broken vol input, not free money"
        )
    elif edge_yes >= cfg["min_edge"] and edge_yes >= edge_no:
        if knife_yes:
            reason = f"YES edge {edge_yes:+.3f} but the quote is collapsing ({quote_move:+.2f} in 60s) - big red candle"
        else:
            action, side, price, edge = "ENTER", "YES", ask_yes, edge_yes
            reason = (
                f"model {p:.2f} vs ask {ask_yes:.2f} -> {edge:+.3f} net edge on YES "
                f"(fee {fee_pc(ask_yes):.3f})"
            )
    elif edge_no >= cfg["min_edge"]:
        if knife_no:
            reason = f"NO edge {edge_no:+.3f} but the quote is running away ({quote_move:+.2f} in 60s) - chasing a spike"
        else:
            action, side, price, edge = "ENTER", "NO", ask_no, edge_no
            reason = (
                f"model {1 - p:.2f} vs ask {ask_no:.2f} -> {edge:+.3f} net edge on NO "
                f"(fee {fee_pc(ask_no):.3f})"
            )
    else:
        reason = (
            f"best net edge {best_edge:+.3f} is below the {cfg['min_edge']:.2f} threshold"
            f" (P(yes)={p:.2f}, yes {f['yes_bid']:.2f}/{ask_yes:.2f})"
        )

    bankroll = ctx["bankroll"]
    contracts = 0
    stake = 0.0
    if action == "ENTER":
        budget = min(cfg["max_stake"], bankroll * cfg["risk_pct"])
        contracts = int(budget // price) if price > 0 else 0
        depth = f["yes_depth"] if side == "YES" else f["no_depth"]
        if depth >= 1:
            contracts = min(contracts, int(max(1, depth)))
        if contracts < cfg["min_contracts"]:
            action, side = "HOLD", None
            reason = f"budget {budget:.2f} cannot buy a contract at {price:.2f}"
        else:
            stake = round(contracts * price, 2)

    exit_plan = _plan(price, side, cfg) if action == "ENTER" else {}

    chair_text = (
        f"Quant anchor P(yes)={p_quant:.2f}, tilted {0.40 * tilt:+.3f} by the discretionary desks, "
        f"shrunk to {p:.2f} for model error; {chair['agreement'] * 100:.0f}% weighted agreement, "
        f"{chair['confidence']:.2f} confidence. "
        + (f"I take {side} at {price:.2f} x {contracts} contracts ({stake:.2f} notional)." if action == "ENTER"
           else "No trade: " + reason + ".")
    )

    transcript = []
    for v in votes:
        transcript.append(
            {
                "round": 1,
                "agent": v["agent"],
                "label": v["label"],
                "stance": v["stance"],
                "p_yes": v["p_yes"],
                "confidence": round(v["confidence"], 3),
                "text": v["thesis"],
                "points": v["points"],
            }
        )
    for r in rebut:
        transcript.append(
            {
                "round": 2,
                "agent": r["agent"],
                "label": r["label"],
                "stance": r["stance"],
                "p_yes": r["p_yes"],
                "confidence": round(r["confidence"], 3),
                "text": r["text"],
                "points": [],
            }
        )
    transcript.append(
        {
            "round": 3,
            "agent": "chair",
            "label": "Chair",
            "stance": action,
            "p_yes": p,
            "confidence": round(chair["confidence"], 3),
            "text": chair_text,
            "points": [f"decision: {action} {side or ''} @ {price:.2f} edge {edge:+.3f}"] if action == "ENTER" else [reason],
        }
    )

    verdict = {
        "action": action,
        "side": side,
        "price": round(price, 4),
        "contracts": contracts,
        "stake": stake,
        "edge": round(edge, 4),
        "edge_gross": round(edge + (cfg["fee_rate"] * price * (1.0 - price)), 4) if action == "ENTER" else 0.0,
        "p_yes": round(p, 4),
        "p_yes_raw": round(p_raw, 4),
        "confidence": round(chair["confidence"], 4),
        "agreement": round(chair["agreement"], 4),
        "reason": reason,
        "exit_plan": exit_plan,
        "vetoes": vetoes,
    }
    return {"transcript": transcript, "verdict": verdict, "features": f}
