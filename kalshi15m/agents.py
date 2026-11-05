"""The trading committee: four independent desks plus a risk officer.

Each desk looks at a different slice of the same data and states a probability
that the market settles YES (BTC closes at/above the strike). The risk officer
does not pick a direction - it can only veto or size down.

The debate happens in two rounds: opening statements, then rebuttals where desks
react to each other and adjust conviction before the chair rules.
"""
from __future__ import annotations

from .util import clamp, sigmoid


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.3f}%"


def momentum_desk(f: dict) -> dict:
    s = max(1e-9, f["sigma_1m"])
    z3 = f["ret_3m"] / (s * 1.7320508)
    z5 = f["ret_5m"] / (s * 2.2360680)
    score = 0.65 * z3 + 0.35 * z5
    p = sigmoid(0.55 * score)
    conf = clamp(0.25 + 0.18 * abs(score), 0.15, 0.90)
    lean = "up" if score >= 0 else "down"
    thesis = (
        f"Spot {lean} over the last minutes: 3m {_fmt_pct(f['ret_3m'])} ({z3:+.2f} sigma), "
        f"5m {_fmt_pct(f['ret_5m'])} ({z5:+.2f} sigma)."
    )
    points = [
        f"1m {_fmt_pct(f['ret_1m'])} / 3m {_fmt_pct(f['ret_3m'])} / 5m {_fmt_pct(f['ret_5m'])}",
        f"Trend score {score:+.2f} -> P(yes)={p:.2f}",
        "Momentum decays fast in 15m binaries: only actionable early in the window.",
    ]
    return {
        "agent": "momentum",
        "label": "Momentum desk",
        "p_yes": p,
        "confidence": conf,
        "stance": "YES" if p >= 0.5 else "NO",
        "thesis": thesis,
        "points": points,
        "score": score,
    }


def meanrev_desk(f: dict) -> dict:
    z = f["z_meanrev"]
    p = sigmoid(-0.45 * z)
    conf = clamp(0.20 + 0.15 * abs(z), 0.12, 0.80)
    stretched = "stretched above" if z > 0 else "stretched below"
    dev = (f["spot"] / f["mean_10m"] - 1.0) if f["mean_10m"] > 0 else 0.0
    thesis = (
        f"Spot is {stretched} its 10-minute mean by {_fmt_pct(dev)} "
        f"({z:+.2f} sigma); reversion favors {'NO' if z > 0 else 'YES'}."
    )
    points = [
        f"10m mean {f['mean_10m']:.2f} vs spot {f['spot']:.2f}",
        f"Stretch z={z:+.2f} -> P(yes)={p:.2f}",
        "In a trending tape this desk is systematically early - size accordingly.",
    ]
    return {
        "agent": "meanrev",
        "label": "Mean-reversion desk",
        "p_yes": p,
        "confidence": conf,
        "stance": "YES" if p >= 0.5 else "NO",
        "thesis": thesis,
        "points": points,
        "score": -z,
    }


def quant_desk(f: dict) -> dict:
    p = f["p_model"]
    z = f["dist_sigma"]
    conf = clamp(0.40 + 0.25 * min(abs(z), 2.5) / 2.5, 0.30, 0.92)
    thesis = (
        f"Brownian fair value: spot is {z:+.2f} sigma from the {f['strike']:.2f} strike with "
        f"{f['time_left'] / 60:.1f} min left and {f['sigma_15m'] * 100:.3f}% 15m vol."
    )
    points = [
        f"P(close >= strike) = {p:.2f} vs market mid {f['yes_mid']:.2f} (raw edge {p - f['yes_mid']:+.3f})",
        f"Distance {f['dist_pct'] * 100:+.3f}% of strike; remaining sigma {f['sigma_rem'] * 100:.3f}%",
        "Model is volatility-driven: if realized vol is mis-estimated, fair value is wrong.",
    ]
    return {
        "agent": "quant",
        "label": "Quant desk",
        "p_yes": p,
        "confidence": conf,
        "stance": "YES" if p >= 0.5 else "NO",
        "thesis": thesis,
        "points": points,
        "score": z,
    }


