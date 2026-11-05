"""Renders the human-facing journal: JOURNAL.md, CSVs, live.json and board.html."""
from __future__ import annotations

import csv
import html
import json
import os

from .util import DATA_DIR, JOURNAL_DIR, iso

JOURNAL_MD = os.path.join(JOURNAL_DIR, "JOURNAL.md")
TRADES_CSV = os.path.join(JOURNAL_DIR, "trades.csv")
EQUITY_CSV = os.path.join(JOURNAL_DIR, "equity.csv")
LIVE_JSON = os.path.join(JOURNAL_DIR, "live.json")
BOARD_HTML = os.path.join(JOURNAL_DIR, "board.html")


def _rows(db):
    wins = db.q("SELECT * FROM windows ORDER BY open_ts")
    trades = db.all_trades()
    by_window: dict[str, list] = {}
    for t in trades:
        by_window.setdefault(t["window_id"], []).append(t)
    debates = db.q("SELECT * FROM debates ORDER BY ts")
    db_by_window: dict[str, list] = {}
    for d in debates:
        db_by_window.setdefault(d["window_id"], []).append(d)
    council: dict[str, list] = {}
    for c in db.q("SELECT * FROM council ORDER BY ts"):
        council.setdefault(c["window_id"], []).append(c)
    return wins, trades, by_window, db_by_window, council


