"""Operator CLI for the paper desk (used by the LLM council and by humans)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .api import Kalshi
from .db import DB
from .features import build_features
from .journal import build as build_journal
from .journal import live_state
from .paper import close_paper_trade, fee, open_paper_trade
from .spot import SpotFeed
from .util import DATA_DIR, DB_PATH, iso, load_json, now_ts, parse_kalshi_ts

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def cfg_load():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def pick_window(db, api, now, window_id=None):
    if window_id:
        row = db.get_window(window_id)
        if row:
            return dict(row)
    row = db.q1("SELECT * FROM windows WHERE close_ts > ? ORDER BY open_ts LIMIT 1", (now,))
    if row:
        return dict(row)
    # fall back to the live Kalshi listing
    for m in api.open_btc15m_markets():
        open_ts = parse_kalshi_ts(m.get("open_time"))
        close_ts = parse_kalshi_ts(m.get("close_time"))
        if open_ts and close_ts and open_ts <= now < close_ts:
            w = {
                "window_id": m["event_ticker"],
                "market_ticker": m["ticker"],
                "open_ts": open_ts,
                "close_ts": close_ts,
                "strike": float(m.get("floor_strike") or 0.0),
            }
            db.upsert_window(w)
            return w
    return None


def snapshot_now(db, api, window):
    book = api.orderbook(window["market_ticker"])
    market = api.market(window["market_ticker"])
    trades = api.trades(window["market_ticker"], limit=200, min_ts=int(time.time() - 300))
    f = build_features(db, window, market, book, trades, now_ts())
    f["last"] = float(market.get("last_price_dollars") or 0.0)
    return f, market


def cmd_status(args):
    db = DB(args.db)
    st = live_state(db, cfg_load())
    cur = st.get("current") or {}
    out = {
        "now": iso(time.time()),
        "bankroll": st["bankroll"],
        "realized": st["stats"].get("net", 0.0),
        "trades": st["stats"].get("n", 0),
        "win_rate": st["stats"].get("win_rate", 0.0),
        "open": st["open"],
        "current_window": cur.get("window_id"),
        "spot": cur.get("spot"),
        "yes_bid": cur.get("yes_bid"),
        "yes_ask": cur.get("yes_ask"),
        "p_model": cur.get("p_model"),
        "time_left": cur.get("time_left"),
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_context(args):
    db = DB(args.db)
    api = Kalshi()
    now = now_ts()
    w = pick_window(db, api, now, args.window)
    if not w:
        print(json.dumps({"error": "no active window"}))
        return 1
    f, market = snapshot_now(db, api, w)
    hist = db.recent_snapshots(w["window_id"], limit=30)
    payload = {
        "generated_at": iso(now),
        "series": "KXBTC15M",
        "window": {
            "window_id": w["window_id"],
            "market_ticker": w["market_ticker"],
            "open_at": iso(float(w["open_ts"])),
            "close_at": iso(float(w["close_ts"])),
            "strike": float(w["strike"] or 0),
            "time_left_s": round(f["time_left"], 1),
        },
        "market": {
            "title": market.get("title"),
            "yes_bid": f["yes_bid"],
            "yes_ask": f["yes_ask"],
            "mid": f["yes_mid"],
            "spread": f["spread"],
            "last": f["last"],
            "volume": f["volume"],
            "open_interest": f["oi"],
        },
        "spot": {
            "price": f["spot"],
            "ret_1m_pct": f["ret_1m"] * 100,
            "ret_3m_pct": f["ret_3m"] * 100,
            "ret_5m_pct": f["ret_5m"] * 100,
            "ret_10m_pct": f["ret_10m"] * 100,
            "sigma_15m_pct": f["sigma_15m"] * 100,
            "distance_pct": f["dist_pct"] * 100,
            "distance_sigma": f["dist_sigma"],
            "mean_10m": f["mean_10m"],
        },
        "model": {
            "p_yes_brownian": f["p_model"],
            "edge_yes": f["edge_yes"],
            "edge_no": f["edge_no"],
        },
        "micro": {
            "book_imbalance": f["ob_imbalance"],
            "taker_flow_2m": f["taker_flow"],
            "yes_offer_depth": f["yes_depth"],
            "no_offer_depth": f["no_depth"],
            "volume_rate_per_s": f["volume_rate"],
        },
        "history": [
            {
                "t": iso(float(r["ts"])),
                "spot": r["spot"],
                "yes_mid": r["yes_mid"],
                "p_model": round(float(r["p_model"] or 0), 3),
            }
            for r in reversed(hist)
        ],
        "position": dict(db.open_trade(w["window_id"])) if db.open_trade(w["window_id"]) else None,
    }
    os.makedirs(os.path.join(DATA_DIR, "council"), exist_ok=True)
    path = os.path.join(DATA_DIR, "council", f"context_{w['window_id']}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
    print(json.dumps(payload, indent=2, default=str))
    return 0


def cmd_enter(args):
    db = DB(args.db)
    cfg = cfg_load()
    api = Kalshi()
    now = now_ts()
    w = pick_window(db, api, now, args.window)
    if not w:
        print(json.dumps({"error": "no active window"}))
        return 1
    if db.open_trade(w["window_id"]):
        print(json.dumps({"error": "window already has an open position"}))
        return 1
    f, _ = snapshot_now(db, api, w)
    side = args.side.upper()
    price = args.price if args.price is not None else (f["yes_ask"] if side == "YES" else round(1.0 - f["yes_bid"], 3))
    contracts = args.contracts or max(1, int(min(cfg["max_stake"], db.balance() * cfg["risk_pct"]) // price))
    verdict = {
        "action": "ENTER",
        "side": side,
        "price": price,
        "contracts": contracts,
        "stake": round(contracts * price, 2),
        "edge": args.edge,
        "p_yes": args.p_yes,
        "confidence": args.confidence,
        "reason": args.reason,
    }
    trade = open_paper_trade(db, w, verdict, args.source, cfg)
    build_journal(db, cfg)
    print(json.dumps({"ok": True, "trade": trade, "window": w["window_id"],
                      "entry_price": price, "contracts": contracts,
                      "cost": round(contracts * price, 2), "fee": fee(contracts, price, cfg["fee_rate"])},
                     indent=2, default=str))
    return 0


def cmd_exit(args):
    db = DB(args.db)
    cfg = cfg_load()
    trade = db.q1("SELECT * FROM trades WHERE status='open' ORDER BY entry_ts DESC LIMIT 1")
    if not trade:
        print(json.dumps({"error": "no open trade"}))
        return 1
    api = Kalshi()
    w = dict(db.get_window(trade["window_id"]))
    f, _ = snapshot_now(db, api, w)
    px = f["yes_bid"] if trade["side"] == "YES" else round(1.0 - f["yes_ask"], 3)
    res = close_paper_trade(db, dict(trade), px, args.reason, cfg)
    build_journal(db, cfg)
    print(json.dumps({"ok": True, "exit_price": px, **res}, indent=2, default=str))
    return 0


def cmd_note(args):
    """Record an LLM council verdict (no trade) so it lands in the journal."""
    db = DB(args.db)
    cfg = cfg_load()
    api = Kalshi()
    now = now_ts()
    w = pick_window(db, api, now, args.window)
    if not w:
        print(json.dumps({"error": "no active window"}))
        return 1
    payload = load_json(args.file, {}) if args.file else {}
    payload.setdefault("ts", iso(now))
    payload.setdefault("window_id", w["window_id"])
    payload.setdefault("verdict", {"action": "HOLD", "reason": args.reason or ""})
    db.add_council(f"C-{w['window_id']}-{int(now)}", w["window_id"], now, payload)
    build_journal(db, cfg)
    print(json.dumps({"ok": True, "window": w["window_id"]}, indent=2))
    return 0


def cmd_lock(args):
    """Reserve a window for the LLM council: the fast committee stands down until `until`."""
    path = os.path.join(DATA_DIR, "council_lock", f"{args.window}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"window": args.window, "until": time.time() + args.seconds, "reason": args.reason}, fh)
    print(json.dumps({"ok": True, "path": path, "until_in_s": args.seconds}))
    return 0


def cmd_report(args):
    db = DB(args.db)
    cfg = cfg_load()
    st = live_state(db, cfg)
    print(json.dumps({k: st[k] for k in ("bankroll", "stats", "exposure", "windows")}, indent=2, default=str))
    return 0


def cmd_journal(args):
    db = DB(args.db)
    cfg = cfg_load()
    build_journal(db, cfg)
    print("journal rebuilt")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kalshi15m.cli")
    ap.add_argument("--db", default=DB_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    sub.add_parser("journal").set_defaults(fn=cmd_journal)

    p = sub.add_parser("context")
    p.add_argument("--window")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_context)

    p = sub.add_parser("enter")
    p.add_argument("--window")
    p.add_argument("--side", required=True, choices=["YES", "yes", "NO", "no"])
    p.add_argument("--contracts", type=int)
    p.add_argument("--price", type=float)
    p.add_argument("--reason", default="")
    p.add_argument("--source", default="llm_council")
    p.add_argument("--edge", type=float, default=None)
    p.add_argument("--p-yes", dest="p_yes", type=float, default=None)
    p.add_argument("--confidence", type=float, default=None)
    p.set_defaults(fn=cmd_enter)

    p = sub.add_parser("exit")
    p.add_argument("--reason", default="manual exit")
    p.set_defaults(fn=cmd_exit)

    p = sub.add_parser("note")
    p.add_argument("--window")
    p.add_argument("--file")
    p.add_argument("--reason", default="")
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("lock")
    p.add_argument("--window", required=True)
    p.add_argument("--seconds", type=int, default=300)
    p.add_argument("--reason", default="llm council")
    p.set_defaults(fn=cmd_lock)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