def micro_desk(f: dict) -> dict:
    score = 1.10 * f["ob_imbalance"] + 0.90 * f["taker_flow"]
    p = sigmoid(score)
    conf = clamp(0.22 + 0.30 * (abs(f["ob_imbalance"]) + abs(f["taker_flow"])), 0.10, 0.85)
    book_dir = "bid-heavy" if f["ob_imbalance"] > 0.02 else ("offer-heavy" if f["ob_imbalance"] < -0.02 else "balanced")
    flow_dir = "net taker buying of YES" if f["taker_flow"] > 0.02 else (
        "net taker selling of YES" if f["taker_flow"] < -0.02 else "no taker skew"
    )
    thesis = (
        f"Book is {book_dir} (imbalance {f['ob_imbalance']:+.2f}) and the 2-minute tape shows {flow_dir} "
        f"({f['taker_flow']:+.2f}); spread {f['spread'] * 100:.1f}c."
    )
    points = [
        f"Book: {f['yes_depth']:.0f} contracts offered YES / {f['no_depth']:.0f} offered NO",
        f"Volume {f['volume']:,.0f}, rate {f['volume_rate']:,.1f}/s, OI {f['oi']:,.0f}",
        f"Flow P(yes)={p:.2f}",
    ]
    return {
        "agent": "micro",
        "label": "Microstructure desk",
        "p_yes": p,
        "confidence": conf,
        "stance": "YES" if p >= 0.5 else "NO",
        "thesis": thesis,
        "points": points,
        "score": f["ob_imbalance"] + f["taker_flow"],
    }


def risk_officer(f: dict, ctx: dict) -> dict:
    cfg = ctx["cfg"]
    vetoes: list[str] = []
    notes: list[str] = []

    if f["spread"] > cfg["max_spread"]:
        vetoes.append(f"spread {f['spread'] * 100:.1f}c exceeds {cfg['max_spread'] * 100:.1f}c cap")
    if f["time_left"] < cfg["min_time_left_s"]:
        vetoes.append(f"only {f['time_left']:.0f}s left (min {cfg['min_time_left_s']}s)")
    if ctx.get("open_position"):
        vetoes.append("already holding a position in this window")
    if ctx.get("day_pnl", 0.0) <= -cfg["daily_loss_limit_pct"] * ctx["bankroll"]:
        vetoes.append(
            f"daily loss {ctx['day_pnl']:+.2f} hit the {cfg['daily_loss_limit_pct'] * 100:.0f}% circuit breaker"
        )
    if min(f["yes_depth"], f["no_depth"]) < 20:
        notes.append(f"thin offer depth ({f['yes_depth']:.0f}/{f['no_depth']:.0f} contracts) - size down")

    shock = abs(f.get("ret_1m", 0.0)) / max(1e-9, f["sigma_1m"])
    if shock > 3.0:
        vetoes.append(f"1-minute vol shock ({shock:.1f} sigma) - fair value is unreliable during a regime break")
    if f.get("vol_ratio", 1.0) > cfg.get("max_vol_ratio", 2.5):
        vetoes.append(
            f"fast vol is {f['vol_ratio']:.1f}x the slow estimate - the model is calibrating on a different tape"
        )

    price_ok = cfg["entry_min_price"] <= f["yes_mid"] <= cfg["entry_max_price"]
    if not price_ok:
        vetoes.append(
            f"mid {f['yes_mid']:.2f} outside tradeable band "
            f"[{cfg['entry_min_price']:.2f}, {cfg['entry_max_price']:.2f}] - risk/reward too poor"
        )

    conf = 0.95 if not vetoes else 0.35
    thesis = "No risk vetoes; trade is inside the risk budget." if not vetoes else "Veto: " + "; ".join(vetoes)
    return {
        "agent": "risk",
        "label": "Risk officer",
        "p_yes": None,
        "confidence": conf,
        "stance": "HOLD" if vetoes else "ALLOW",
        "thesis": thesis,
        "points": vetoes + notes,
        "vetoes": vetoes,
        "notes": notes,
    }


def opening_statements(f: dict, ctx: dict) -> list[dict]:
    return [momentum_desk(f), meanrev_desk(f), quant_desk(f), micro_desk(f), risk_officer(f, ctx)]


