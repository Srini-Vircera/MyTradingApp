# Database schema overview (PostgreSQL, Milestone 8)

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
