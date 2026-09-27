# Operator API

Milestone 11, extended by the control plane (PR #4). Code: `src/adaptive_quant/api/`
(`app.py` read pages and the kill switch, `control.py` the control plane); schema:
[`apps/api/openapi.json`](../apps/api/openapi.json). Architecture:
[CONTROL_PLANE.md](CONTROL_PLANE.md).

The API gives views of the platform, the kill switch, and an **allowlisted control
plane**: it queues worker jobs (data, backtests, research, a read-only broker check),
accepts CSV uploads for validation, records strategy governance decisions up to
`shadow`, stores audited runtime settings and switches the scheduler and
shadow/paper mode. **It cannot place, change or cancel orders, enable live trading,
grant `live_approved`, bypass the scheduler master gate or read or change any
deployment secret.** Tests enforce this.

## Running

```bash
# .env: AQ_API_TOKEN=<at least 32 random characters>, DATABASE_URL=...
python -c 'import secrets; print(secrets.token_urlsafe(48))'   # generate a token
aq --env paper api serve        # http://127.0.0.1:8000/api/v1 (api.host / api.port)
aq api openapi > apps/api/openapi.json   # regenerate the committed schema
```

**Defaults:**
- The server binds to **localhost**. Expose it only behind TLS (M13).
- Without `DATABASE_URL` the database-backed pages return 503; configuration, strategies, system health and the kill switch still work.

## Security

| Control | Behaviour |
|---|---|
| Authentication | One operator bearer token (`Authorization: Bearer <AQ_API_TOKEN>`), compared in constant time. The server refuses to start with a missing or short token (`api.min_bearer_chars`, default 32). Failed attempts are logged without the supplied value. Only the probes `GET /api/v1/health/live` (process up) and `GET /api/v1/health/ready` (database reachable and migrated; 503 otherwise, no details) are public |
| Allowed mutations | An explicit allowlist, `MUTATING_ROUTES` in `api/app.py` (21 POST routes; listed below). Tests compare every route in the app and in the OpenAPI schema with a hard-coded copy of that list, so adding a mutation is a deliberate, reviewed change. There is no PUT, PATCH or DELETE |
| Every mutation | Bearer token; strict request model (`extra="forbid"`, enums, bounded lengths and ranges; a test walks the schema of every mutation body); a named `actor`; audited in `control_events` (accepted **and** refused); readable errors without internals or secrets; rate limited per client (jobs 30/min, uploads 10/10 min, control 20/min; `kill-switch/engage` and `trading/scheduler/stop` are never limited because stopping must always work) |
| CSRF | Not applicable by design: authentication is an explicit `Authorization` header (never a cookie), so a cross-site page cannot make an authenticated request; CORS allows only configured origins |
| Kill-switch confirmation | `engage` needs `confirm: "STOP AUTOMATED TRADING"`; `release` needs `confirm: "RE-ENABLE TRADING"`. Both need a named `actor` and a `reason`. Unknown fields are rejected. Actions are recorded in the kill-switch audit file and `kill_switch_events` (as `api:<actor>`) |
| Trading mode | `POST /trading/mode` switches **shadow ↔ paper only** (a `Literal` model; `live` is a 422). SHADOW → PAPER needs a successful read-only verification of the Alpaca **paper** account (every check passed, at most 60 minutes old) and stopped automation. PAPER → SHADOW never needs the broker and stops automation first (fail closed). Live mode is never available through the API (see [SAFETY.md](SAFETY.md) and [CONTROL_PLANE.md](CONTROL_PLANE.md#trading-mode-automation-kill-switch-and-the-master-gate)) |
| Secrets | Never accepted, stored or returned. The settings page lists which credentials the *worker* has configured (booleans from its heartbeat), never values; `AQ_API_TOKEN` is not listed at all |
| Import boundaries | The API package cannot import brokers, the order planner or manager, the trading cycle or the scheduler (static test) |
| Responses | `Cache-Control: no-store`, `nosniff`, `X-Frame-Options: DENY`, a restrictive CSP and `Referrer-Policy: no-referrer`. The configuration is redacted. Database problems return 503 without internals |
| CORS | Explicit origins only (`api.cors_origins`, default `http://localhost:3000`); `*` is refused by config validation |
| Docs UI | Disabled (no third-party assets); the schema is at `/api/v1/openapi.json` |
| Page size | Every list takes `limit` (1 .. `api.max_page_size`) |

## Endpoints (`/api/v1`)

| Page | Endpoint | Contents |
|---|---|---|
| Overview | `GET /overview` | Mode banner, kill switch, today's session and schedule (early close), latest cycle and its steps, latest performance, target weights, open-order count, last reconciliation |
| Portfolio | `GET /portfolio` | Latest account snapshot, broker and expected positions, target portfolio |
| Strategies | `GET /strategies` | Configured strategies with lifecycle, version, parameters, eligible modes and approval; lifecycle events |
| Signals | `GET /signals?cycle_id=` | Stored strategy signals |
| Risk | `GET /risk` | Latest risk decision (band, volatility scale, every adjustment), history, configured limits |
| Performance | `GET /performance` | Daily performance series |
| Backtests | `GET /backtests` | Stored backtest (`metrics.json`) and research (`research.json`) summaries |
| Orders | `GET /orders?state=&cycle_id=`, `GET /shadow-orders` | Order intents with their state history; shadow orders |
| Executions | `GET /executions` | Fills with expected price and slippage |
| Reconciliation | `GET /reconciliation` | Reconciliation reports (including acknowledgements) |
| Cycles | `GET /cycles`, `GET /cycles/{id}/explain` | Trading cycles and the full decision chain of one |
| System health | `GET /system/health` | Version, database reachability and schema revision, kill switch, latest cycle, recent errors and notifications |
| Configuration | `GET /configuration` | Redacted resolved settings, config version, warnings, live-trading lock (read-only) |
| Kill switch | `GET /kill-switch`, `POST /kill-switch/engage`, `POST /kill-switch/release` | Status, and the two confirmed actions |

Every page response includes a `banner` with environment, mode, `uses_real_money`, `live_trading_enabled`, config version, and a notice that simulated results are hypothetical.

### Control plane (`/api/v1`, all need the token)

| Area | Reads | Allowlisted writes (all `POST`) |
|---|---|---|
| Jobs | `GET /jobs?status=&job_type=`, `GET /jobs/{id}` (with log) | `/jobs/data/download`, `/jobs/data/validate`, `/jobs/data/synthesize`, `/jobs/data/inventory`, `/jobs/backtest`, `/jobs/research`, `/jobs/broker/verify`, `/jobs/{id}/cancel`, `/jobs/{id}/retry` (idempotent job types only) |
| Data | `GET /data/datasets`, `GET /data/options`, `GET /data/uploads`, `GET /data/uploads/{id}` | `/data/uploads` (raw CSV body), `/data/uploads/{id}/import`, `/data/uploads/{id}/discard` |
| Backtests | `GET /backtests/options`, `GET /backtests/runs`, `GET /backtests/runs/{job_id}` | (queued through `/jobs/backtest`) |
| Research | `GET /research/runs` | (queued through `/jobs/research`) |
| Strategies | `GET /strategies/manager` | `/strategies/{id}/params`, `/strategies/{id}/enabled`, `/strategies/{id}/lifecycle` |
| Settings | `GET /settings` | `/settings` |
| Trading | `GET /trading/control`, `GET /trading/live-readiness` | `/trading/scheduler/start`, `/trading/scheduler/stop`, `/trading/mode` |
| Audit | `GET /audit/events?action=` | — |

Confirmation phrases (checked exactly by the API): `START SHADOW TRADING`,
`START PAPER TRADING`, `Switch to PAPER trading`, `Switch to SHADOW mode`,
`TIGHTEN RISK LIMITS` (any risk-limit change), `APPROVE PROMOTION` (any lifecycle
promotion, which also needs a justification of at least 20 characters).

**Jobs.** A request only validates its parameters (a closed set of job types with
strict Pydantic models; see `control/jobs.py`) and inserts a row in `control_jobs`;
the worker executes it. An identical job that is already queued or running is
returned instead of a duplicate. Job rows never hold credentials; errors are
scrubbed of any configured secret value before they are stored.

**Uploads.** `POST /data/uploads?symbol=&kind=bars|actions&frequency=&filename=&actor=`
with the CSV as the body (`Content-Type: text/csv`, at most 25 MiB; the proxy allows
26 MB on this path only). The file name is a display hint and must be a plain
`*.csv` name (no `/`, `\`, `..`); the server decides where data is stored. The API
parses the file with the platform's own file provider and data validator in a
temporary directory, stores the bytes and the preview in PostgreSQL
(`data_uploads`), and returns the preview (row count, date range, head/tail rows,
problems, warnings such as ignored `div`/`split` columns or missing corporate
actions). Only a `validated` upload can be imported; import is a worker job.
