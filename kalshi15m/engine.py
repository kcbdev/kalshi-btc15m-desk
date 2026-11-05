"""The always-on paper-trading engine for Kalshi BTC15M.

Loop: poll spot -> poll window market/book/prints -> run the committee at
checkpoints -> manage exits -> settle at window close -> rewrite the journal.
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

from .api import Kalshi, KalshiError
from .committee import run_committee
from .db import DB
from .features import build_features, snapshot_row
from .journal import build as build_journal
from .paper import close_paper_trade, fee, open_paper_trade, settle_paper_trade
from .spot import SpotFeed, backfill_coinbase
from .util import DATA_DIR, DB_PATH, LOG_DIR, Logger, iso, load_json, now_ts, parse_kalshi_ts

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Engine:
    def __init__(self, cfg, log, db_path=DB_PATH):
        self.cfg = cfg
        self.log = log
        self.db = DB(db_path)
        self.api = Kalshi()
        self.spot = SpotFeed()
        self.active = None
        self.pending: list[dict] = []
        self.last_spot = 0.0
        self.last_market = 0.0
        self.last_decision = 0.0
        self.last_journal = 0.0
        self.last_settle_check = 0.0
        self.features = None
        self.running = True
        self.decisions_this_window = 0

    # ------------------------------------------------------------------ loop
    def run(self):
        self.log("engine start (paper trading, no real orders)")
        # window registration must not wait on a slow price feed, so warm up in the background
        threading.Thread(target=self._warm_up, daemon=True).start()
        # recover windows that closed while the engine was down
        for row in self.db.unsettled_windows(now_ts()):
            w = dict(row)
            self.pending.append(w)
            self.log(f"recovery: {w['window_id']} closed but unsettled - queued")
        while self.running:
            t0 = time.time()
            try:
                self.tick(now_ts())
            except Exception:
                self.log("tick error: " + traceback.format_exc().replace("\n", " | "))
            dt = time.time() - t0
            if dt > 5:
                self.log(f"slow tick: {dt:.1f}s")
            time.sleep(max(0.5, 2.0 - dt))

    def _warm_up(self):
        db = None
        try:
            db = DB(self.db.path)  # sqlite connections are per-thread
            n = backfill_coinbase(db, 40)
            self.log(f"spot backfill: {n} one-minute candles seeded" if n else "spot backfill: none added")
        except Exception:
            self.log("backfill failed: " + traceback.format_exc().replace("\n", " | "))
        finally:
            if db is not None:
                db.close()

    def stop(self, *_a):
        self.running = False
        self.log("engine stop requested")

    # ------------------------------------------------------------------ tick
    def tick(self, now: float):
        # The dashboard can wipe/reset the DB; when that happens, drop the
        # in-memory window state so everything reloads from scratch.
        epoch = self.db.get_meta("reset_epoch")
        if not hasattr(self, "_reset_epoch"):
            self._reset_epoch = epoch
        elif epoch != self._reset_epoch:
            self._reset_epoch = epoch
            self.active = None
            self.pending = []
            self.features = None
            self.last_decision = 0
            self.last_market = 0
            self.log("stats reset detected - reloading window state from scratch")

        self.ensure_window(now)

        if now - self.last_spot >= self.cfg["poll_spot_s"]:
            self.poll_spot(now)
            self.last_spot = now

        if self.active and now - self.last_market >= self.cfg["poll_market_s"]:
            self.poll_market(now)
            self.last_market = now

        if self.active:
            self.maybe_decide(now)

        self.maybe_settle(now)

        if now - self.last_journal >= 60:
            self.refresh_journal(now)

    # ------------------------------------------------------------------ data
    def poll_spot(self, now):
        price, quotes = self.spot.fetch()
        if not price:
            return
        prev = getattr(self, "last_spot_price", None)
        prev_ts = getattr(self, "last_spot_ts", 0.0)
        # A single stale tick must not move the model; but a real move (all venues
        # agreeing after a gap or a regime change) must never be rejected forever.
        if prev and (now - prev_ts) < 300:
            jump = abs(price / prev - 1.0)
            if jump > 0.0015:
                vals = list(quotes.values())
                venues_agree = len(vals) >= 2 and (max(vals) - min(vals)) <= 50.0
                if not venues_agree:
                    self.log(f"spot glitch rejected {prev:,.2f} -> {price:,.2f} quotes={quotes}")
                    return
                self.log(
                    f"spot regime shift accepted {prev:,.2f} -> {price:,.2f} "
                    f"({jump * 100:+.2f}%), venues agree: {quotes}"
                )
        elif prev and (now - prev_ts) >= 300:
            self.log(f"spot feed resumed after {now - prev_ts:.0f}s gap at {price:,.2f}")
        self.db.add_spot(now, price, "consensus")
        self.last_spot_price = price
        self.last_spot_ts = now

    def ensure_window(self, now):
        if self.active and now >= float(self.active["close_ts"]):
            self.log(
                f"window closed: {self.active['window_id']} (spot {getattr(self, 'last_spot_price', 0):,.2f})"
            )
            self.pending.append(self.active)
            self.active = None
            self.features = None
            self.last_decision = 0
            self.decisions_this_window = 0
            self.refresh_journal(now, force=True)

        if self.active:
            return

        try:
            markets = self.api.open_btc15m_markets(self.cfg["series_ticker"])
        except KalshiError as exc:
            self.log(f"window lookup failed: {exc}")
            return

        candidates = []
        for m in markets:
            open_ts = parse_kalshi_ts(m.get("open_time"))
            close_ts = parse_kalshi_ts(m.get("close_time"))
            if not open_ts or not close_ts:
                continue
            if open_ts <= now < close_ts:
                candidates.append((m, open_ts, close_ts))
        if not candidates:
            return

        m, open_ts, close_ts = sorted(candidates, key=lambda x: x[2])[0]
        window = {
            "window_id": m["event_ticker"],
            "market_ticker": m["ticker"],
            "open_ts": open_ts,
            "close_ts": close_ts,
            "strike": float(m.get("floor_strike") or 0.0),
        }
        self.db.upsert_window(window)
        self.active = window
        self.features = None
        self.log(
            f"window {window['window_id']} {iso(open_ts)[11:16]}-{iso(close_ts)[11:16]}Z "
            f"strike {window['strike']:,.2f} market {window['market_ticker']}"
        )
        self.refresh_journal(now, force=True)

    def poll_market(self, now):
        w = self.active
        try:
            market = self.api.market(w["market_ticker"])
            book = self.api.orderbook(w["market_ticker"])
            trades = self.api.trades(w["market_ticker"], limit=200, min_ts=int(now - 300))
        except KalshiError as exc:
            self.log(f"market poll failed: {exc}")
            return

        f = build_features(self.db, w, market, book, trades, now)
        if not f:
            return
        f["last"] = float(market.get("last_price_dollars") or 0.0)
        self.features = f
        self.db.add_snapshot(snapshot_row(f))

        # persist first/last observations on the window row
        row = self.db.get_window(w["window_id"])
        updates = {}
        if row and row["spot_open"] is None:
            updates["spot_open"] = f["spot"]
        if row and row["yes_open"] is None:
            updates["yes_open"] = f["yes_mid"]
        for k, v in updates.items():
            self.db.x(f"UPDATE windows SET {k}=? WHERE window_id=?", (v, w["window_id"]))

        self.manage_exits(now, f)

    # ------------------------------------------------------------------ decisions
    def council_lock_active(self, window_id, now) -> bool:
        """If the LLM council reserved this window, the fast committee stands down."""
        path = os.path.join(DATA_DIR, "council_lock", f"{window_id}.json")
        lock = load_json(path, {}) or {}
        try:
            return float(lock.get("until", 0)) > now
        except (TypeError, ValueError):
            return False

    def maybe_decide(self, now):
        w = self.active
        f = self.features
        if not f:
            return
        elapsed = now - float(w["open_ts"])
        if elapsed < self.cfg["decision_start_s"]:
            return
        if f["time_left"] <= self.cfg["min_time_left_s"]:
            return
        if now - self.last_decision < self.cfg["decision_every_s"]:
            return
        if self.db.open_trade(w["window_id"]):
            return
        if len(self.db.open_trades()) >= self.cfg["max_open_positions"]:
            return
        if self.council_lock_active(w["window_id"], now):
            return

        self.last_decision = now
        ctx = {
            "cfg": self.cfg,
            "bankroll": self.db.balance(),
            "day_pnl": self.day_pnl(now),
            "open_position": None,
        }
        result = run_committee(f, ctx, self.cfg)
        verdict = result["verdict"]
        did = f"D-{w['window_id']}-{int(now)}"
        self.db.add_debate(
            {
                "id": did,
                "window_id": w["window_id"],
                "ts": now,
                "mode": "auto",
                "features": f,
                "transcript": result["transcript"],
                "verdict": verdict,
            }
        )
        self.decisions_this_window += 1

        if verdict["action"] == "ENTER":
            trade = open_paper_trade(self.db, w, verdict, "auto_committee", self.cfg, debate_id=did)
            self.log(
                f"ENTER {verdict['side']} {verdict['contracts']} @ {verdict['price']:.2f} "
                f"edge {verdict['edge']:+.3f} p={verdict['p_yes']:.2f} conf {verdict['confidence']:.2f} "
                f"window {w['window_id']} ({verdict['reason']})"
            )
            self.refresh_journal(now, force=True)
        else:
            self.log(f"hold: {verdict['reason']} (window {w['window_id']})")

    def manage_exits(self, now, f):
        trade = self.db.open_trade(self.active["window_id"])
        if not trade:
            return
        # Backtest over 400 settled windows: holding to settlement is worth
        # +$567 while the stop/flip rule set cut that to +$60. Early exits were
        # selling binaries at local extremes on noisy model flips.
        if self.cfg.get("exit_mode", "hold") == "hold":
            return
        side = trade["side"]
        entry = float(trade["entry_price"])
        mark = f["yes_mid"] if side == "YES" else 1.0 - f["yes_mid"]
        exit_px = f["yes_bid"] if side == "YES" else max(0.01, 1.0 - f["yes_ask"])

        reason = None
        if mark <= entry * self.cfg["stop_loss_frac"]:
            reason = f"hard stop: mark {mark:.2f} <= {entry * self.cfg['stop_loss_frac']:.2f}"
        elif side == "YES" and f["p_model"] < 0.30 and f["time_left"] > self.cfg["exit_min_time_left_s"]:
            reason = f"model flip: P(yes) {f['p_model']:.2f} < 0.30"
        elif side == "NO" and f["p_model"] > 0.70 and f["time_left"] > self.cfg["exit_min_time_left_s"]:
            reason = f"model flip: P(yes) {f['p_model']:.2f} > 0.70"
        elif f["time_left"] <= self.cfg["exit_min_time_left_s"] and mark < entry:
            reason = f"time stop on a losing position: {f['time_left']:.0f}s left, mark {mark:.2f} < entry {entry:.2f}"

        if reason:
            res = close_paper_trade(self.db, trade, exit_px, reason, self.cfg)
            self.log(f"EXIT {side} {trade['contracts']} @ {exit_px:.2f} ({reason}) pnl {res['pnl']:+.2f}")
            self.refresh_journal(now, force=True)

    # ------------------------------------------------------------------ settlement
    def maybe_settle(self, now):
        if not self.pending:
            return
        if now - self.last_settle_check < self.cfg["settlement_poll_s"]:
            return
        self.last_settle_check = now

        keep = []
        for w in self.pending:
            if now < float(w["close_ts"]) + self.cfg["settlement_delay_s"]:
                keep.append(w)
                continue
            try:
                market = self.api.market(w["market_ticker"])
            except KalshiError as exc:
                self.log(f"settlement poll failed for {w['window_id']}: {exc}")
                keep.append(w)
                continue
            result = (market.get("result") or "").lower()
            if result not in ("yes", "no"):
                keep.append(w)
                continue
            final_value = float(market.get("expiration_value") or 0.0)
            self.finalize_window(w, result, final_value, now)
        self.pending = keep

    def finalize_window(self, w, result, final_value, now):
        last = self.db.q1(
            "SELECT * FROM snapshots WHERE window_id=? ORDER BY ts DESC LIMIT 1", (w["window_id"],)
        )
        spot_close = self.db.q1(
            "SELECT spot FROM spot_ticks WHERE ts<=? ORDER BY ts DESC LIMIT 1", (float(w["close_ts"]),)
        )
        self.db.x(
            "UPDATE windows SET result=?, final_value=?, spot_close=?, yes_close=?, settled_ts=?, status='settled' WHERE window_id=?",
            (
                result,
                final_value,
                float(spot_close["spot"]) if spot_close else None,
                float(last["yes_mid"]) if last else None,
                now,
                w["window_id"],
            ),
        )
        trade = self.db.open_trade(w["window_id"])
        if trade:
            res = settle_paper_trade(self.db, trade, result, final_value)
            self.log(
                f"SETTLED {w['window_id']} {result.upper()} final {final_value:,.2f} "
                f"{trade['side']} -> pnl {res['pnl']:+.2f} balance {res['balance']:,.2f}"
            )
        else:
            self.log(f"SETTLED {w['window_id']} {result.upper()} final {final_value:,.2f} (no position)")
        self.refresh_journal(now, force=True)

    # ------------------------------------------------------------------ misc
    def day_pnl(self, now):
        dt = datetime.fromtimestamp(now, tz=timezone.utc)
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rows = self.db.q("SELECT amount FROM ledger WHERE ts>=? AND kind IN ('settlement','trade_exit')", (start,))
        return sum(float(r["amount"]) for r in rows)

    def refresh_journal(self, now, force=False):
        if not force and now - self.last_journal < 60:
            return
        try:
            build_journal(self.db, self.cfg)
            self.last_journal = now
        except Exception:
            self.log("journal build failed: " + traceback.format_exc().replace("\n", " | "))

    def status(self):
        return {
            "active": self.active,
            "pending": [w["window_id"] for w in self.pending],
            "features": self.features,
            "balance": self.db.balance(),
        }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=0, help="run N ticks then exit (0 = forever)")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args(argv)

    faulthandler.enable()
    log = Logger("engine")
    log(f"boot pid={os.getpid()} argv={' '.join(sys.argv)}")
    eng = Engine(load_config(), log, db_path=args.db)
    signal.signal(signal.SIGINT, eng.stop)
    signal.signal(signal.SIGTERM, eng.stop)

    if args.ticks:
        for _ in range(args.ticks):
            eng.tick(now_ts())
            time.sleep(1)
        print(json.dumps(eng.status(), indent=2, default=str))
        return 0
    try:
        eng.run()
    except BaseException:
        log("FATAL: " + traceback.format_exc().replace("\n", " | "))
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
