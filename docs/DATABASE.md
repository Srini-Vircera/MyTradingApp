# Database schema overview (PostgreSQL, Milestone 8)

> **Implemented in Milestone 8:** `src/adaptive_quant/persistence/`. See
> [Implementation notes](#implementation-notes-m8).

Principles: every decision is reproducible from stored rows; append-only
where possible (corrections are new rows, not updates); every row carries
`created_at` (UTC) and, for decisions, `config_version` and `run_id`.
Time-series bars themselves live in Parquet (fast research I/O); PostgreSQL
stores their metadata and every operational/audit record. Migrations via Alembic.

## Reference & versioning

| Table | Key columns | Notes |
|---|---|---|
| `config_versions` | `config_version` PK, `environment`, `digest`, `resolved_json` (secrets redacted), `source_hashes`, `created_at` | Inserted on first use of a new fingerprint. |
| `instruments` | `symbol` PK, `asset_class`, `leverage`, `underlying`, `tradeable`, `inception_date` | |
| `strategy_versions` | `id` PK, `strategy_id`, `version`, `family`, `params_json`, `code_hash`, `created_at`, `research_notes` | Unique (`strategy_id`, `version`). |
| `strategy_lifecycle_events` | `id`, `strategy_version_id` FK, `from_state`, `to_state`, `actor_id`, `actor_kind`, `reason`, `at` | Append-only; current state = latest event. |
| `research_runs` | `id`, `kind` (backtest/walk_forward/monte_carlo/sensitivity), `strategy_version_id`, `config_version`, `data_snapshot_id`, `metrics_json`, `artifact_uri`, `created_at` | Trial registry for multiple-testing corrections. |

## Market data metadata

| Table | Key columns |
|---|---|
| `data_snapshots` | `id`, `provider`, `symbol`, `frequency`, `adjustment`, `start`, `end`, `row_count`, `content_hash`, `is_synthetic`, `parquet_uri`, `fetched_at` |
| `data_quality_reports` | `id`, `snapshot_id` FK, `passed`, `issues_json`, `checked_at` |
| `corporate_actions` | `symbol`, `ex_date`, `type`, `ratio`/`amount`, `source` |

## Decision audit trail (one "trading cycle" per session)

| Table | Key columns |
|---|---|
| `trading_cycles` | `cycle_id` PK, `session_date`, `environment`, `mode`, `config_version`, `run_id`, `status`, `started_at`, `finished_at` |
| `preflight_reports` | `id`, `cycle_id` FK, `passed`, `results_json`, `checked_at` |
| `indicator_snapshots` | `id`, `cycle_id`, `symbol`, `as_of`, `data_timestamp`, `values_json` |
| `strategy_signals` | `id`, `cycle_id`, `strategy_version_id`, `timestamp`, `data_timestamp`, `direction`, `raw_score`, `normalized_score`, `confidence`, `suggested_exposure`, `reason`, `indicator_values_json` |
| `ensemble_decisions` | `id`, `cycle_id`, `method`, `strategy_weights_json`, `combined_score`, `regime_json` |
| `portfolio_proposals` | `id`, `cycle_id`, `weights_json`, `rationale` (pre-risk) |
| `risk_decisions` | `decision_id` PK, `cycle_id`, `proposal_id`, `approved_weights_json`, `adjustments_json` (rule, before, after, why), `drawdown_band`, `vol_scale`, `blocked_risk_increasing` |
| `target_portfolios` | `decision_id` FK, `as_of`, `weights_json`, `cash_weight`, `net_underlying_exposure` |

## Execution

| Table | Key columns |
|---|---|
| `order_intents` | `client_order_id` PK, `cycle_id`, `decision_id`, `symbol`, `side`, `quantity`, `order_type`, `tif`, `limit_price`, `risk_increasing`, `state`, `created_at` — **written before transmission** |
| `order_events` | `id`, `client_order_id` FK, `from_state`, `to_state`, `broker_order_id`, `filled_qty`, `avg_price`, `raw_json`, `at` (append-only state history) |
| `executions` | `id`, `client_order_id`, `broker_execution_id` UNIQUE, `qty`, `price`, `fee`, `at`, `expected_price`, `slippage_bps` |
| `position_snapshots` | `id`, `cycle_id`, `source` (broker/expected), `symbol`, `quantity`, `market_value`, `as_of` |
| `account_snapshots` | `id`, `cycle_id`, `equity`, `cash`, `buying_power`, `as_of` |
| `reconciliation_reports` | `id`, `cycle_id`, `passed`, `differences_json`, `at` |
| `shadow_orders` | same shape as `order_intents`, never transmitted |

## Monitoring

| Table | Key columns |
|---|---|
| `daily_performance` | `session_date`, `environment`, `equity`, `pnl`, `return`, `drawdown`, `turnover`, `benchmark_returns_json` |
| `system_events` | `id`, `severity`, `component`, `event`, `context_json`, `at` |
| `errors` | `id`, `component`, `error_type`, `message`, `traceback`, `cycle_id`, `at` |
| `kill_switch_events` | `id`, `engaged`, `actor`, `reason`, `at` |
| `notifications_sent` | `id`, `event_type`, `severity`, `channel`, `delivered`, `error`, `at` |

"Why TQQQ 40%?" is answered by joining `target_portfolios` → `risk_decisions`
(adjustments) → `ensemble_decisions` → `strategy_signals` (scores, reasons,
indicator values) → `indicator_snapshots` → `data_snapshots`, all under one
`cycle_id` and `config_version`.

## Implementation notes (M8)

**Code** (`src/adaptive_quant/persistence/`):

| Module | Contents |
|---|---|
| `models.py` | SQLAlchemy 2.0 models for every table above (28) |
| `migrations/` | Alembic; revision `0001` creates the schema and the safety triggers |
| `db.py` | Engine and transaction handling |
| `repositories.py` | Reference data, trading cycles and decisions, orders, monitoring |
| `explain.py` | The "why?" query |
| `records.py` | Plain record types the repositories accept |

**Layering.** The package depends only on `core` (checked by a static test). The trading layer maps platform objects to records (`trading/audit.py`) and injects the order state-machine guard (`order_repository`).

**Commands** (the URL comes only from `DATABASE_URL`, e.g. `postgresql://aq:…@127.0.0.1:5432/adaptive_quant`, and is printed with the password hidden):

```bash
make db-up                 # local PostgreSQL (docker compose)
aq db upgrade              # migrate; record config version, instruments, strategy versions
aq db status               # reachable? schema at head? any drift between models and database?
aq db explain cyc-paper-2024-01-02   # full stored decision chain as JSON
```

**Guarantees:**

| Guarantee | How |
|---|---|
| All or nothing | Each repository call is one transaction. The whole decision chain (signals, ensemble, proposal, risk decision, target) commits atomically |
| Order intents persisted before transmission | `OrderRepository.create_intent` returns only after COMMIT. An unreachable database raises `DatabaseUnavailableError`; a duplicate `client_order_id`, or an unknown cycle or risk decision, raises `PersistenceError`. Either way the order must not be sent |
| Database down ⇒ no trading | `DatabaseCheck` in the pre-trade gate refuses **all** orders (`database_unavailable`) if the database is unreachable or not at the head migration |
| Append-only audit | Triggers reject UPDATE and DELETE on every audit table, and DELETE on `order_intents` and `trading_cycles` |
| Order safety in the database | Triggers enforce defence in depth behind the application's state-machine guard: identifying columns are immutable, terminal states are final, and UNKNOWN can never return to created, validated or submitted |
| No look-ahead in stored signals | CHECK `data_timestamp <= timestamp` |
| Idempotency | Configs, strategy versions, data snapshots and instruments are inserted once (keyed on their fingerprints). Duplicate broker execution reports are ignored |
| Exact numbers | Money, prices and quantities are `NUMERIC`. Decimals in JSON payloads are stored as strings |
| Secrets | The URL (with password) never appears in logs, errors or `repr`. Resolved configs are stored redacted |

**Tests** (`tests/persistence/`, marker `postgres`). They run against **real PostgreSQL**: a
temporary local cluster, or the server in `AQ_TEST_DATABASE_URL` (CI runs a PostgreSQL 16
service). Each test gets a fresh, migrated database. Covered:
- migrations match the models, with no drift, and downgrade cleanly;
- every trigger and constraint;
- atomic rollback;
- the order lifecycle, including UNKNOWN handling;
- the full explain chain from a real M7 decision;
- an unreachable server, and a database taken down mid-session: the next intent is refused and nothing is written;
- the CLI.
