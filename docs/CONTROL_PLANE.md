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

## Trading mode, automation, kill switch and the master gate

Four **separate** controls; each one can only make trading safer than the one above it:

| Control | Values | Where it is set | What it does |
|---|---|---|---|
| `AQ_SCHEDULER_ENABLED` | on / off | Railway variable on the **worker** service (deployment) | Master gate for automation. Off ⇒ automation cannot run, whatever the dashboard says. The dashboard shows it but can never change it |
| **Trading Mode** | SHADOW / PAPER | Dashboard (Settings → Trading, or Trading Control); stored in PostgreSQL | SHADOW: strategies, signals and orders are calculated and recorded, nothing is sent. PAPER: orders may go only to the verified Alpaca **paper** account (simulated funds) |
| **Automation** | STOPPED / RUNNING | Dashboard (Trading Control: *Start/Stop Paper Trading*); stored in PostgreSQL | Whether the worker runs the trading cycle. Default STOPPED |
| **Kill switch** | ENGAGED / RELEASED | Dashboard or CLI (typed confirmation); stored in PostgreSQL | Blocks new risk. Fresh installs start ENGAGED |

Choosing PAPER **does not** start automation, release the kill switch or approve a
strategy. Starting automation does not change the mode. Live trading is not one of
the modes: it stays LOCKED / NOT AVAILABLE and requires the separate, reviewed
conditions in [SAFETY.md](SAFETY.md).

The effective behaviour is the intersection of every layer:
`deployment gates (aq deploy check, AQ_SCHEDULER_ENABLED, paper-only broker factory)`
+ `runtime mode` + `kill switch` + `automation switch` + `strategy eligibility` +
`per-cycle pre-flight`. A dashboard setting can never override a deployment-level
restriction.

### How the mode is persisted and used

* **Storage.** `runtime_config.overlay_json["trading.mode"]` (`"shadow"` or `"paper"`) in
  PostgreSQL, with every change appended to `runtime_config_changes` and
  `control_events`. It survives browser refreshes and restarts or redeploys of the
  dashboard, API and worker. Without an overlay the reviewed YAML value applies
  (`shadow` in production).
* **Resolution.** API and worker both call `services.runtime.effective_config()`:
  YAML + overlay, re-validated (the overlay can only hold `shadow`/`paper`; anything
  using real money is refused), fingerprinted into a new `config_version`.
* **Worker.** When automation starts, the supervisor builds the trading cycle from the
  effective configuration (so a PAPER overlay produces the Alpaca paper adapter,
  SHADOW produces shadow order recording). On every supervision round (~15 s) it
  compares the persisted mode with the mode it started in and stops at once if they
  differ.
* **Transmit guard.** Before every order transmission the order manager asks a guard
  that re-reads, from PostgreSQL, the persisted mode, the automation switch and the
  master gate. If the mode is no longer the one automation started in, automation was
  stopped, or anything cannot be read, the order is not sent (fail closed) — even in
  the middle of a cycle step.

### SHADOW → PAPER (*Switch to PAPER trading*)

1. The operator selects **Paper**. The dashboard shows the paper-account checklist.
2. *Verify Alpaca paper account (read-only)* queues a worker job. The worker, which
   alone holds the credentials, checks:
   - broker is Alpaca;
   - the endpoint is the paper environment (`paper-api.alpaca.markets`);
   - credentials exist and authenticate;
   - the account reports itself as a paper account;
   - account, positions and open orders are readable;
   - the database is reachable;
   - the kill-switch state is readable.

   It also runs the read-only pre-flight: stored market data valid and fresh for the
   required symbols, and the last reconciliation. The adapter only ever talks to the
   paper host and the job only reads. Results (never credentials) are stored in
   `control_state['broker_verification']`.
3. *Switch to PAPER trading…* is enabled only when every switch check passed within
   the last 60 minutes and automation is stopped. The operator types
   `Switch to PAPER trading`. The API re-checks everything, stores the new mode and
   audits: previous mode, requested mode, operator, time, the typed confirmation, the
   broker-verification result and the resulting effective mode.

### PAPER → SHADOW (*Switch to SHADOW mode*)

Always allowed (it never needs the broker). If automation is running, it is stopped
**first** (the transmit guard then refuses every paper order immediately), then the
mode is stored. The worker stops the scheduler on its next round. There is no moment
where the dashboard says SHADOW while a paper order can still be transmitted.

### Start / Stop Paper Trading

*Start Paper Trading* requires every item of the readiness checklist:

- master gate ON;
- worker online;
- mode PAPER;
- a fresh, fully passed paper-account verification and read-only pre-flight;
- kill switch RELEASED;
- at least one strategy approved for PAPER;
- valid, fresh market data for the required symbols;
- reconciliation healthy;
- no automation already running (the single-instance lock).

Failed items are listed with what to do. The worker re-checks the mode, the
verification and the kill switch before it starts, takes the single-scheduler
advisory lock, and every cycle runs the full pre-flight. *Stop Paper Trading* is
always accepted and never rate limited; from that moment no order is transmitted.

### Operating flow

`Configure data` → `Backtest` → `Research / validate a strategy` →
`Approve the strategy for paper (Strategy Manager)` → `Select PAPER mode` →
`Verify the Alpaca paper account` → `Review the readiness checklist` →
`Release the kill switch when appropriate` → `Start Paper Trading` → `Monitor` →
`Stop Paper Trading`.

Data uploads and downloads, research and backtests work the same in either mode and
never use a broker.

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
