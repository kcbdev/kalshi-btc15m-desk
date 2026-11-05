"""Live-stats HTTP server for the BTC15M paper desk (stdlib only).

Endpoints:
  /                  dashboard (auto-refreshing)
  /board             compact live board
  /api/live          full live state (JSON)
  /api/summary       compact JSON for polling
  /api/windows?n=30  recent windows with results
  /api/journal       full JOURNAL.md
  /api/trades.csv    trade log
  /api/equity.csv    equity curve
  /healthz           health probe (200/503)
  /metrics           Prometheus-style text metrics
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .journal import render_journal
from .util import DB_PATH, iso

START_TS = time.time()

CSS = """
:root{color-scheme:dark}
body{background:#0b0e14;color:#d7dce5;font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif;margin:0;padding:26px 28px}
a{color:#7fb2ff}
h1{font-size:19px;margin:0 0 2px;letter-spacing:.01em}
.sub{color:#7b8496;font-size:12px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin:14px 0 22px}
.card{background:#121722;border:1px solid #1e2635;border-radius:10px;padding:12px 14px}
.k{color:#7b8496;font-size:10.5px;text-transform:uppercase;letter-spacing:.09em}
.v{font-size:21px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.v.small{font-size:13px;font-weight:520}
table{width:100%;border-collapse:collapse;margin:6px 0 22px;font-size:13px}
th{text-align:left;color:#7b8496;font-weight:500;border-bottom:1px solid #1e2635;padding:6px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
td{padding:6px 8px;border-bottom:1px solid #141a26;font-variant-numeric:tabular-nums}
.up{color:#3ddc97}.down{color:#ff6b81}.muted{color:#7b8496}
h2{font-size:11.5px;text-transform:uppercase;letter-spacing:.09em;color:#7b8496;margin:22px 0 4px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid #2a3448;color:#aeb8c9}
.msg{border-left:2px solid #2a3448;padding:6px 0 6px 12px;margin:7px 0;font-size:13px}
.who{font-weight:650}.meta{color:#7b8496;font-size:11px;margin-left:8px}
footer{color:#5f6a7d;font-size:11.5px;margin-top:26px;border-top:1px solid #1b2230;padding-top:12px}
"""


def _conn(db_path):
    con = sqlite3.connect(db_path, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _stats(con):
    rows = con.execute("SELECT status, pnl FROM trades").fetchall()
    closed = [r for r in rows if r["status"] in ("settled", "closed") and r["pnl"] is not None]
    pnls = [float(r["pnl"]) for r in closed]
    wins = [p for p in pnls if p > 0]
    return {
        "trades": len(closed),
        "open_trades": len([r for r in rows if r["status"] == "open"]),
        "wins": len(wins),
        "losses": len(pnls) - len(wins),
        "win_rate": (len(wins) / len(pnls)) if pnls else 0.0,
        "net": sum(pnls),
        "avg": (sum(pnls) / len(pnls)) if pnls else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
    }


def snapshot(db_path=DB_PATH):
    """One consistent read of everything the dashboard needs."""
    con = _conn(db_path)
    try:
        bal = con.execute("SELECT balance FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
        bankroll = float(bal["balance"]) if bal else 0.0
        ledger = list(con.execute("SELECT ts, amount, balance FROM ledger ORDER BY id"))
        st = _stats(con)
        open_trades = [dict(r) for r in con.execute("SELECT * FROM trades WHERE status='open'")]
        last_snap = con.execute("SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        cur = dict(last_snap) if last_snap else None
        last_spot = con.execute("SELECT ts, spot FROM spot_ticks ORDER BY ts DESC LIMIT 1").fetchone()
        wins = [dict(r) for r in con.execute(
            "SELECT window_id, open_ts, close_ts, strike, result, final_value, spot_open FROM windows "
            "ORDER BY open_ts DESC LIMIT 30")]
        trades = [dict(r) for r in con.execute(
            "SELECT window_id, side, entry_ts, entry_price, contracts, edge, status, result, exit_reason, pnl, source "
            "FROM trades ORDER BY entry_ts DESC LIMIT 25")]
        debate = con.execute("SELECT * FROM debates ORDER BY ts DESC LIMIT 1").fetchone()
        debate = dict(debate) if debate else None
        n_windows = con.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"]
        n_settled = con.execute("SELECT COUNT(*) c FROM windows WHERE status='settled'").fetchone()["c"]
        return {
            "now": time.time(),
            "bankroll": bankroll,
            "start": float(ledger[0]["amount"]) if ledger else 0.0,
            "stats": st,
            "ledger": [{"ts": r["ts"], "balance": float(r["balance"])} for r in ledger],
            "open": open_trades,
            "current": cur,
            "last_spot": dict(last_spot) if last_spot else None,
            "windows": wins,
            "trades": trades,
            "debate": debate,
            "n_windows": n_windows,
            "n_settled": n_settled,
        }
    finally:
        con.close()


# --------------------------------------------------------------------------- html
def _money(x):
    return f"{x:+,.2f}"


def _sparkline(ledger, w=680, h=90):
    pts = [p["balance"] for p in ledger if p["balance"] is not None]
    if len(pts) < 2:
        return "<div class='muted'>equity curve appears after the first settlement</div>"
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    step = w / (len(pts) - 1)
    coords = " ".join(f"{i * step:.1f},{h - (v - lo) / span * (h - 8) - 4:.1f}" for i, v in enumerate(pts))
    color = "#3ddc97" if pts[-1] >= pts[0] else "#ff6b81"
    return (
        f"<svg width='100%' viewBox='0 0 {w} {h}' preserveAspectRatio='none' style='height:{h}px'>"
        f"<polyline fill='none' stroke='{color}' stroke-width='2' points='{coords}'/></svg>"
        f"<div class='muted' style='font-size:11px'>start {pts[0]:,.2f} - low {lo:,.2f} - high {hi:,.2f} - now {pts[-1]:,.2f}</div>"
    )


def dashboard_html(state) -> str:
    st = state["stats"]
    cur = state.get("current") or {}
    net = st["net"]
    cls = "up" if net >= 0 else "down"
    pnl_pct = ((state["bankroll"] / state["start"]) - 1) * 100 if state["start"] else 0

    open_html = "<tr><td colspan=5 class='muted'>flat - waiting for an edge</td></tr>"
    if state["open"]:
        open_html = "".join(
            f"<tr><td>{t['window_id']}</td><td><b>{t['side']}</b></td><td>{t['entry_price']:.2f}</td>"
            f"<td>{t['contracts']}</td><td>{iso(t['entry_ts'])[11:19]}</td></tr>"
            for t in state["open"]
        )

    rows = []
    for t in state["trades"]:
        pnl = t["pnl"]
        pnl_txt = f"{float(pnl):+.2f}" if pnl is not None else "open"
        pnl_cls = "up" if (pnl or 0) > 0 else ("down" if (pnl or 0) < 0 else "muted")
        rows.append(
            f"<tr><td>{iso(t['entry_ts'])[11:19]}</td><td>{t['window_id']}</td><td>{t['side']}</td>"
            f"<td>{t['entry_price']:.2f} x {t['contracts']}</td>"
            f"<td>{t['result'] or t['exit_reason'] or t['status']}</td>"
            f"<td class='{pnl_cls}'>{pnl_txt}</td></tr>"
        )
    trade_html = "".join(rows) or "<tr><td colspan=6 class='muted'>no trades yet</td></tr>"

    win_html = "".join(
        f"<tr><td>{iso(w['open_ts'])[11:16]}</td><td>{w['window_id']}</td>"
        f"<td>{float(w['strike'] or 0):,.2f}</td><td>{float(w['final_value'] or 0):,.2f}</td>"
        f"<td><span class='pill'>{w['result'].upper() if w['result'] else 'open'}</span></td></tr>"
        for w in state["windows"]
    )

    debate_html = "<div class='muted'>no debate yet</div>"
    if state.get("debate"):
        try:
            tr = json.loads(state["debate"]["transcript_json"])
            debate_html = "".join(
                f"<div class='msg'><span class='who'>{m.get('label')}</span>"
                f"<span class='meta'>R{m.get('round')} conf {m.get('confidence', 0):.2f}</span>"
                f"<div>{m.get('text')}</div></div>" for m in tr[-4:]
            )
        except Exception:
            pass

    tl = float((cur or {}).get("time_left") or 0)
    cur_block = ""
    if cur:
        cur_block = f"""
        <div class='grid'>
          <div class='card'><div class='k'>window</div><div class='v small'>{cur.get('window_id')}</div></div>
          <div class='card'><div class='k'>spot</div><div class='v'>${float(cur.get('spot') or 0):,.0f}</div></div>
          <div class='card'><div class='k'>yes bid / ask</div><div class='v'>{float(cur.get('yes_bid') or 0):.2f} / {float(cur.get('yes_ask') or 0):.2f}</div></div>
          <div class='card'><div class='k'>model P(yes)</div><div class='v'>{float(cur.get('p_model') or 0):.2f}</div></div>
          <div class='card'><div class='k'>time left</div><div class='v'>{int(tl // 60)}m {int(tl % 60):02d}s</div></div>
        </div>"""

    spot_age = ""
    if state.get("last_spot"):
        age = int(time.time() - float(state["last_spot"]["ts"]))
        spot_age = f" &middot; spot {age}s ago"

    return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta http-equiv='refresh' content='20'><title>BTC15M paper desk - live</title>
<style>{CSS}</style></head><body>
<h1>Kalshi BTC15M &middot; multi-agent paper desk</h1>
<div class='sub'>simulated only &middot; updated {iso(time.time())}{spot_age} &middot; window {state['n_settled']}/{state['n_windows']} settled</div>
<div class='grid'>
  <div class='card'><div class='k'>bankroll</div><div class='v'>${state['bankroll']:,.2f}</div></div>
  <div class='card'><div class='k'>realized P/L</div><div class='v {cls}'>{net:+,.2f} <span style='font-size:12px'>({pnl_pct:+.2f}%)</span></div></div>
  <div class='card'><div class='k'>trades</div><div class='v'>{st['trades']}</div></div>
  <div class='card'><div class='k'>win rate</div><div class='v'>{st['win_rate'] * 100:.0f}%</div></div>
  <div class='card'><div class='k'>open</div><div class='v'>{st['open_trades']}</div></div>
</div>
{cur_block}
<h2>equity</h2>
{_sparkline(state['ledger'])}
<h2>open positions</h2>
<table><tr><th>window</th><th>side</th><th>entry</th><th>qty</th><th>since</th></tr>{open_html}</table>
<h2>recent trades</h2>
<table><tr><th>time</th><th>window</th><th>side</th><th>fill</th><th>outcome</th><th>P/L</th></tr>{trade_html}</table>
<h2>recent windows</h2>
<table><tr><th>open</th><th>window</th><th>strike</th><th>final</th><th>result</th></tr>{win_html}</table>
<h2>latest committee ruling</h2>
{debate_html}
<footer>JSON: <a href='/api/live'>/api/live</a> &middot; <a href='/api/summary'>/api/summary</a> &middot;
<a href='/api/windows'>/api/windows</a> &middot; <a href='/api/journal'>/api/journal</a> &middot;
<a href='/metrics'>/metrics</a> &middot; <a href='/healthz'>/healthz</a></footer>
</body></html>"""


# --------------------------------------------------------------------------- server
def reset_stats(db_path, bankroll, archive=True):
    """Archive the current run, then clear every stat and start a fresh bankroll."""
    archived = None
    if archive:
        archive_dir = os.path.join(os.path.dirname(db_path), "archive")
        os.makedirs(archive_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dst_path = os.path.join(archive_dir, f"kalshi15m-{stamp}.db")
        src = sqlite3.connect(db_path, timeout=20)
        dst = sqlite3.connect(dst_path)
        try:
            with dst:
                src.backup(dst)
            archived = os.path.basename(dst_path)
        finally:
            dst.close()
            src.close()

    con = sqlite3.connect(db_path, timeout=20)
    try:
        cleared = {}
        for table in ("trades", "debates", "council", "snapshots", "windows", "ledger", "meta"):
            cleared[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            con.execute(f"DELETE FROM {table}")
        now = time.time()
        con.execute(
            "INSERT INTO ledger(ts,kind,amount,balance,ref) VALUES(?,?,?,?,?)",
            (now, "init", bankroll, bankroll, "reset"),
        )
        con.execute(
            "INSERT INTO meta(k,v) VALUES('reset_epoch',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(now),),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True, "bankroll": bankroll, "archived": archived,
            "cleared": cleared, "reset_at": iso(now)}


class Handler(BaseHTTPRequestHandler):
    cfg = {}
    db_path = DB_PATH
    server_version = "btc15m-desk/1.0"

    def log_message(self, fmt, *args):  # keep Coolify logs clean
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, indent=2, default=str))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._route()
        except Exception as exc:  # noqa: BLE001
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self._route()
        except Exception as exc:  # noqa: BLE001
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def _route(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path in ("/", "/index.html"):
            st = snapshot(self.db_path)
            return self._send(200, dashboard_html(st), "text/html")
        if path == "/board":
            from .journal import _board_html, live_state
            from .db import DB
            db = DB(self.db_path)
            return self._send(200, _board_html(live_state(db, self.cfg)), "text/html")
        if path == "/admin/reset":
            if self.command != "POST":
                return self._json({"error": "POST required"}, 405)
            token = os.environ.get("K15_ADMIN_TOKEN", "")
            if not token or self.headers.get("X-Admin-Token", "") != token:
                return self._json({"error": "unauthorized"}, 401)
            out = reset_stats(self.db_path, float(self.cfg.get("bankroll_start", 1000.0)))
            return self._json(out)
        if path == "/api/live":
            return self._json(snapshot(self.db_path))
        if path == "/api/summary":
            st = snapshot(self.db_path)
            cur = st.get("current") or {}
            return self._json({
                "ts": iso(st["now"]),
                "bankroll": round(st["bankroll"], 2),
                "net": round(st["stats"]["net"], 2),
                "trades": st["stats"]["trades"],
                "win_rate": round(st["stats"]["win_rate"], 3),
                "open": len(st["open"]),
                "windows": st["n_windows"],
                "settled": st["n_settled"],
                "current_window": cur.get("window_id"),
                "spot": cur.get("spot"),
                "yes_bid": cur.get("yes_bid"),
                "yes_ask": cur.get("yes_ask"),
                "p_model": round(float(cur.get("p_model") or 0), 4) if cur else None,
                "time_left_s": round(float(cur.get("time_left") or 0)) if cur else None,
                "last_spot_age_s": (round(time.time() - float(st["last_spot"]["ts"])) if st.get("last_spot") else None),
            })
        if path == "/api/windows":
            n = int((qs.get("n") or ["30"])[0])
            return self._json(snapshot(self.db_path)["windows"][:n])
        if path == "/api/journal":
            from .db import DB
            db = DB(self.db_path)
            return self._send(200, render_journal(db, self.cfg), "text/markdown")
        if path in ("/api/trades.csv", "/api/equity.csv"):
            name = "trades.csv" if "trades" in path else "equity.csv"
            p = os.path.join(os.path.dirname(self.db_path), "..", "journal", name)
            p = os.path.normpath(p)
            if not os.path.exists(p):
                from .journal import write_csvs
                from .db import DB
                write_csvs(DB(self.db_path))
            with open(p, "r", encoding="utf-8") as fh:
                return self._send(200, fh.read(), "text/csv")
        if path == "/healthz":
            st = snapshot(self.db_path)
            age = None
            if st.get("last_spot"):
                age = time.time() - float(st["last_spot"]["ts"])
            ok = age is not None and age < 180
            return self._json({
                "status": "ok" if ok else "stale",
                "last_spot_age_s": round(age) if age is not None else None,
                "uptime_s": round(time.time() - START_TS),
                "bankroll": round(st["bankroll"], 2),
                "trades": st["stats"]["trades"],
                "current_window": (st.get("current") or {}).get("window_id"),
            }, 200 if ok else 503)
        if path == "/metrics":
            st = snapshot(self.db_path)
            age = (time.time() - float(st["last_spot"]["ts"])) if st.get("last_spot") else -1
            body = "\n".join([
                f"desk_bankroll {st['bankroll']:.2f}",
                f"desk_net_pnl {st['stats']['net']:.2f}",
                f"desk_trades_total {st['stats']['trades']}",
                f"desk_win_rate {st['stats']['win_rate']:.4f}",
                f"desk_open_positions {st['stats']['open_trades']}",
                f"desk_windows_total {st['n_windows']}",
                f"desk_windows_settled {st['n_settled']}",
                f"desk_last_spot_age_seconds {age:.0f}",
                f"desk_uptime_seconds {time.time() - START_TS:.0f}",
                "",
            ])
            return self._send(200, body, "text/plain")
        return self._json({"error": "not found", "path": path}, 404)


def serve(port=None, cfg=None, log=None):
    port = int(port or os.environ.get("PORT", "8000"))
    Handler.cfg = cfg or {}
    Handler.db_path = os.environ.get("K15_DB_PATH") or DB_PATH
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if log:
        log(f"http server listening on 0.0.0.0:{port} (db={Handler.db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return httpd
