"""SQLite persistence for the BTC15M paper desk."""
from __future__ import annotations

import json
import os
import sqlite3
import time

from .util import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    window_id     TEXT PRIMARY KEY,
    market_ticker TEXT,
    open_ts       REAL,
    close_ts      REAL,
    strike        REAL,
    spot_open     REAL,
    spot_close    REAL,
    yes_open      REAL,
    yes_close     REAL,
    result        TEXT,
    final_value   REAL,
    settled_ts    REAL,
    status        TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS spot_ticks (
    ts    REAL,
    spot  REAL,
    src   TEXT,
    PRIMARY KEY (ts)
);
CREATE INDEX IF NOT EXISTS ix_spot_ts ON spot_ticks(ts);

CREATE TABLE IF NOT EXISTS snapshots (
    ts            REAL,
    window_id     TEXT,
    spot          REAL,
    yes_bid       REAL,
    yes_ask       REAL,
    yes_mid       REAL,
    spread        REAL,
    last          REAL,
    volume        REAL,
    oi            REAL,
    ob_imbalance  REAL,
    taker_flow    REAL,
    time_left     REAL,
    p_model       REAL,
    dist_sigma    REAL,
    PRIMARY KEY (ts, window_id)
);

CREATE TABLE IF NOT EXISTS debates (
    id           TEXT PRIMARY KEY,
    window_id    TEXT,
    ts           REAL,
    mode         TEXT,
    features_json TEXT,
    transcript_json TEXT,
    verdict_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_debates_win ON debates(window_id);

CREATE TABLE IF NOT EXISTS trades (
    id             TEXT PRIMARY KEY,
    window_id      TEXT,
    market_ticker  TEXT,
    source         TEXT,
    side           TEXT,
    entry_ts       REAL,
    entry_price    REAL,
    contracts      INTEGER,
    cost           REAL,
    entry_fee      REAL,
    exit_ts        REAL,
    exit_price     REAL,
    exit_fee       REAL,
    exit_reason    TEXT,
    status         TEXT,
    result         TEXT,
    payout         REAL,
    pnl            REAL,
    p_model_entry  REAL,
    p_final        REAL,
    confidence     REAL,
    edge           REAL,
    debate_id      TEXT,
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_win ON trades(window_id);

CREATE TABLE IF NOT EXISTS ledger (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL,
    kind    TEXT,
    amount  REAL,
    balance REAL,
    ref     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS council (
    id        TEXT PRIMARY KEY,
    window_id TEXT,
    ts        REAL,
    payload   TEXT
);
"""


class DB:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    # ---------- helpers ----------
    def q(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def q1(self, sql, params=()):
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql, params=()):
        self.conn.execute(sql, params)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ---------- meta ----------
    def get_meta(self, k, default=None):
        r = self.q1("SELECT v FROM meta WHERE k=?", (k,))
        return r["v"] if r else default

    def set_meta(self, k, v):
        self.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    # ---------- windows ----------
    def upsert_window(self, w: dict):
        self.x(
            """INSERT INTO windows(window_id, market_ticker, open_ts, close_ts, strike, status)
               VALUES(?,?,?,?,?,COALESCE(?, 'open'))
               ON CONFLICT(window_id) DO UPDATE SET
                 market_ticker=excluded.market_ticker,
                 open_ts=excluded.open_ts,
                 close_ts=excluded.close_ts,
                 strike=excluded.strike,
                 status=CASE WHEN windows.status='settled' THEN 'settled' ELSE excluded.status END""",
            (
                w["window_id"],
                w.get("market_ticker"),
                w.get("open_ts"),
                w.get("close_ts"),
                w.get("strike"),
                w.get("status"),
            ),
        )

    def get_window(self, window_id):
        return self.q1("SELECT * FROM windows WHERE window_id=?", (window_id,))

    def unsettled_windows(self, now):
        return self.q("SELECT * FROM windows WHERE status!='settled' AND close_ts < ? ORDER BY close_ts", (now,))

    # ---------- market data ----------
    def add_spot(self, ts, spot, src="median"):
        self.x("INSERT OR REPLACE INTO spot_ticks(ts,spot,src) VALUES(?,?,?)", (ts, spot, src))

    def recent_spots(self, since_ts):
        return self.q("SELECT ts, spot FROM spot_ticks WHERE ts>=? ORDER BY ts", (since_ts,))

    def add_snapshot(self, s: dict):
        self.x(
            """INSERT OR REPLACE INTO snapshots
               (ts,window_id,spot,yes_bid,yes_ask,yes_mid,spread,last,volume,oi,ob_imbalance,taker_flow,time_left,p_model,dist_sigma)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                s["ts"], s["window_id"], s["spot"], s["yes_bid"], s["yes_ask"], s["yes_mid"],
                s["spread"], s["last"], s["volume"], s["oi"], s["ob_imbalance"], s["taker_flow"],
                s["time_left"], s["p_model"], s["dist_sigma"],
            ),
        )

    def recent_snapshots(self, window_id, limit=200):
        return self.q(
            "SELECT * FROM snapshots WHERE window_id=? ORDER BY ts DESC LIMIT ?", (window_id, limit)
        )

    # ---------- debates ----------
    def add_debate(self, d: dict):
        self.x(
            "INSERT OR REPLACE INTO debates(id,window_id,ts,mode,features_json,transcript_json,verdict_json) VALUES(?,?,?,?,?,?,?)",
            (
                d["id"], d["window_id"], d["ts"], d.get("mode", "auto"),
                json.dumps(d.get("features", {}), default=str),
                json.dumps(d.get("transcript", []), default=str),
                json.dumps(d.get("verdict", {}), default=str),
            ),
        )

    def get_debate(self, did):
        return self.q1("SELECT * FROM debates WHERE id=?", (did,))

    def debates_for(self, window_id):
        return self.q("SELECT * FROM debates WHERE window_id=? ORDER BY ts", (window_id,))

    # ---------- trades ----------
    def add_trade(self, t: dict):
        self.x(
            """INSERT OR REPLACE INTO trades
               (id,window_id,market_ticker,source,side,entry_ts,entry_price,contracts,cost,entry_fee,
                status,result,payout,pnl,p_model_entry,p_final,confidence,edge,debate_id,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t["id"], t["window_id"], t["market_ticker"], t["source"], t["side"], t["entry_ts"],
                t["entry_price"], t["contracts"], t["cost"], t["entry_fee"], t.get("status", "open"),
                None, None, None, t.get("p_model_entry"), t.get("p_final"), t.get("confidence"),
                t.get("edge"), t.get("debate_id"), t.get("notes"),
            ),
        )

    def open_trade(self, window_id):
        return self.q1("SELECT * FROM trades WHERE window_id=? AND status='open'", (window_id,))

    def open_trades(self):
        return self.q("SELECT * FROM trades WHERE status='open'")

    def trades_for(self, window_id):
        return self.q("SELECT * FROM trades WHERE window_id=? ORDER BY entry_ts", (window_id,))

    def all_trades(self):
        return self.q("SELECT * FROM trades ORDER BY entry_ts")

    def mark_exit(self, trade_id, exit_ts, exit_price, exit_fee, reason, pnl):
        self.x(
            "UPDATE trades SET exit_ts=?, exit_price=?, exit_fee=?, exit_reason=?, status='closed', pnl=? WHERE id=?",
            (exit_ts, exit_price, exit_fee, reason, pnl, trade_id),
        )

    def settle_trade(self, trade_id, result, payout, pnl, p_final):
        self.x(
            "UPDATE trades SET status='settled', result=?, payout=?, pnl=?, p_final=? WHERE id=?",
            (result, payout, pnl, p_final, trade_id),
        )

    # ---------- ledger ----------
    def balance(self):
        r = self.q1("SELECT balance FROM ledger ORDER BY id DESC LIMIT 1")
        if r is None:
            from .util import load_json
            cfg = load_json(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), {})
            start = float(cfg.get("bankroll_start", 1000.0))
            self.x("INSERT INTO ledger(ts,kind,amount,balance,ref) VALUES(?,?,?,?,?)",
                   (time.time(), "init", start, start, "genesis"))
            return start
        return float(r["balance"])

    def add_ledger(self, ts, kind, amount, ref):
        bal = self.balance() + amount
        self.x("INSERT INTO ledger(ts,kind,amount,balance,ref) VALUES(?,?,?,?,?)", (ts, kind, amount, bal, ref))
        return bal

    def ledger_rows(self):
        return self.q("SELECT * FROM ledger ORDER BY id")

    # ---------- council (LLM) ----------
    def add_council(self, cid, window_id, ts, payload):
        self.x("INSERT OR REPLACE INTO council(id,window_id,ts,payload) VALUES(?,?,?,?)",
               (cid, window_id, ts, json.dumps(payload, default=str)))
