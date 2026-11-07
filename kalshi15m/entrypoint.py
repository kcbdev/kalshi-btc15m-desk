"""Container entrypoint: run the paper desk and the live-stats server together.

    python -m kalshi15m.entrypoint

The engine runs in its own thread (SQLite connections are per-thread), a watchdog
restarts it if it ever exits, and the HTTP server serves the dashboard in the main
thread. Logs go to stdout so Coolify's runtime log view shows them live.
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
import time
import traceback

from .engine import Engine, load_config
from .server import serve
from .util import DB_PATH, ROOT, Logger

# env -> (config key, cast)
_ENV_MAP = {
    "K15_BANKROLL": ("bankroll_start", float),
    "K15_RISK_PCT": ("risk_pct", float),
    "K15_MAX_STAKE": ("max_stake", float),
    "K15_MIN_EDGE": ("min_edge", float),
    "K15_MAX_EDGE": ("max_edge", float),
    "K15_MIN_PRICE": ("entry_min_price", float),
    "K15_MAX_PRICE": ("entry_max_price", float),
    "K15_MIN_CONFIDENCE": ("min_confidence", float),
    "K15_MIN_TIME_LEFT": ("min_time_left_s", int),
    "K15_DECISION_START": ("decision_start_s", int),
    "K15_DECISION_EVERY": ("decision_every_s", int),
    "K15_POLL_MARKET": ("poll_market_s", int),
    "K15_POLL_SPOT": ("poll_spot_s", int),
    "K15_EXIT_MODE": ("exit_mode", str),
    "K15_MAX_OPEN": ("max_open_positions", int),
    "K15_DAILY_LOSS_LIMIT": ("daily_loss_limit_pct", float),
}


def seed_if_empty(log=None) -> None:
    """On a fresh volume, start from the bundled snapshot so history is continuous."""
    seed = os.path.join(ROOT, "seed", "kalshi15m.db")
    if os.path.exists(DB_PATH) or not os.path.exists(seed):
        return
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        shutil.copy2(seed, DB_PATH)
        if log:
            log(f"seeded database from bundled snapshot ({os.path.getsize(DB_PATH) / 1024:.0f} KB)")
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"seed copy failed (continuing with empty db): {exc}")


def apply_env(cfg: dict, log=None) -> dict:
    for env, (key, cast) in _ENV_MAP.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        try:
            cfg[key] = cast(raw)
            if log:
                log(f"config override {key}={cfg[key]} (from {env})")
        except (TypeError, ValueError):
            if log:
                log(f"ignoring bad env {env}={raw!r}")
    return cfg


class Desk:
    """Owns the engine thread and restarts it if it dies."""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.engine = None
        self.thread = None
        self.running = True
        self.restarts = 0
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            self.thread = threading.Thread(target=self._run, name="engine", daemon=True)
            self.thread.start()
        self.log("engine thread started")

    def _run(self):
        try:
            # the DB connection must be created inside this thread
            self.engine = Engine(self.cfg, self.log)
            self.log("engine initialised")
            self.engine.run()
            self.log("engine run() returned")
        except BaseException:
            self.log("engine thread crashed: " + traceback.format_exc().replace("\n", " | "))

    def watchdog(self):
        while self.running:
            time.sleep(10)
            if self.thread and not self.thread.is_alive():
                self.restarts += 1
                self.log(f"watchdog: engine thread not alive, restarting (#{self.restarts})")
                self.start()

    def shutdown(self, *_a):
        self.running = False
        if self.engine:
            self.engine.running = False
        self.log("shutdown requested")


def main():
    log = Logger("desk")
    seed_if_empty(log)
    cfg = apply_env(load_config(), log)
    log(
        "boot: bankroll=%.2f risk=%.3f edge=%.2f-%.2f band=%.2f-%.2f exit=%s data=%s"
        % (
            cfg["bankroll_start"], cfg["risk_pct"], cfg["min_edge"], cfg["max_edge"],
            cfg["entry_min_price"], cfg["entry_max_price"], cfg.get("exit_mode"),
            os.environ.get("K15_DATA_DIR", "(default)"),
        )
    )
    log(f"python={sys.version.split()[0]} pid={os.getpid()}")

    desk = Desk(cfg, log)
    signal.signal(signal.SIGTERM, desk.shutdown)
    signal.signal(signal.SIGINT, desk.shutdown)

    desk.start()
    threading.Thread(target=desk.watchdog, name="watchdog", daemon=True).start()

    try:
        serve(port=int(os.environ.get("PORT", "8000")), cfg=cfg, log=log)
    finally:
        desk.shutdown()


if __name__ == "__main__":
    main()
