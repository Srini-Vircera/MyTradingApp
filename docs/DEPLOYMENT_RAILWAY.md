# Deployment on Railway (Milestone 13)

The platform deploys as ordinary Docker images. Railway Pro is the reference
target; the same images run with `docker compose --profile stack` or on any
container host (see [Portability](#portability)).

**This deployment never trades real money.** It supports research,
backtesting, shadow mode and, later, Alpaca **paper** trading. Every container
runs `aq deploy check` before starting and refuses to run if:
- the mode is live, or live trading is enabled in the configuration;
- `AQ_LIVE_TRADING_CONFIRM` is set;
- the broker endpoint is not Alpaca paper.

## Architecture

```
                 HTTPS (Railway edge, automatic certificates)
                                   │
                          ┌────────▼─────────┐
    browser ─────────────►│ dashboard (Caddy)│  public domain, port 8080
                          │ static site      │  /healthz
                          │ /api/v1/* proxy  │
                          └────────┬─────────┘
                                   │ private network: api.railway.internal:8000
                          ┌────────▼─────────┐
                          │ api (FastAPI)    │  NO public domain
                          │ pre-deploy:      │  /api/v1/health/ready
                          │  aq db upgrade   │
                          └────────┬─────────┘
                                   │ postgres.railway.internal:5432
 ┌──────────────────────┐   ┌──────▼─────────┐
 │ worker (aq worker run)├─►│ PostgreSQL     │  audit trail, shared kill switch,
 │ jobs: data, backtests,│  │ (Railway)      │  job queue, uploads, results,
 │  research, broker chk │  └────────────────┘  runtime settings, control state
 │ scheduler (gated)     │
 │ volume: /app/var      │
 └───────────────────────┘
```

| Service | Image | Public? | Disk | Health check | Replicas |
|---|---|---|---|---|---|
| `Postgres` | Railway PostgreSQL template | no (see [manual list](#what-you-configure-in-railway)) | Railway volume | Railway | 1 |
| `api` | `deploy/docker/app.Dockerfile`, `AQ_ROLE=api` | **no** | none | `GET /api/v1/health/ready` | 1 |
| `worker` | same image, `AQ_ROLE=worker` | no | volume at `/app/var` | none (no HTTP) | **exactly 1** |
| `dashboard` | `deploy/docker/dashboard.Dockerfile` | **yes** (HTTPS) | none | `GET /healthz` | 1 |

**Why this shape**
- **Single origin.**
  - The browser only talks to the dashboard's HTTPS origin. Caddy serves the static dashboard and forwards `/api/v1/*` to the API over Railway's private network.
  - The API therefore has no public address, and no CORS is needed.
  - Caddy adds HSTS, a strict Content-Security-Policy (`connect-src 'self'`, `frame-ancestors 'none'`) and the other security headers, redirects plain HTTP to HTTPS, and caps request bodies at 64 KB — except `POST /api/v1/data/uploads` (CSV uploads), which allows 26 MB; the API itself enforces 25 MiB.
- **Shared kill switch.**
  - The API and the worker are separate containers, so the kill switch lives in PostgreSQL (`trading.kill_switch.store: database` in `config/production.yaml`).
  - STOP in the dashboard is seen by the worker's next check.
  - If the database cannot be read, the switch counts as **engaged** (fail closed).
- **One scheduler, ever.** The worker takes a PostgreSQL advisory lock before running the trading cycle. A second instance, such as a duplicate replica or an overlapping redeploy, waits up to 2 minutes and then refuses to start.
- **Only the worker keeps files** (`/app/var`): market data, backtest and research reports, and logs. Everything else is in PostgreSQL.
- **The browser controls the worker through PostgreSQL** ([CONTROL_PLANE.md](CONTROL_PLANE.md)). The API validates a request and queues a job row; the worker claims it, runs the shared service, and stores the result (dataset list, backtest summaries with the equity curve, research scorecards) back in PostgreSQL for the dashboard. CSV uploads are stored in PostgreSQL by the API and written to `/app/var` by the worker. No long task runs inside an HTTP request, and the API never needs the volume.
- **Two switches for the scheduler.** It runs only when `AQ_SCHEDULER_ENABLED=true` on the worker (the deployment master gate) **and** an operator has started it on the Trading Control page (audited; default stopped). Either one stops it.

## Files

| Path | Purpose |
|---|---|
| `deploy/docker/app.Dockerfile` | Python image for `api` and `worker`. Dependencies are pinned with hashes (`deploy/requirements.lock`), `tini` is checksum-pinned, it runs as uid 10001, and config is validated at build time |
| `deploy/docker/entrypoint.sh` | Role dispatch (`AQ_ROLE`), privilege drop, deploy check |
| `deploy/docker/aq-job.sh` | `aq-job <args>` runs `aq` as the app user (for one-off jobs in a shell) |
| `deploy/docker/dashboard.Dockerfile`, `deploy/docker/Caddyfile` | Static dashboard, reverse proxy, security headers |
| `deploy/railway/{api,worker,dashboard}.json` | Railway config-as-code: build, pre-deploy migration, health checks, restart policy, replicas |
| `docker-compose.yml` (profile `stack`) | The same topology locally |
| `scripts/db_backup.sh`, `scripts/db_restore.sh` | Off-platform logical backups and restore drills |

## What you configure in Railway

Everything not listed here is in the repository.

1. **Project.**
   - Create a project on the Pro plan and connect this GitHub repository.
   - Use one environment, `production`, and one region for every service. US East is closest to the exchange and to Alpaca.
2. **PostgreSQL.**
   - Add the PostgreSQL database service and keep its name `Postgres`: the variable references below use that name.
   - Under the database's backups, enable scheduled backups: daily and weekly.
   - Remove or disable its public TCP proxy unless you are taking an off-platform backup ([Backups](#backups-and-restore)).
3. **Three services from the same repository**, named exactly `api`, `worker` and `dashboard`.
   - For each, set the config-as-code path to `deploy/railway/<name>.json`.
   - Leave the root directory as the repository root.
   - Set the deploy branch you want.
   - Turn on "wait for CI" if you want deploys to follow green CI.
4. **Variables.** Use sealed variables for secrets.

   | Service | Variable | Value |
   |---|---|---|
   | api | `AQ_ROLE` | `api` |
   | api | `PORT` | `8000` |
   | api | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
   | api | `AQ_API_TOKEN` | a new random value of at least 32 characters, e.g. `openssl rand -hex 32`. **Sealed.** |
   | worker | `AQ_ROLE` | `worker` |
   | worker | `AQ_SCHEDULER_ENABLED` | `false` at first ([Runbook](#runbook)). The master gate: while `false` the scheduler never runs, whatever the dashboard says |
   | worker | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
   | worker | `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | **Alpaca paper account keys only.** Sealed. Needed for Alpaca data downloads, the broker check and the scheduler. Only the worker has them; the API and the browser never do |
   | worker | `POLYGON_API_KEY` | optional (if you use Polygon data). Sealed |
   | worker | `SMTP_USERNAME`, `SMTP_PASSWORD` | optional (email notifications, with `notifications.email` in YAML). Sealed |
   | dashboard | `PORT` | `8080` |
   | dashboard | `API_UPSTREAM` | `${{api.RAILWAY_PRIVATE_DOMAIN}}:8000` |

   - **Never set `AQ_LIVE_TRADING_CONFIRM`.** Its presence alone makes every container refuse to start.
   - `AQ_ENV=production` is already set in the image.
5. **Networking.**
   - `dashboard`: generate a Railway domain, or add your custom domain and its DNS record, with target port 8080. Railway issues the TLS certificate.
   - `api` and `worker`: **no public domain.** Private networking is on by default.
6. **Volume.** Attach one volume to `worker`, mounted at `/app/var`, and enable backups for it.
7. **Notifications (recommended).** Turn on Railway's deploy-failure and crash notifications, and a usage alert or limit for the project.

## Deploys, migrations and health

- **Build.** Railway builds each service from its Dockerfile (config-as-code), with the build context at the repository root. `watchPatterns` avoid rebuilding services whose files did not change.
- **Migrations.**
  - The `api` service's pre-deploy command, `aq db upgrade`, applies Alembic migrations and records the configuration version and strategy versions.
  - If it fails, the new deployment does not go live.
  - Only the API runs migrations.
  - Migrations are forward-only in production. A rollback to an older image works while the schema change is additive (all migrations so far are, including `0004_control_plane`).
- **Health checks.**
  - The API's `/api/v1/health/ready` returns 200 only when the database is reachable **and** at the head revision. There is no authentication and no detail in the response.
  - The dashboard's `/healthz` is Caddy itself.
  - Railway waits for these before switching traffic.
- **Start-up checks.** `aq deploy check --role api|worker` runs before every start. It lists every problem and exits 2, which fails the deployment.
- **Restarts.**
  - `ON_FAILURE`, at most 10 retries. A configuration error therefore stops after 10 attempts instead of looping forever.
  - SIGTERM is handled: tini forwards it, the scheduler finishes its current step, and uvicorn drains connections within `drainingSeconds`.
- **Replicas.**
  - All services run 1 replica.
  - The worker must stay at 1; the advisory lock enforces it anyway.
  - The API can scale later because its state is in PostgreSQL, but keep 1 for now.

## Logging

- **Format.** The application logs JSON lines to stdout/stderr (`logging.format: json`): UTC timestamps, level, event, `run_id` and `config_version` where known. Fields that look like secrets are redacted. Caddy logs JSON access lines.
- **Where to read them.** Railway collects them per service; use the log explorer to filter by service and level.
- **Nothing to rotate.** Nothing depends on local log files.
- **Audit trail.** Kill-switch changes, cycles, orders and notifications are in PostgreSQL, not only in logs.

## Backups and restore

| What | How | Where |
|---|---|---|
| PostgreSQL (primary) | Railway scheduled backups: daily + weekly | Railway |
| Worker volume (`/app/var`) | Railway volume backups. Data can be re-downloaded and reports regenerated, so it is convenience only | Railway |
| PostgreSQL (off-platform) | `DATABASE_URL=<public URL> scripts/db_backup.sh backups/`: custom-format dump, readability check, `.sha256`; never prints the URL | your machine or storage |

**Restore drill (do one before paper trading):**
1. Create an empty scratch database.
2. Run `TARGET_DATABASE_URL=... scripts/db_restore.sh backups/aq-<stamp>.dump`. It refuses a non-empty target, verifies the checksum and prints the schema revision.

**Disaster recovery:**
1. Restore into a new PostgreSQL service.
2. Point `DATABASE_URL` in `api` and `worker` at it.
3. Redeploy `api`; the pre-deploy command runs the migrations.
4. Check with `aq-job db status` on the worker.
5. The kill switch comes back in its backed-up state. If the backup has no state it is engaged. Review it before releasing.

## Runbook

**First deploy (research and backtesting only)**
1. Configure Railway as [above](#what-you-configure-in-railway), with `AQ_SCHEDULER_ENABLED=false`.
2. Deploy. The API migrates the database. The worker passes its checks, publishes the dataset list and waits for jobs (log line `worker: master gate OFF … jobs only`).
3. Open the dashboard domain and enter `AQ_API_TOKEN`. The banner shows **SHADOW MODE**. The kill switch shows **engaged** (fresh installation).
4. Work from the browser:
   - **Data**: upload a CSV (preview → Import) or queue a download; check validation and freshness. Without a `SYMBOL_actions.csv` the page warns that dividends are not included.
   - **Backtests**: choose strategies (e.g. `baseline_buy_hold`), dates, capital and costs → Run backtest. The result (metrics, equity and drawdown charts, benchmark comparison) appears when the worker finishes.
   - **Research**: start a run; scorecards appear in the page.
   - **Jobs**: every job, its progress and log.

   A shell is still available for administration: `railway ssh --service worker`, then `aq-job <command>` (e.g. `aq-job data download …`, `aq-job kill-switch status`).

**Shadow mode (orders computed and recorded, never sent)**
1. A person reviews the evidence and moves at least one strategy to `paper` (or `shadow`) in the **Strategy Manager** (justification + `APPROVE PROMOTION`; recorded in the governance ledger). Live approval is never available there.
2. Set `AQ_SCHEDULER_ENABLED=true` on `worker` (a deliberate deployment change) with the Alpaca **paper** keys. The worker restarts; the scheduler still does not run.
3. On **Trading Control**, press *Start Shadow Trading…* and type `START SHADOW TRADING`. The worker starts the cycle within ~15 s after its own checks (eligible strategies, database, single-scheduler lock).
4. Release the kill switch (System Health or Trading Control → Re-enable trading…) once reconciliation and health look right.

**Moving to Alpaca paper trading (simulated funds) — all from the browser**
1. Complete the paper-readiness review ([PAPER_TRADING.md](PAPER_TRADING.md)) and approve at least one strategy for paper in the Strategy Manager.
2. Settings → Trading (or Trading Control) → **Trading Mode: Paper** → *Verify Alpaca paper account (read-only)*. The worker checks the provider, the paper endpoint, the credentials, authentication, that the account is a paper account, broker and database reachability, the kill-switch state, stored market data and reconciliation. It only reads.
3. *Switch to PAPER trading…* and type `Switch to PAPER trading` (refused, with the reasons shown, unless the verification passed within the last hour and automation is stopped). The banner turns amber **PAPER TRADING — SIMULATED FUNDS**. Automation stays stopped and the kill switch unchanged.
4. Review the readiness checklist on Trading Control, release the kill switch when appropriate, then **Start Paper Trading** (`START PAPER TRADING`). Live remains impossible: there is no live broker adapter, and live mode is refused by the configuration overlay, `aq deploy check` and the broker factory.

**What still needs Railway configuration (one time, deliberately):** the Alpaca paper keys on the worker (`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`), and `AQ_SCHEDULER_ENABLED=true` on the worker to allow automation at all. Everything else — data, backtests, research, strategy approval, mode, verification, kill switch, start/stop — is done in the dashboard.

**Stop trading immediately**
- In the dashboard: STOP AUTOMATED TRADING.
- If the dashboard is down: in a worker shell, run `aq-job kill-switch engage --actor NAME --reason TEXT`.
- Stop the scheduler on Trading Control (never rate limited). Engaging the kill switch as well blocks new risk immediately.
- Last resort: set `AQ_SCHEDULER_ENABLED=false` (the worker stops the scheduler on its next round, and it cannot be restarted from the UI) or remove the worker deployment.

**Rotate the API token:** change `AQ_API_TOKEN` on `api` and redeploy it. Open dashboard tabs are signed out on their next request.

**Roll back:** redeploy the previous successful deployment of the service from Railway's deployment list. The start-up checks and the scheduler lock still apply.

**Upgrading to the control plane (PR #4) — one-time steps**
1. Merge; Railway rebuilds `api`, `worker` and `dashboard`.
2. `api`'s pre-deploy command applies migration `0004_control_plane` (new tables only: `control_jobs`, `control_job_logs`, `data_uploads`, `dataset_inventory`, `backtest_runs`, `runtime_config`, `runtime_config_changes`, `control_state`, `control_events`, `worker_heartbeats`; append-only and terminal-state triggers). No existing table changes.
3. The worker now starts `aq worker run` instead of `aq trade run`/idle. No variable changes are needed: `AQ_SCHEDULER_ENABLED` keeps meaning "the scheduler may run", and the new operator switch starts **stopped** — so a worker that previously ran the scheduler with `AQ_SCHEDULER_ENABLED=true` will **not** trade until someone presses Start on Trading Control. This is intentional.
4. Check: `/api/v1/health/ready` is 200; the dashboard shows the new pages; Jobs shows *worker online*; Data lists the existing datasets (the worker publishes them at start).

**Rolling back the control plane**
1. Redeploy the previous `api`, `worker` and `dashboard` deployments. The old code ignores the new tables, so the database does not need to change; the worker falls back to `aq trade run` gated by `AQ_SCHEDULER_ENABLED` alone.
2. Before rolling back the worker, stop the scheduler on Trading Control (or set `AQ_SCHEDULER_ENABLED=false`) if you do not want the old worker to start trading immediately.
3. Only if the tables must be removed (normally unnecessary): take a backup, then in a worker shell run
   `python -c 'import os; from adaptive_quant.persistence import migrate; from adaptive_quant.persistence.db import Database; migrate.downgrade(Database(os.environ["DATABASE_URL"]), "0003")'`.
   It drops only the control-plane tables (their history is lost). The migration's downgrade is covered by the migration round-trip test.

## Troubleshooting

**The dashboard's deploy logs say `AQ_ROLE must be api, worker or migrate`.**
- **What it means:** the dashboard service is running the Python api/worker image (`deploy/docker/app.Dockerfile`) instead of its own. The dashboard image (`deploy/docker/dashboard.Dockerfile`, Caddy) has no `AQ_ROLE` entrypoint, so it cannot print this message.
- **Confirm it:** open the dashboard service's build logs.
  - The right image builds `node:22-bookworm-slim` (`npm ci`, `npm run build`) and then `caddy:2.10-alpine`.
  - The wrong one builds `python:3.12-slim-bookworm` (`pip install --require-hashes`, `tini`).
- **Check, in the dashboard service's settings:**
  1. The config-as-code path must be `deploy/railway/dashboard.json`, not `api.json` or `worker.json`. The config file overrides the Dockerfile path shown in the settings.
  2. No `RAILWAY_DOCKERFILE_PATH` variable, including one inherited from shared or project variables, may point to `deploy/docker/app.Dockerfile`.
  3. The service source must be the repository, not a Docker image. If the service was made by duplicating `api` or `worker`, recheck all of its settings.
- **Then:** trigger a new build. Redeploying an old deployment reuses its old image.

## Portability

The same images and variables run anywhere Docker runs:

```bash
cp .env.example .env    # set POSTGRES_PASSWORD, AQ_API_TOKEN (>= 32 chars), optional paper keys
docker compose --profile stack up -d --build
open http://localhost:8080
```

- **What compose runs:** a one-shot `migrate` service, then `api`, which is healthy only when ready and has no published port. Then `worker`, with a named volume, and `dashboard` on `127.0.0.1:8080`.
- **Other hosts:** on another platform or Kubernetes, map the same roles:
  - Run `aq db upgrade` as a pre-start job.
  - Use the same health paths.
  - Put `/app/var` on a persistent volume for the worker.
  - Run exactly one worker.
  - Terminate TLS in front of Caddy.
- **Binding:** the entrypoint binds all interfaces, using IPv6 dual-stack where the kernel has IPv6 (Railway's private network) and IPv4 otherwise. `AQ_BIND_HOST` overrides it.

## Known limitations

- **Full HTML reports stay on the worker volume.** Runs started from the dashboard store their summary, metrics and equity curve in PostgreSQL; the full HTML report is written under `/app/var/reports` on the worker. Runs started with `aq-job backtest run` in a shell are not listed in the database.
- **Settings apply to the next job.** A running scheduler keeps the configuration it started with; Trading Control shows when a stop/start is needed.
- **Restart the worker after a database restart.** A database restart cuts the scheduler's lock connection, and the lock is not re-taken until the worker restarts. With exactly one worker nothing competes for it in the meantime.
- **The paper-vs-backtest report is not built yet.** It needs recorded paper trading history, so it follows once paper trading runs.
