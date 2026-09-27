# Control plane

How the dashboard controls the platform without ever running a long task in an HTTP
request, reaching a broker from the API, or making real-money trading possible.

```
browser ──HTTPS──> dashboard (Caddy) ──/api/v1──> api ──SQL──> PostgreSQL <──SQL── worker
                                                  │                                 │
                                    validate, queue job,                  claim job (SKIP LOCKED),
                                    audit, read results                   run the shared service,
                                                                          store results, heartbeat
```

* **The API** validates requests, writes rows (jobs, uploads, runtime settings,
  control state, audit events) and reads results. It never touches `/app/var`,
  never imports a broker, the order manager or the trading cycle (static test),
  and never reads a secret other than its own token and `DATABASE_URL`.
* **The worker** (`aq worker run`, the `worker` Railway service) owns `/app/var`
  (market data, reports). It executes jobs one at a time and supervises the
  trading scheduler in a separate thread.
* **PostgreSQL** is the only channel between them.

## Services shared by the CLI and the worker

The orchestration that used to live in the CLI modules now lives in
`src/adaptive_quant/services/`; the CLI and the worker call the same functions,
so there is one implementation of each workflow:

| Service | Used by | What it does |
|---|---|---|
| `services/data.py` | `aq data …`, data jobs, uploads | download/update, validate, freshness, synthetic history, dataset inventory, CSV upload preview/import |
| `services/backtests.py` | `aq backtest run`, backtest jobs | request → the one backtest engine → report + JSON summary |
| `services/research.py` | `aq research run`, research jobs | the full research pipeline → report + scorecards |
| `services/strategies.py` | Strategy Manager | parameter schemas, catalogue rows, governed lifecycle moves |
| `services/runtime.py` | API, worker, scheduler | the effective configuration (YAML + audited overlay) |

The quantitative engines are unchanged except for one guarded addition: a
backtest may run **without TQQQ/SQQQ price data** when the selected strategies can
provably never hold them (long-only, at most 1× exposure, an allocation mode that
uses QQQ up to 1×, and volatility targeting that can only reduce exposure). The
engine also checks at run time and stops the backtest with an error if an
allocation would ever put weight on an instrument without prices. This is what
lets `baseline_buy_hold` run on a QQQ-only dataset; results say which instruments
were not loaded.

## Jobs

Table `control_jobs` (migration `0004`): id, type, validated parameters, status
(`queued` → `running` → `succeeded` | `failed` | `cancelled`), requested by,
requested/started/finished/heartbeat times, progress and message, result summary,
scrubbed error, the `config_version` used, `retry_of`, `cancel_requested`.
Log lines are in the append-only `control_job_logs`.

* **Closed catalogue** (`control/jobs.py`): `data.download`, `data.validate`,
  `data.synthesize`, `data.inventory`, `data.import_upload`, `backtest.run`,
  `research.run`, `broker.verify`. Parameters are strict Pydantic models
  (`extra="forbid"`, enums, bounds). There is no generic or shell job.
* **Claiming**: `SELECT … FOR UPDATE SKIP LOCKED` on the oldest queued job; only
  the claiming worker can heartbeat or finish it. A database trigger makes terminal
  states final and forbids moving a running job back to queued.
* **Cancel**: a queued job is cancelled at once; a running job sets
  `cancel_requested` and stops at its next progress checkpoint.
* **Crash**: on start the worker marks running jobs with a stale heartbeat
  (10 min) as failed — never silently re-run.
* **Retry**: only idempotent job types (everything except `data.import_upload`)
  and only when not active; it queues a new job linked by `retry_of`.
* **Deduplication**: an identical queued/running job is returned instead of a
  second copy.
* **No secrets**: jobs never contain credentials (providers read them from the
  worker environment); error messages are scrubbed of every configured secret
  value before they are stored or logged.

## Uploads

1. `POST /api/v1/data/uploads` with the CSV bytes. The API checks the plain
   `*.csv` display name (no paths), the content type, the size (25 MiB), symbol,
   kind and frequency; decodes it as UTF-8 text (no binary); parses it with the
   platform's own `FileDataProvider` in a private temporary directory; and runs
   the standard data validator (dates, OHLC consistency, duplicates, malformed
   values, gaps). The bytes and the preview go into `data_uploads`
   (`validated` or `invalid`).
