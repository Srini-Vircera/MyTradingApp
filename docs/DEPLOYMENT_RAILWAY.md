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
 │ worker               ├──►│ PostgreSQL     │  audit trail + shared kill switch
 │ scheduler (optional) │   │ (Railway)      │  volume backups
 │ research / backtests │   └────────────────┘
 │ volume: /app/var     │
 └──────────────────────┘
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
  - Caddy adds HSTS, a strict Content-Security-Policy (`connect-src 'self'`, `frame-ancestors 'none'`) and the other security headers, redirects plain HTTP to HTTPS, and caps request bodies at 64 KB.
- **Shared kill switch.**
  - The API and the worker are separate containers, so the kill switch lives in PostgreSQL (`trading.kill_switch.store: database` in `config/production.yaml`).
  - STOP in the dashboard is seen by the worker's next check.
  - If the database cannot be read, the switch counts as **engaged** (fail closed).
- **One scheduler, ever.** The worker takes a PostgreSQL advisory lock before running the trading cycle. A second instance, such as a duplicate replica or an overlapping redeploy, waits up to 2 minutes and then refuses to start.
- **Only the worker keeps files** (`/app/var`): market data, backtest and research reports, and logs. Everything else is in PostgreSQL.

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
   | worker | `AQ_SCHEDULER_ENABLED` | `false` at first ([Runbook](#runbook)) |
   | worker | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
   | worker | `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | **Alpaca paper account keys only.** Sealed. Needed for data downloads and the scheduler |
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
  - Migrations are forward-only in production. A rollback to an older image works while the schema change is additive (all migrations so far are).
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
2. Deploy. The API migrates the database. The worker passes its checks and stays idle.
3. Open the dashboard domain and enter `AQ_API_TOKEN`. The banner shows **SHADOW MODE**. The kill switch shows **engaged** (fresh installation).
4. Run jobs in a shell on the worker (`railway ssh --service worker`), always through `aq-job` so files belong to the app user:
   ```bash
   aq-job data download ...
   aq-job backtest run ...
   aq-job research run ...
   aq-job kill-switch status
   ```
   Reports are written under `/app/var` on the worker volume.

**Shadow mode (orders computed and recorded, never sent)**
1. A person promotes strategies in `config/strategies.yaml` through a reviewed pull request. Software never does this.
2. Set `AQ_SCHEDULER_ENABLED=true` on `worker`, with the Alpaca **paper** keys. The worker restarts and runs `aq trade run`.
3. Release the kill switch from the dashboard (System Health → Re-enable trading…) once reconciliation and health look right.

**Moving to Alpaca paper trading (later)**
1. Complete the paper-readiness review.
2. Change `trading.mode: shadow` to `paper` in `config/production.yaml` through a reviewed pull request.
3. Deploy. Nothing else changes: same keys (paper), same checks. Live remains impossible.

**Stop trading immediately**
- In the dashboard: STOP AUTOMATED TRADING.
- If the dashboard is down: in a worker shell, run `aq-job kill-switch engage --actor NAME --reason TEXT`.
- Last resort: set `AQ_SCHEDULER_ENABLED=false` or remove the worker deployment.

**Rotate the API token:** change `AQ_API_TOKEN` on `api` and redeploy it. Open dashboard tabs are signed out on their next request.

**Roll back:** redeploy the previous successful deployment of the service from Railway's deployment list. The start-up checks and the scheduler lock still apply.

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

- **Backtests page is empty on Railway.** The dashboard's Backtests page lists report directories visible to the API container, and the API has no volume. Use `aq-job` on the worker and download reports from there. Storing report summaries in the database is a follow-up.
- **Restart the worker after a database restart.** A database restart cuts the scheduler's lock connection, and the lock is not re-taken until the worker restarts. With exactly one worker nothing competes for it in the meantime.
- **The paper-vs-backtest report is not built yet.** It needs recorded paper trading history, so it follows once paper trading runs.
