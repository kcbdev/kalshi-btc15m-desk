"""Local test for the reset logic (no HTTP): copy the live DB, reset it, verify."""
import json
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kalshi15m.server import reset_stats, snapshot  # noqa: E402

tmp = os.path.join(ROOT, "data", "reset_test")
shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp, exist_ok=True)
db = os.path.join(tmp, "kalshi15m.db")
shutil.copy2(os.path.join(ROOT, "data", "kalshi15m.db"), db)

before = snapshot(db)
print("before:", before["bankroll"], "windows", before["n_windows"], "trades", before["stats"]["trades"])

out = reset_stats(db, 1000.0)
print("reset:", json.dumps(out["cleared"]), "| archived:", out["archived"])

after = snapshot(db)
print("after :", after["bankroll"], "windows", after["n_windows"], "trades", after["stats"]["trades"],
      "ledger_rows", len(after["ledger"]))

arch = os.path.join(tmp, "archive", out["archived"])
print("archive exists:", os.path.exists(arch), "size", os.path.getsize(arch) if os.path.exists(arch) else 0)

con = sqlite3.connect(db)
print("spot ticks kept:", con.execute("SELECT COUNT(*) FROM spot_ticks").fetchone()[0])
con.close()
shutil.rmtree(tmp, ignore_errors=True)
print("OK")