def _stats(trades):
    closed = [t for t in trades if t["status"] in ("settled", "closed") and t["pnl"] is not None]
    if not closed:
        return {"n": 0}
    pnls = [float(t["pnl"]) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    fees = sum(float(t["entry_fee"] or 0) + float(t["exit_fee"] or 0) for t in closed)
    edges = [float(t["edge"]) for t in closed if t["edge"] is not None]
    return {
        "n": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "net": sum(pnls),
        "gross_win": sum(wins),
        "gross_loss": sum(losses),
        "avg": sum(pnls) / len(pnls),
        "best": max(pnls),
        "worst": min(pnls),
        "fees": fees,
        "avg_edge": (sum(edges) / len(edges)) if edges else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf"),
    }


def _max_drawdown(db):
    bal = 0.0
    peak = 0.0
    mdd = 0.0
    for row in db.ledger_rows():
        bal = float(row["balance"])
        peak = max(peak, bal)
        mdd = max(mdd, peak - bal)
    return mdd


def _fmt_money(x: float) -> str:
    return f"{x:+,.2f}"


def _window_line(w) -> str:
    return f"{iso(w['open_ts'])[11:16]}-{iso(w['close_ts'])[11:16]} UTC"


def _trade_line(t) -> str:
    pnl = t["pnl"]
    if pnl is None:
        outcome = "OPEN"
        pnl_s = "-"
    else:
        outcome = t["result"] or t["exit_reason"] or "closed"
        pnl_s = _fmt_money(float(pnl))
    return (
        f"| {iso(t['entry_ts'])[11:19]} | `{t['window_id']}` | {t['source']} | **{t['side']}** "
        f"| {t['entry_price']:.2f} x {t['contracts']} | {float(t['edge'] or 0) * 100:+.1f}c "
        f"| {outcome} | {pnl_s} |"
    )


def render_journal(db, cfg) -> str:
    wins, trades, by_window, debates_by_window, council_by_window = _rows(db)
    stats = _stats(trades)
    balance = db.balance()
    start = float(cfg["bankroll_start"])
    open_trades = db.open_trades()
    exposure = sum(float(t["cost"]) for t in open_trades)
    mdd = _max_drawdown(db)

    lines = []
    A = lines.append
    A("# Kalshi BTC15M - Paper Trading Journal")
    A("")
    A("> **Simulation only.** No real orders are placed and no money is at risk. Fills are paper fills at the")
    A("> displayed Kalshi top-of-book at decision time; fees follow Kalshi's published quadratic schedule.")
    A("> Each 15-minute window is a binary market: YES pays $1 if BTC's close reference is at or above the")
    A("> window's strike (the prior window's close reference).")
    A("")
    A(f"_Last updated: {iso(__import__('time').time())} - engine: `kalshi15m.engine`_")
    A("")
    A("## Desk status")
    A("")
    A("| | |")
    A("|---|---|")
    A(f"| Bankroll | **${balance:,.2f}** (start ${start:,.2f}, {_fmt_money(balance - start)}) |")
    A(f"| Realized P/L | {_fmt_money(stats.get('net', 0.0))} over {stats.get('n', 0)} trades |")
    A(f"| Win rate | {stats.get('win_rate', 0.0) * 100:.1f}% ({stats.get('wins', 0)}W / {stats.get('losses', 0)}L) |")
    A(f"| Open exposure | ${exposure:,.2f} across {len(open_trades)} position(s) |")
    A(f"| Max drawdown | ${mdd:,.2f} |")
    A(f"| Windows tracked | {len(wins)} |")
    A("")

    if open_trades:
        A("### Open positions")
        A("")
        A("| Window | Side | Entry | Contracts | Mark | Unrealized |")
        A("|---|---|---|---|---|---|")
        for t in open_trades:
            snap = db.q1("SELECT yes_mid FROM snapshots WHERE window_id=? ORDER BY ts DESC LIMIT 1", (t["window_id"],))
            mark = float(snap["yes_mid"]) if snap else float(t["entry_price"])
            mark_side = mark if t["side"] == "YES" else 1.0 - mark
            unreal = (mark_side - float(t["entry_price"])) * int(t["contracts"])
            A(
                f"| `{t['window_id']}` | {t['side']} | {t['entry_price']:.2f} | {t['contracts']} "
                f"| {mark_side:.2f} | {unreal:+.2f} |"
            )
        A("")

    A("## Performance")
    A("")
    A("| Metric | Value |")
    A("|---|---|")
    A(f"| Net P/L | {_fmt_money(stats.get('net', 0.0))} |")
    A(f"| Return on bankroll | {((balance / start) - 1) * 100:+.2f}% |")
    A(f"| Trades settled | {stats.get('n', 0)} |")
    A(f"| Average P/L per trade | {_fmt_money(stats.get('avg', 0.0))} |")
    A(f"| Best / worst trade | {_fmt_money(stats.get('best', 0.0))} / {_fmt_money(stats.get('worst', 0.0))} |")
    A(f"| Fees paid | ${stats.get('fees', 0.0):,.2f} |")
    A(f"| Average entry edge | {stats.get('avg_edge', 0.0) * 100:+.1f}c |")
    A("")

    # per source
    sources = {}
    for t in trades:
        sources.setdefault(t["source"], []).append(t)
    if sources:
        A("### By decision maker")
        A("")
        A("| Source | Trades | Win rate | Net P/L |")
        A("|---|---|---|---|")
        for src, ts in sorted(sources.items()):
            s = _stats(ts)
            if s.get("n"):
                A(f"| {src} | {s['n']} | {s['win_rate'] * 100:.1f}% | {_fmt_money(s['net'])} |")
        A("")

    A("## Trade log")
    A("")
    A("| Entry (UTC) | Window | Source | Side | Fill | Edge | Outcome | P/L |")
    A("|---|---|---|---|---|---|---|---|")
    if trades:
        for t in trades:
            A(_trade_line(t))
    else:
        A("| - | - | - | - | - | - | - | - |")
    A("")

    A("## Window-by-window")
    A("")
    if not wins:
        A("_No windows captured yet._")
        A("")
    for w in wins:
        wid = w["window_id"]
        w_trades = by_window.get(wid, [])
        w_debates = debates_by_window.get(wid, [])
        header = f"### {iso(w['open_ts'])[11:16]}-{iso(w['close_ts'])[11:16]} UTC - `{wid}`"
        if w["result"]:
            header += f" - settled **{w['result'].upper()}**"
        A(header)
        A("")
        parts = [f"Strike **{float(w['strike'] or 0):,.2f}**", f"spot at open {float(w['spot_open'] or 0):,.2f}"]
        if w["spot_close"]:
            parts.append(f"spot at close {float(w['spot_close']):,.2f}")
        if w["final_value"]:
            parts.append(f"final reference {float(w['final_value']):,.2f}")
        A(" - ".join(parts))
        A("")
        if w_trades:
            for t in w_trades:
                d = db.get_debate(t["debate_id"]) if t["debate_id"] else None
                A(
                    f"**Decision ({t['source']})** at {iso(t['entry_ts'])[11:19]} UTC: "
                    f"**{t['side']}** {t['contracts']} contracts @ {t['entry_price']:.2f} "
                    f"(${float(t['cost']):,.2f}, fee ${float(t['entry_fee']):,.2f}), "
                    f"edge {float(t['edge'] or 0) * 100:+.1f}c."
                )
                A("")
                if d:
                    A("<details><summary>Committee debate</summary>")
                    A("")
                    try:
                        transcript = json.loads(d["transcript_json"])
                    except Exception:
                        transcript = []
                    for msg in transcript:
                        rnd = {1: "Opening", 2: "Rebuttal", 3: "Ruling"}.get(msg.get("round"), "Round")
                        p = msg.get("p_yes")
                        p_txt = f", P(yes)={p:.2f}" if p is not None else ""
                        A(
                            f"- **{msg.get('label')}** ({rnd}{p_txt}, conf {msg.get('confidence', 0):.2f}): "
                            f"{msg.get('text')}"
                        )
                    A("")
                    A("</details>")
                    A("")

                # the extracted path around the entry: spot, quote and model at each poll
                snaps = db.q(
                    "SELECT ts, spot, yes_mid, p_model, time_left FROM snapshots WHERE window_id=? ORDER BY ts",
                    (wid,),
                )
                if snaps:
                    picks = snaps[:: max(1, len(snaps) // 10)][:11]
                    A("<details><summary>Extracted market path</summary>")
                    A("")
                    A("| time (UTC) | spot | YES mid | model P(yes) | left |")
                    A("|---|---|---|---|---|")
                    for s in picks:
                        A(
                            f"| {iso(s['ts'])[11:19]} | {float(s['spot']):,.2f} | {float(s['yes_mid']):.2f} "
                            f"| {float(s['p_model'] or 0):.2f} | {float(s['time_left']):.0f}s |"
                        )
                    A("")
                    A("</details>")
                    A("")
                if t["status"] == "settled":
                    pnl = float(t["pnl"] or 0)
                    A(
                        f"**Result:** {t['result'].upper() if t['result'] else '-'} - "
                        f"P/L **{_fmt_money(pnl)}** - bankroll after ${db.ledger_rows()[-1]['balance']:,.2f}"
                    )
                    A("")
                elif t["status"] == "closed":
                    A(f"**Early exit** ({t['exit_reason']}) at {float(t['exit_price'] or 0):.2f} - P/L **{_fmt_money(float(t['pnl'] or 0))}**")
                    A("")
                else:
                    A("**Status:** open")
                    A("")
        else:
            if w_debates:
                first = w_debates[0]
                try:
                    verdict = json.loads(first["verdict_json"])
                    feats = json.loads(first["features_json"])
                except Exception:
                    verdict, feats = {}, {}
                reason = verdict.get("reason", "no trade")
                elapsed = float(feats.get("elapsed", 0) or 0)
                A(
                    f"**No trade (fast committee).** First checkpoint at +{elapsed:.0f}s: {reason}. "
                    f"{len(w_debates)} evaluation(s) this window; the desk stayed flat."
                )
                A("")
            else:
                A("_No decision recorded._")
                A("")

        for c in council_by_window.get(wid, []):
            try:
                payload = json.loads(c["payload"])
            except Exception:
                payload = {}
            verdict = payload.get("verdict", {})
            A(f"**LLM council review** ({iso(c['ts'])[11:19]} UTC) - {verdict.get('action', 'HOLD')}"
              + (f" {verdict.get('side')}" if verdict.get("side") else "")
              + (f": {verdict.get('reason')}" if verdict.get("reason") else ""))
            A("")
            for msg in payload.get("transcript", []):
                p = msg.get("p_yes")
                p_txt = f", P(yes)={p:.2f}" if p is not None else ""
                A(f"- **{msg.get('speaker') or msg.get('label')}**{p_txt}: {msg.get('text')}")
            A("")
    A("")
    A("## Method")
    A("")
    A("- **Data**: Kalshi public REST (`/trade-api/v2`) for market, order book and prints; BTC spot is a")
    A("  clustered consensus of Coinbase / Kraken / Binance sampled every 5s, with single-tick glitches")
    A("  rejected and a 3-tick median used for fair value so one noisy quote cannot swing the model.")
    A("- **Committee**: four directional desks (momentum, mean-reversion, quant, microstructure) each state")
    A("  P(settle YES); the risk officer can veto (spread, time left, price band, daily loss breaker, thin book,")
    A("  volatility regime break). Round 1 opening statements, round 2 rebuttals that adjust conviction, then")
    A("  the chair anchors on the quant fair value, adds a capped discretionary tilt, and shrinks the result")
    A("  5% toward 0.5 for model error.")
    A("- **Entry**: only when the fair value beats the *ask you can actually pay*, after the 7%-of-P(1-P) fee,")
    A("  by at least 4c; inside a 0.15-0.85 mid band; with >= 120s left; one position per window. The desk stands")
    A("  down when the quote is collapsing (>6c in 60s against the entry side), on a >3-sigma one-minute shock,")
    A("  or when the 'edge' exceeds 18c (that is a stale quote or a broken vol input, not free money).")
    A("- **Exit**: hold to settlement. A 400-window candle backtest of the same entries showed the")
    A("  stop/flip/time-stop rule set cut net P/L from +$567 to +$60 - early exits were selling binaries at")
    A("  local extremes on noisy model flips. Risk is bounded by the 1% stake, so positions ride to expiry.")
    A("  Trades taken before this change keep their historical exit reason in the log for comparison.")
    A("- **Fees**: `ceil(0.07 * contracts * price * (1 - price))` per side, as published by Kalshi.")
    A("")
    return "\n".join(lines) + "\n"


def write_csvs(db):
    trades = db.all_trades()
    with open(TRADES_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["id", "window_id", "source", "side", "entry_ts_utc", "entry_price", "contracts", "cost",
             "entry_fee", "exit_ts_utc", "exit_price", "exit_fee", "exit_reason", "status", "result",
             "payout", "pnl", "p_model_entry", "edge", "confidence", "debate_id"]
        )
        for t in trades:
            w.writerow([
                t["id"], t["window_id"], t["source"], t["side"], iso(t["entry_ts"]), t["entry_price"],
                t["contracts"], t["cost"], t["entry_fee"], iso(t["exit_ts"]), t["exit_price"],
                t["exit_fee"], t["exit_reason"], t["status"], t["result"], t["payout"], t["pnl"],
                round(float(t["p_model_entry"] or 0), 4), t["edge"], t["confidence"], t["debate_id"],
            ])
    with open(EQUITY_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_utc", "kind", "amount", "balance", "ref"])
        for r in db.ledger_rows():
            w.writerow([iso(r["ts"]), r["kind"], r["amount"], r["balance"], r["ref"]])


def live_state(db, cfg):
    trades = db.all_trades()
    stats = _stats(trades)
    open_trades = db.open_trades()
    last_snap = db.q1("SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1")
    current = None
    if last_snap:
        current = {k: last_snap[k] for k in last_snap.keys()}
    latest_debate = db.q1("SELECT * FROM debates ORDER BY ts DESC LIMIT 1")
    debate = None
    if latest_debate:
        try:
            debate = {
                "window_id": latest_debate["window_id"],
                "ts": latest_debate["ts"],
                "transcript": json.loads(latest_debate["transcript_json"]),
                "verdict": json.loads(latest_debate["verdict_json"]),
            }
        except Exception:
            debate = None
    return {
        "updated": __import__("time").time(),
        "bankroll": db.balance(),
        "start": float(cfg["bankroll_start"]),
        "stats": stats,
        "exposure": sum(float(t["cost"]) for t in open_trades),
        "open": [dict(t) for t in open_trades],
        "current": current,
        "debate": debate,
        "recent_trades": [dict(t) for t in trades[-15:]][::-1],
        "windows": db.q("SELECT COUNT(*) c FROM windows")[0]["c"],
    }


def write_live(db, cfg):
    state = live_state(db, cfg)
    with open(LIVE_JSON, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
    with open(BOARD_HTML, "w", encoding="utf-8") as fh:
        fh.write(_board_html(state))
    return state


def _board_html(s) -> str:
    stats = s["stats"]
    cur = s.get("current") or {}
    pnl = stats.get("net", 0.0)
    cls = "up" if pnl >= 0 else "down"

    open_rows = ""
    for t in s["open"]:
        open_rows += (
            f"<tr><td>{html.escape(t['window_id'])}</td><td><b>{t['side']}</b></td>"
            f"<td>{t['entry_price']:.2f}</td><td>{t['contracts']}</td><td>{t['source']}</td></tr>"
        )
    if not open_rows:
        open_rows = "<tr><td colspan=5 class='muted'>flat - waiting for an edge</td></tr>"

    trade_rows = ""
    for t in s["recent_trades"]:
        p = t["pnl"]
        p_txt = f"{float(p):+.2f}" if p is not None else "open"
        c = "up" if (p or 0) > 0 else ("down" if (p or 0) < 0 else "muted")
        trade_rows += (
            f"<tr><td>{html.escape(str(t['window_id']))}</td><td>{t['side']}</td>"
            f"<td>{t['entry_price']:.2f}</td><td>{t['contracts']}</td>"
            f"<td>{html.escape(str(t['result'] or t['status']))}</td>"
            f"<td class='{c}'>{p_txt}</td></tr>"
        )
    if not trade_rows:
        trade_rows = "<tr><td colspan=6 class='muted'>no trades yet</td></tr>"

    debate_html = ""
    if s.get("debate"):
        d = s["debate"]
        for m in d["transcript"]:
            p = m.get("p_yes")
            p_txt = f" P={p:.2f}" if p is not None else ""
            debate_html += (
                f"<div class='msg'><span class='who'>{html.escape(str(m.get('label')))}</span>"
                f"<span class='meta'>R{m.get('round')}{p_txt} conf {m.get('confidence', 0):.2f}</span>"
                f"<div>{html.escape(str(m.get('text')))}</div></div>"
            )

    cur_html = ""
    if cur:
        tl = float(cur.get("time_left") or 0)
        cur_html = f"""
        <div class='grid'>
          <div class='card'><div class='k'>Window</div><div class='v small'>{html.escape(str(cur.get('window_id')))}</div></div>
          <div class='card'><div class='k'>Spot</div><div class='v'>${float(cur.get('spot') or 0):,.2f}</div></div>
          <div class='card'><div class='k'>YES bid / ask</div><div class='v'>{float(cur.get('yes_bid') or 0):.2f} / {float(cur.get('yes_ask') or 0):.2f}</div></div>
          <div class='card'><div class='k'>Model P(yes)</div><div class='v'>{float(cur.get('p_model') or 0):.2f}</div></div>
          <div class='card'><div class='k'>Time left</div><div class='v'>{int(tl // 60)}m {int(tl % 60):02d}s</div></div>
        </div>"""

    return f"""<!doctype html>
<html><head><meta charset='utf-8'><meta http-equiv='refresh' content='15'>
<title>Kalshi BTC15M paper desk</title>
<style>
 body{{background:#0b0e14;color:#d7dce5;font:14px/1.45 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;margin:0;padding:28px}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#7b8496;font-size:12px;margin-bottom:20px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:14px 0 22px}}
 .card{{background:#121722;border:1px solid #1e2635;border-radius:10px;padding:12px 14px}}
 .k{{color:#7b8496;font-size:11px;text-transform:uppercase;letter-spacing:.08em}}
 .v{{font-size:22px;font-weight:650;margin-top:4px}} .v.small{{font-size:13px;font-weight:500}}
 table{{width:100%;border-collapse:collapse;margin:8px 0 22px;font-size:13px}}
 th{{text-align:left;color:#7b8496;font-weight:500;border-bottom:1px solid #1e2635;padding:6px 8px}}
 td{{padding:6px 8px;border-bottom:1px solid #141a26}}
 .up{{color:#3ddc97}} .down{{color:#ff6b81}} .muted{{color:#7b8496}}
 .msg{{border-left:2px solid #2a3448;padding:6px 0 6px 12px;margin:8px 0}}
 .who{{font-weight:650}} .meta{{color:#7b8496;font-size:11px;margin-left:8px}}
 h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:#7b8496;margin:24px 0 6px}}
</style></head><body>
<h1>Kalshi BTC15M &middot; paper desk</h1>
<div class='sub'>simulated fills at displayed book &middot; refreshes every 15s &middot; updated {html.escape(iso(s['updated']))}</div>
<div class='grid'>
  <div class='card'><div class='k'>Bankroll</div><div class='v'>${s['bankroll']:,.2f}</div></div>
  <div class='card'><div class='k'>Realized P/L</div><div class='v {cls}'>{pnl:+,.2f}</div></div>
  <div class='card'><div class='k'>Trades</div><div class='v'>{stats.get('n', 0)}</div></div>
  <div class='card'><div class='k'>Win rate</div><div class='v'>{stats.get('win_rate', 0) * 100:.0f}%</div></div>
  <div class='card'><div class='k'>Open exposure</div><div class='v'>${s['exposure']:,.2f}</div></div>
</div>
{cur_html}
<h2>Open positions</h2>
<table><tr><th>Window</th><th>Side</th><th>Entry</th><th>Qty</th><th>Source</th></tr>{open_rows}</table>
<h2>Recent decisions</h2>
<table><tr><th>Window</th><th>Side</th><th>Fill</th><th>Qty</th><th>Result</th><th>P/L</th></tr>{trade_rows}</table>
<h2>Latest committee debate</h2>
{debate_html or "<div class='muted'>no debate yet</div>"}
</body></html>"""


def build(db, cfg):
    md = render_journal(db, cfg)
    with open(JOURNAL_MD, "w", encoding="utf-8") as fh:
        fh.write(md)
    write_csvs(db)
    write_live(db, cfg)
    return md