def weighted_p(votes, weights):
    num = den = 0.0
    for v in votes:
        if v["p_yes"] is None:
            continue
        w = weights.get(v["agent"], 0.0) * max(0.05, v["confidence"])
        num += w * v["p_yes"]
        den += w
    return (num / den) if den > 0 else 0.5


def rebuttals(votes, weights, f: dict) -> list[dict]:
    """Round two: each desk reacts to the room and adjusts conviction."""
    by = {v["agent"]: v for v in votes}
    provisional = weighted_p(votes, weights)
    direction = "YES" if provisional >= 0.5 else "NO"
    out = []

    for v in votes:
        agent = v["agent"]
        text = ""
        adj = 1.0

        if agent == "momentum" and "quant" in by:
            q = by["quant"]
            if (q["p_yes"] - 0.5) * (v["p_yes"] - 0.5) < 0:
                text = (
                    f"The quant model sits at P={q['p_yes']:.2f}, against my trend read. "
                    "Two different lenses, so I cut conviction rather than fight it."
                )
                adj = 0.80
            elif (by["micro"]["p_yes"] - 0.5) * (v["p_yes"] - 0.5) > 0:
                text = "Order flow points the same way as price. I hold my conviction."
                adj = 1.05
            else:
                text = "Flow is not confirming the move, but price is the primary signal. Standing pat."
        elif agent == "meanrev":
            strong_trend = abs(by["momentum"].get("score", 0.0)) > 1.0
            trend_dir = by["momentum"]["stance"]
            if strong_trend and trend_dir == direction:
                text = (
                    f"Trend score {by['momentum'].get('score', 0):+.2f} with flow confirming. "
                    "Fading a live trend is a low-probability game - I stand down."
                )
                adj = 0.60
            else:
                text = "Price is still stretched from its mean; I keep my fade, sized small."
        elif agent == "quant":
            dis = [by[a]["stance"] for a in ("momentum", "meanrev") if by[a]["stance"] != direction]
            if len(dis) >= 2:
                text = (
                    "Both discretionary desks lean against fair value. Either my vol estimate is stale "
                    "or the market knows something - I widen my error bars."
                )
                adj = 0.85
            elif abs(v["p_yes"] - f["yes_mid"]) > 0.15:
                text = (
                    f"Market mid {f['yes_mid']:.2f} is far from my fair value {v['p_yes']:.2f}; "
                    "that usually means the tape is pricing a catalyst. I keep the edge but do not add."
                )
                adj = 0.90
            else:
                text = "Model and market are broadly aligned; the edge is thin but real."
        elif agent == "micro":
            if by["risk"]["vetoes"] and any("spread" in x for x in by["risk"]["vetoes"]):
                text = "Risk is right that the spread is wide; my flow read gets noisier, so I fade myself."
                adj = 0.70
            elif abs(by["micro"]["p_yes"] - 0.5) > 0.2:
                text = "Book and tape agree with each other, which is rare enough to trust. Keeping size."
                adj = 1.05
            else:
                text = "Flow is mixed; no strong conviction from my side."
        elif agent == "risk":
            if v["vetoes"]:
                text = "The veto stands unless the chair documents an exception. I do not have one."
            else:
                text = "No objection. Keep stake inside the risk budget."

        new_conf = clamp(v["confidence"] * adj, 0.05, 0.95)
        out.append(
            {
                "agent": agent,
                "label": v["label"],
                "text": text,
                "confidence": new_conf,
                "p_yes": v["p_yes"],
                "stance": v["stance"],
            }
        )
    return out


def chair_rule(votes, weights) -> dict:
    p = weighted_p(votes, weights)
    direction = "YES" if p >= 0.5 else "NO"

    agreement_num = agreement_den = 0.0
    for v in votes:
        if v["p_yes"] is None:
            continue
        w = weights.get(v["agent"], 0.0) * max(0.05, v["confidence"])
        agreement_den += w
        if (v["p_yes"] - 0.5) * (p - 0.5) > 0:
            agreement_num += w
    agreement = agreement_num / agreement_den if agreement_den > 0 else 0.0

    confidence = clamp(2 * abs(p - 0.5) * (0.45 + 0.55 * agreement), 0.0, 0.95)
    return {"p_yes": p, "direction": direction, "agreement": agreement, "confidence": confidence}
