"""Small shared helpers: time, math, json, logging."""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone

ROOT = os.environ.get("K15_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("K15_DATA_DIR") or os.path.join(ROOT, "data")
JOURNAL_DIR = os.environ.get("K15_JOURNAL_DIR") or os.path.join(ROOT, "journal")
LOG_DIR = os.environ.get("K15_LOG_DIR") or os.path.join(ROOT, "logs")

for _d in (DATA_DIR, JOURNAL_DIR, LOG_DIR, os.path.join(DATA_DIR, "raw")):
    os.makedirs(_d, exist_ok=True)

DB_PATH = os.environ.get("K15_DB_PATH") or os.path.join(DATA_DIR, "kalshi15m.db")


def now_ts() -> float:
    return time.time()


def iso(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmm(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def norm_cdf(x: float) -> float:
    try:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    except Exception:
        return 0.5


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-min(x, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(x, -60.0))
    return z / (1.0 + z)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def safe_float(v, default=0.0):
    try:
        if v in (None, ""):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def load_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def dump_json(path: str, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
    os.replace(tmp, path)


class Logger:
    def __init__(self, name: str):
        self.path = os.path.join(LOG_DIR, f"{name}.log")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def __call__(self, msg: str):
        line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z {msg}"
        print(line, flush=True)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def parse_kalshi_ts(s: str | None) -> float | None:
    """Parse '2026-11-04T22:15:00Z' (or with fractional seconds) into epoch seconds."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None
