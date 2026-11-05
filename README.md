# Kalshi BTC15M - multi-agent paper-trading desk

A live, always-on paper-trading desk for Kalshi's **BTC 15-minute** markets
(series `KXBTC15M`). It extracts the current 15-minute window, runs a structured
debate between independent decision agents, places simulated trades, and writes a
verifiable journal with entry time/price and the settled outcome/P&L.

**No real orders are ever sent. No money is at risk.**

## What it does

| Piece | File | Notes |
|---|---|---|
| Market data | `kalshi15m/api.py` | Kalshi public REST `/trade-api/v2` - market, order book, prints. No auth. |
| Spot feed | `kalshi15m/spot.py` | Clustered consensus of Coinbase / Kraken / Binance every 5s; rejects single-tick glitches; 1-minute backfill on boot. |
| Features | `kalshi15m/features.py` | Realized vol (fast 15s + slow 1m blend), distance to strike in sigma on a 3-tick median spot, book imbalance, taker flow, quote momentum. |
| Committee | `kalshi15m/agents.py`, `committee.py` | 4 directional desks + risk officer, 2 debate rounds + chair ruling, fee-adjusted edge, risk vetoes. |
| Paper execution | `kalshi15m/paper.py` | Fills at the displayed ask, Kalshi quadratic fee schedule, settlement, bankroll ledger. |
| Engine | `kalshi15m/engine.py` | 2s loop: poll, decide on checkpoints, hold to settlement, settle at window close. |
| Journal | `kalshi15m/journal.py` | `journal/JOURNAL.md`, `trades.csv`, `equity.csv`, `live.json`, `board.html`. |
| Operator CLI | `kalshi15m/cli.py` | `status`, `context`, `enter`, `exit`, `note`, `lock`, `journal`, `report`. |
| Backtest | `scripts/backtest.py`, `scripts/sweep.py` | Replays the desk against 400 settled windows using Kalshi's own 1-minute bid/ask candles. |

## The market

Each 15-minute window is a binary contract:

> YES if the 60-second average of CF Benchmarks' BRTI just before the close is
> **at or above** the 60-second average just before the open.

That opening reference is the listed `floor_strike`. So it is effectively
"is BTC up over the next 15 minutes", and YES/NO pay $1/$0 at settlement.

## The committee

Each desk states P(settle YES):

- **Momentum** - 1/3/5-minute spot returns in sigma units.
- **Mean-reversion** - distance from the 10-minute mean, faded.
- **Quant** - Brownian fair value `Phi(log(spot/strike) / sigma_remaining)` with a
  fast/slow realized-vol blend and a very small momentum drift.
- **Microstructure** - order-book imbalance and 2-minute taker flow.
- **Risk officer** - no direction; can veto on spread, time left, price band,
  daily-loss circuit breaker, thin book, vol regime break, and one-minute shocks.

Round 1: opening statements. Round 2: each desk reads the room and revises its
conviction. Round 3: the chair anchors on the quant fair value, adds a capped
discretionary tilt, shrinks 5% toward 0.5, and requires a **fee-adjusted** edge of
at least 4c against the ask you can actually pay.

An LLM council can also review a window (see `data/council/`); its debate and
verdict are recorded in the journal alongside the fast committee's.

## The journal

`journal/JOURNAL.md` is rewritten from the database on every event:

- bankroll, win rate, drawdown, fees, average entry edge;
- a trade log with entry time, fill, edge, outcome and P/L;
- a window-by-window section with the full committee transcript and the settled
  result, plus the council review when one ran.

`journal/board.html` is a self-refreshing live board for the browser.

## Backtest, and why the desk holds to settlement

`scripts/backtest.py` replays the desk's rules against the last 400 settled
windows using Kalshi's per-minute candle bid/ask (real prices you could have paid)
and Coinbase 1-minute BTC candles for spot and realized vol.

The first version looked spectacular (+195%) until the look-ahead bug was found:
Coinbase keys candles by bucket *start*, so using the bucket containing the
decision time leaked up to 60 seconds of future prices. After the fix:

| Exit policy (same entries) | Trades | Net P/L | Win rate | Max DD |
|---|---|---|---|---|
| Hard stop 55% + model flip + time stop | 302 | +$59.75 | 30.1% | $294.93 |
| **Hold to settlement** | 267 | **+$702.42** | 41.6% | $382.97 |

Early exits were selling binaries at local extremes on noisy model flips, so the
live desk now holds to settlement and bounds risk with a 1% stake instead.
`scripts/sweep.py` re-runs the comparison for other thresholds.

## Verify it

```powershell
python -m kalshi15m.cli status      # live window + desk state
python scripts/audit.py             # recompute every P/L and check the settlement rule
python -m kalshi15m.cli journal     # rebuild JOURNAL.md from the database
python scripts/backtest.py --windows 400
```

`scripts/audit.py` independently re-derives:

1. the ledger running balance,
2. `cost = contracts * price` and the Kalshi fee for every fill,
3. settlement payouts and P/L,
4. that each window's recorded result matches `final_value >= strike`,
5. the one-position-per-window rule.

## Run it

```powershell
# engine (restarts itself if it exits)
python -u -m kalshi15m.engine

# open the live board
Start-Process journal\board.html
```

## Files

```
kalshi15m/          engine package
data/               sqlite db, raw json, council context/locks, backtest cache
journal/            JOURNAL.md, trades.csv, equity.csv, live.json, board.html
logs/               engine.log, supervisor.log
scripts/            audit.py, backtest.py, sweep.py, run_desk.ps1, helpers
```