2. The operator reviews the preview (row count, date range, head/tail rows,
   problems and warnings such as ignored `div`/`split` columns or no
   corporate-actions file) and chooses **Import** (only for `validated`) or
   **Discard** (drops the stored bytes, keeps the record).
3. A `data.import_upload` job on the worker re-validates the same bytes, writes
   them atomically to a **server-chosen** name in the import directory
   (`SYMBOL.csv`, `SYMBOL_<freq>.csv` or `SYMBOL_actions.csv`, archiving any
   previous file), then runs the normal file-provider download so the data is
   versioned and validated exactly like any other data.

## Runtime configuration

`runtime_config` (one row) holds an overlay of dotted setting paths and strategy
overrides; `runtime_config_changes` (append-only) records every change with actor,
time, old/new value, reason and the config versions before and after.

* The effective configuration is the reviewed YAML plus the overlay, re-validated
  with the full `Settings` schema (including cross-field risk rules) and the
  strategy registry, then fingerprinted into a new `config_version`
  (`<env>-<hash>`). Every job records the version it used.
* Only the settings in `config/runtime.py: EDITABLE` can be changed. Classes:
  **runtime** (applies to the next job), **runtime_confirm** (risk limits:
  tighten-only against the reviewed YAML value, with the phrase
  `TIGHTEN RISK LIMITS`), **restart**, **secret** and **immutable** (read-only in
  the UI with the reason).
* `trading.mode` can only be `shadow` or `paper`, and only through the Trading
  Control workflow. The overlay refuses any result that uses real money or
  enables live trading.
* Strategy overrides may change parameters (research/disabled only), `enabled`,
  and the lifecycle up to `shadow`. `live_approved` cannot be set by any override.
* Optimistic concurrency: a change must name the revision it was based on.
* A running scheduler keeps the configuration it started with; the Trading
  Control page says when a stop/start is needed to apply newer settings.

## Scheduler supervision

The trading scheduler runs inside the worker only while **both**:

1. the deployment master gate `AQ_SCHEDULER_ENABLED` is true on the worker
   service (the UI cannot change it; `false` stops a running scheduler within
   one supervision round), and
2. the audited operator switch `control_state['scheduler'].desired == "running"`
   (default: stopped).

Starting builds the same dependencies as `aq trade run` (shadow/paper only,
eligible human-promoted strategies, database-backed kill switch, paper broker
checks) and takes the single-scheduler advisory lock. Every cycle still runs the
kill-switch, data-freshness, broker, reconciliation, risk and pre-flight checks.
Stopping is always accepted, is never rate limited, and fails safe: no new step
starts; a step in progress finishes. The kill switch remains a separate,
independent control.

## Audit

`control_events` (append-only) records every control-plane mutation — accepted
and refused — with actor, action, target, outcome, details and client address.
Lifecycle moves are also written to the governance ledger
(`strategy_lifecycle_events`) with a `human` actor. Kill-switch actions keep their
own audit trail and are mirrored here.

## Security analysis (summary)

| Threat | Mitigation |
|---|---|
| Unauthenticated control | every mutation requires the bearer token (tested per route); probes are the only public routes |
| New write route added by mistake | the allowlist is asserted against the app and the OpenAPI schema, and mirrored in the dashboard tests |
| Mass assignment / unexpected input | `extra="forbid"` on every body model (tested by walking the schema), enums and bounds |
| Command execution | no shell or generic job type; a closed catalogue of handlers calling services |
| Path traversal via upload | the client name is only a hint; server-chosen file names; resolved-path check; atomic write |
| Oversized / binary upload | proxy limit 26 MB on the upload path only (64 KB elsewhere), API streaming limit 25 MiB, UTF-8/no-NUL checks |
| Secret disclosure | API never reads broker/provider secrets; worker publishes only booleans; errors scrubbed; tests assert no secret appears in responses |
| CSRF | header-based bearer auth (no cookies) and explicit CORS origins |
| Flooding | per-client rate limits; stop/engage exempt |
| Accidental live trading | no API path to live mode or `live_approved`; overlay refuses real money; `aq deploy check` refuses live; the broker factory has no live adapter; `AQ_LIVE_TRADING_CONFIRM` is never created |
| Scheduler started against deployment intent | the master gate is checked on every supervision round, independent of the UI |
| Concurrent schedulers / workers | advisory lock for the scheduler; `SKIP LOCKED` job claims with owner checks |
