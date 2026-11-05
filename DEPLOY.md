# Deploy the BTC15M paper desk on Coolify

One container runs everything: the trading engine + a live-stats web server.
Kalshi market data is public, so **no API keys or secrets are required**.

```
┌─────────────────────── container ───────────────────────┐
│  engine thread ──►  scrypts market every ~12s, trades    │
│       │             paper money, writes SQLite           │
│       ▼                                                  │
│  /app/data/kalshi15m.db   (+ /app/journal, /app/logs)    │
│       ▲                                                  │
│  HTTP server :8000  /  /api/*  /healthz  /metrics        │
└──────────────────────────────────────────────────────────┘
              persistent volumes            Coolify proxy ──► https://desk.example.com
```

## 1. Push the repo somewhere Coolify can reach

Coolify deploys from a Git repository (GitHub/GitLab/Gitea or a public URL).
From this workspace:

```bash
git init
git add .
git commit -m "Kalshi BTC15M multi-agent paper desk"
git branch -M main
git remote add origin git@github.com:<you>/kalshi-btc15m-desk.git
git push -u origin main
```

`.gitignore` already excludes `data/`, `logs/`, `journal/` and `.env`, so only
code and deployment files go up. The Docker image only copies `kalshi15m/` +
`requirements.txt`, so the image stays small.

## 2. Create the Coolify resource

1. **New Resource → Docker Compose** (recommended) and pick the repo.
2. Base directory: `/` · Compose file: `docker-compose.yml`.
3. Coolify reads the service `desk` and its published port `8000`.
4. **Domains**: set `https://desk.<your-domain>` and point it at the `desk`
   service, port **8000**.
5. **Environment variables**: leave the defaults, or paste the ones you want from
   `.env.example` (they override `kalshi15m/config.json` without a rebuild).
6. **Deploy.**

> If your Coolify version does not auto-detect the port, either set the domain's
> port to `8000` manually or change `ports` in `docker-compose.yml` to match the
> host port you want (`${DESK_PORT:-8000}:8000`).

### Dockerfile-only alternative

If you prefer a single container instead of compose:

| Setting | Value |
|---|---|
| Build pack | Dockerfile |
| Dockerfile | `/Dockerfile` |
| Port | `8000` |
| Persistent storage | volume → `/app/data` |
| Healthcheck | `python -m kalshi15m.healthcheck` |

Mounting only `/app/data` is enough to survive redeploys (that is where the
SQLite database lives). `/app/journal` and `/app/logs` are regenerated if lost.

## 3. What you get

| URL | Purpose |
|---|---|
| `/` | live dashboard: bankroll, P/L, equity curve, open position, current window, recent trades/windows, latest committee ruling (auto-refresh 20s) |
| `/api/live` | full JSON state |
| `/api/summary` | small JSON for polling / phone widgets |
| `/api/windows?n=50` | recent windows + results |
| `/api/journal` | the full markdown journal |
| `/api/trades.csv`, `/api/equity.csv` | CSV exports |
| `/healthz` | 200 when the spot feed is fresh (<180s), 503 otherwise |
| `/metrics` | Prometheus-style text metrics |
| `/board` | compact board (the file you open locally) |

## 4. Logs and monitoring

- **Runtime logs**: Coolify → project → the `desk` service → **Logs**. Everything
  is printed to stdout (`engine`, `watchdog`, `http server listening`, trade
  lines, `ENTER`, `SETTLED`, `EXIT`). They are unbuffered.
- **Health**: Coolify's health check hits `/healthz` every 60s. If the spot feed
  goes stale for 3 minutes the container is marked unhealthy and restarted by the
  `restart: unless-stopped` policy.
- **Watchdog**: inside the container a supervisor restarts the engine thread if
  it ever exits, without restarting the container.
- **Metrics**: scrape `/metrics` if you add a monitoring stack later.

## 5. Persistence and updates

- `desk_data` volume → `/app/data` holds `kalshi15m.db` (all windows, snapshots,
  trades, ledger, debates). This is the only thing that must survive.
- Back it up with `docker run --rm -v desk_data:/data -v $PWD:/backup alpine tar czf /backup/desk-$(date +%F).tar.gz -C /data .`
  or Coolify's volume backup feature.
- **Deploying a new version**: push to the repo → Coolify → Redeploy. The volume
  is retained, the engine resumes from the database and re-settles any window
  that closed while it was down.

## 6. Tuning without a rebuild

All strategy knobs are environment variables (see `.env.example`):

| Var | Default | Meaning |
|---|---|---|
| `K15_BANKROLL` | 1000 | starting paper bankroll |
| `K15_RISK_PCT` | 0.005 | fraction of bankroll per trade |
| `K15_MAX_STAKE` | 10 | hard cap per position, USD |
| `K15_MIN_EDGE` | 0.04 | fee-adjusted edge required to enter |
| `K15_MAX_EDGE` | 0.18 | larger "edges" treated as data errors |
| `K15_MIN_PRICE` / `K15_MAX_PRICE` | 0.15 / 0.85 | tradeable mid band |
| `K15_MIN_TIME_LEFT` | 120 | no entries with less time left |
| `K15_EXIT_MODE` | hold | `hold` = to settlement, `stop` = legacy stop/flip exits |
| `K15_DAILY_LOSS_LIMIT` | 0.12 | circuit breaker |

## 7. Optional hardening

- The dashboard shows only paper-trading statistics, but if you want it private
  enable Coolify's **Basic Auth** on the resource (or put it behind a proxy).
- Pin the image by not changing the `python:3.12-slim` base tag.
- The container runs as uid 10001 (`desk`); volumes are created with the right
  ownership by the image.

## 8. Local smoke test

```bash
docker compose up --build
# then open http://localhost:8000  and  http://localhost:8000/healthz
```

Or without Docker:

```bash
python -m kalshi15m.entrypoint      # PORT=8000 default
```
