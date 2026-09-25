# Development milestones and acceptance criteria

Every milestone ends with: full test suite green, `ruff` + `mypy --strict`
clean, a code review pass, and an update to this file and the README. A
milestone is not "done" while any critical test fails.

Order note: the suggested build order is kept except that **database
persistence (M8) moves ahead of the order manager (M9)**, because order
intents must be durably stored *before* transmission. Ensemble, portfolio and
risk are grouped (M7) because they share the target-portfolio contract.

| M | Scope | Status |
|---|---|---|
| 1 | Foundation: repo, configuration, core domain, safety primitives, CLI, CI | **done** |
| 2 | Market data: providers, calendar, validation, Parquet store, synthetic LETF | **done** |
| 3 | Indicator engine | **done** |
| 4 | Strategy interface + candidate catalogue | **done** |
| 5 | Backtesting engine + performance analytics + reports | **done** |
| 6 | Research robustness: sensitivity, walk-forward, Monte Carlo, DSR/PBO, ranking, governance registry | **done** |
| 7 | Ensemble + portfolio allocation + risk engine | **done** |
| 8 | PostgreSQL persistence + audit trail | **done** |
| 9 | Broker abstraction, Alpaca paper, order planner/manager, reconciliation | **done** |
| 10 | Scheduler, trading cycle, shadow mode, email notifications | **done** |
| 11 | FastAPI | **done** |
| 12 | Next.js dashboard | **done** |
| 13 | Docker deployment, paper-vs-backtest report, AWS documentation | next |

---

### M1 — Foundation ✅
**Deliverables:** `pyproject.toml` (Python 3.12, ruff, mypy strict, pytest); core
enums/errors/clock/money/ids/domain models; typed config schema + loader with
deep-merge, strict keys, cross-field validation, secret scanning, fingerprinting;
six-factor live-trading lock; env-only secrets; structured logging with
redaction; kill switch (fail-closed, audited); pre-trade gate; order state
machine; strategy lifecycle rules; broker and notifier contracts; `aq` CLI;
docker-compose PostgreSQL; CI workflow; design docs.
**Acceptance:**
- [x] All three environments load; none is live; typos, secrets-in-YAML and inconsistent risk bands are rejected with readable messages.
- [x] Live mode refused unless all six opt-ins present (each tested individually).
- [x] Kill switch: fresh/corrupt/missing state ⇒ engaged; release needs human + exact phrase; audited.
- [x] Pre-trade gate fails closed on exceptions, wrong types and empty check lists; reports all failures.
- [x] UNKNOWN order state can never transition back to SUBMITTED.
- [x] No automated promotion beyond VALIDATED.
- [x] ≥ 90% line coverage on M1 code; ruff + mypy strict clean.

### M2 — Market data ✅
**Deliverables** (see [DATA.md](DATA.md)):
- NYSE trading calendar.
- Canonical bar format: bar-end UTC timestamps.
- Provider interface with file (CSV/Parquet), Alpaca and Polygon adapters, including retry/backoff and header-only auth.
- Corporate actions, with split and total-return adjustment derived locally.
- A validator covering 15 defect kinds, plus a freshness check.
- A content-addressed, versioned, integrity-checked Parquet store.
- A pipeline with vendor-revision detection.
- A point-in-time `MarketDataView`.
- A synthetic leveraged-ETF model with a tracking-error gate and per-row labelling.
- A `MarketDataCheck` in the pre-trade gate.
- `aq data download | validate | list | synthesize`.

**Acceptance:**
- [x] Provider interface with file, Alpaca and Polygon adapters; network adapters tested against recorded-format fixtures (pagination, auth headers, symbol mapping, error and retry paths).
- [x] NYSE calendar covers holidays, early closes and special closures (tested dates incl. 2001-09-11, Sandy, 2025-01-09); out-of-coverage queries raise.
- [x] Validator detects every listed defect class, with one test per class.
- [x] Store is idempotent and versioned by content hash; corruption is detected; invalid data is never served by default.
- [x] Raw and adjusted (split, total-return) series are both stored; golden-number tests cover adjustments.
- [x] Synthetic TQQQ/SQQQ: daily-reset model (golden tests, volatility-drag test), always tagged synthetic (row flag, manifest, Parquet metadata). A tracking-error bound is enforced: synthetic series that exceed it are stored as failed and never used.
- [ ] **Operator step:** calibrate the synthetic model on *real* TQQQ/SQQQ history (`aq data download` + `aq data synthesize`) and record the measured tracking error. This couldn't be done in the build environment, which has no market-data access.
- [x] `aq data download`, `validate`, `list` and `synthesize` commands, tested end-to-end.
- [x] Staleness/validity wired into the pre-trade gate (`market_data_missing | invalid | stale`).

### M3 — Indicators ✅
**Deliverables** (see [INDICATORS.md](INDICATORS.md)):
- 21 indicator kinds as pure causal functions.
- A validated `IndicatorSpec` registry: every kind declares its warm-up, and specs are checked when created.
- `IndicatorEngine`, with full-history computation and point-in-time snapshots.

**Acceptance:**
- [x] All listed indicators: SMA, EMA, rate of change, momentum, RSI, ATR, historical volatility, rolling std, Bollinger Bands and width (plus %B), distance from MA, rolling highs/lows, drawdown, rolling Sharpe, volatility percentile, trend slope, momentum acceleration. Also: true range, rolling z-score, trend R², drawdown duration.
- [x] Each is tested against hand-computed values, and against slow reference loops on random data.
- [x] No look-ahead, shown two ways for every registered kind (34 configurations): truncating history leaves past values identical, and perturbing the future leaves the past unchanged. A test fails if a new kind is added without coverage.
- [x] Warm-up returns NaN, never partial values. Declared warm-ups are verified; the first value needs exactly warm-up + 1 rows; chained indicators propagate warm-up.
- [x] Snapshots are point-in-time by construction (a poisoned future cannot influence them).

### M4 — Strategies ✅
**Deliverables** (see [STRATEGIES.md](STRATEGIES.md)):
- A `Strategy` base class. It owns point-in-time access, warm-up, output validation and long/short restrictions.
- A typed and bounded parameter schema, and a registry.
- 20 candidates plus 2 benchmarks, covering all 15 families.
- A `StrategyCatalog` built from config: validated params and param grids, `version_id` fingerprints, and lifecycle eligibility by trading mode.
- A written-approval requirement for `live_approved`.
- A fail-closed signal runner, with `SignalGenerationCheck` in the pre-trade gate.
- `aq strategies list | validate | signals`, which is research-only.

**Acceptance:**
- [x] `Strategy` ABC and registry; 22 implementations across every `StrategyFamily`.
- [x] A shared contract suite for every strategy: scores in range and consistent with direction, deterministic, no look-ahead (future-data poisoning, truncation, intraday evaluation before the close), exact warm-up, missing or short optional NDX data, readable reason, exposure within its own limits.
- [x] Configured parameters and every param-grid value are validated against each strategy's schema; errors are aggregated.
- [x] Governance: live mode can only use `live_approved` strategies; `live_approved` requires a written human approval; shipped config has none eligible outside research.
- [x] Static safety test: `quant/*` never imports `trading/*`; strategies and indicators never import network, provider, store or secrets code.

### M5 — Backtesting & analytics ✅
**Deliverables** (see [BACKTESTING.md](BACKTESTING.md)):
- An event-driven engine that uses M4 strategies through a precomputed, point-in-time path.
- Four execution-timing models, plus execution delay.
- Costs: spread, slippage, square-root impact, commission, participation-capped partial fills, and cash-limited fills.
- An exact Decimal FIFO ledger, with a per-session identity check.
- An interim exposure allocator with the static caps from `risk.yaml`.
- Benchmarks.
- The full metric set.
- A self-contained HTML report plus CSV/JSON exports.
- `aq backtest run`.

**Acceptance:**
- [x] The event-driven engine reuses the real strategy code (the risk engine arrives in M7; interim static caps are documented).
- [x] Four explicit execution-timing models; tests prove a same-bar close fill is impossible unless `closing_auction` is selected, and that it is then flagged.
- [x] Commission, spread, slippage, impact, delay and partial fills are modelled, with hand-calculated tests.
- [x] Golden-number regression tests for every metric.
- [x] SPY/QQQ/TQQQ/cash benchmarks; synthetic periods are separated (banner, shaded band, separate real/synthetic metrics).
- [x] An HTML report with the equity curve (linear and log), drawdown, rolling return, rolling Sharpe, monthly heatmap, annual returns, exposure and allocation.
- [x] Dedicated look-ahead, timing, accounting, cost and determinism tests (future poisoning, sign-oracle power test, runtime guards).
- [ ] Before trusting any number: real data downloaded and validated, and the synthetic model calibrated on real TQQQ/SQQQ (carried over from M2).

### M6 — Research robustness ✅
**Deliverables** (see [RESEARCH.md](RESEARCH.md)):
- Trials (one M5 backtest per `param_grid` point, parallelisable, deterministic) and an append-only trial registry.
- A robustness score and plateau-centre selection; heatmaps.
- Rolling and anchored walk-forward with stitched OOS returns.
- Deflated Sharpe, PBO (CSCV), stationary-bootstrap CIs, White's Reality Check / Hansen SPA, and Benjamini–Hochberg FDR.
- Regime and decade consistency.
- Monte Carlo over six dimensions.
- A percentile-rank scorecard with fail-closed gates.
- A governance ledger with automated `research ↔ validated` only.
- A research HTML report and CSV/JSON exports.
- `aq research run | trials | status`.

**Acceptance:**
- [x] Parameter sweeps, heatmaps and a robustness score: the sharp-peak fixture is flagged overfit, the plateau fixture is not, and selection prefers the plateau centre.
- [x] Anchored and rolling walk-forward. IS and OOS are clearly separated, in separate charts and fold tables. Future-poisoning tests show selection never sees the test window.
- [x] Monte Carlo over trade sequence, return blocks, costs, parameters, start date and signal delay, with adverse-percentile tables (median, p75, p90, p95, worst), rounded.
- [x] DSR (reproduces the Bailey & López de Prado example), PBO (hand case, noise ≈ 0.5, skill ≈ 0, brute-force equivalence), block bootstrap, Reality Check/SPA, and BH-FDR (reproduces the BH 1995 example).
- [x] Ranking scorecard, never CAGR; gates fail closed; synthetic OOS can never validate.
- [x] Trial registry: append-only, distinct counting, corruption refused; it feeds the DSR N.
- [x] Governance: software never targets beyond `validated` (tested for every state) and never edits `strategies.yaml`.
- [ ] **Operator step:** run `aq research run` on validated real data (after the M2/M5 calibration steps) and review the evidence. No real-data research has been performed in the build environment.

### M7 — Ensemble, portfolio, risk ✅
**Deliverables** (see [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md#implementation-notes-m7)):
- `quant/ensemble`: point-in-time shadow returns, four weighting methods, correlation clustering, family caps.
- `quant/portfolio`: the allocation policy and the `PortfolioManager` decision chain.
- `quant/risk`: estimators and the 8-step risk engine, with `RiskDecision` / `RiskAdjustment` records.
- `RiskEngineCheck` in the pre-trade gate.
- Backtester and research integration, the default (`backtest.allocation.risk_engine`).
- Config: ensemble parameters, `drawdown_hysteresis`, per-regime exposure caps.

**Acceptance:**
- [x] Equal, fixed, risk-adjusted and walk-forward weighting; correlation clustering and family caps.
- [x] Score → allocation policy (with a dead band).
- [x] A risk engine applying every rule in RISK_MANAGEMENT.md, with an adjustment record per change.
- [x] Property tests: output never violates any configured limit for random inputs (3,000 cases in CI; 30,000 run once).
- [x] A failure inside any rule, or a failed verification, ⇒ refusal. In backtests, a refused decision places no orders.
- [x] The backtester and research use the same chain. Future-poisoning tests still hold with the risk engine on.

### M8 — Persistence ✅
**Deliverables** (see [DATABASE.md](DATABASE.md#implementation-notes-m8)):
- SQLAlchemy models for all 28 tables, and an Alembic migration with append-only and order-safety triggers.
- Transactional repositories and the "explain decision" query.
- Trading-layer record mapping and the guarded order repository.
- `DatabaseCheck` in the pre-trade gate.
- `aq db upgrade | status | explain`.
- A PostgreSQL service in CI.

**Acceptance:**
- [x] SQLAlchemy models and Alembic migrations for DATABASE.md. The migration equals the models (no drift) and is reversible.
- [x] Repositories with integration tests against real PostgreSQL: a temporary cluster locally, a service container in CI.
- [x] The "explain decision" query returns the full chain for a cycle: config, pre-trade checks, target, risk adjustments, proposal, ensemble, signals, orders, events and executions.
- [x] DB down ⇒ order intents cannot be created (tested with an unreachable server and with a database taken down mid-session); the pre-trade gate refuses.

### M9 — Broker, OMS, reconciliation ✅
**Deliverables** (see [PAPER_TRADING.md](PAPER_TRADING.md#implementation-notes-m9)):
- Alpaca paper adapter (paper endpoint only) and a fault-injecting simulated broker.
- Order planner and order manager.
- Reconciliation with human acknowledgement.
- `BrokerStateCheck` / `ReconciliationCheck`.
- A static rule that only the order manager transmits.

**Acceptance:**
- [x] Alpaca paper adapter, tested against recorded-format responses: every status mapped, submission never retried, duplicate and rejection mapping, retries on reads, no secret leakage.
- [x] Simulated broker with fault injection.
- [x] Integration tests (real PostgreSQL): API timeout, duplicate submission, partial fill, rejection, restart mid-trade, unexpected position, existing unknown order, zero/insufficient buying power, price spike, large gap.
- [x] The 1,500/1,000/500 example yields no order.
- [x] Reconciliation halts risk-increasing trading on a discrepancy until a human acknowledges it.
- [ ] **Operator step:** run against a real Alpaca *paper* account (needs keys; not possible in the build environment).

### M10 — Scheduler, shadow mode, notifications ✅
**Deliverables** (see [PAPER_TRADING.md](PAPER_TRADING.md#implementation-notes-m10)):
- Session schedule and the nine-step `TradingCycle`, with a persisted step log (migration 0002).
- `Scheduler` loop.
- Store-backed cycle data.
- Email channel and templates for every event type.
- End-of-day summary.
- `aq trade status | run | ack-reconciliation`.

**Acceptance:**
- [x] Cycle runner with a minutes-before-close schedule.
- [x] Tests for weekends, holidays, early closes (13:00), market halts (broker says closed) and halted instruments.
- [x] A restart resumes the same cycle: finished steps are skipped, the decision is reused, no duplicate orders; a late start after the cutoff places none.
- [x] Shadow mode never calls `submit_order` (asserted with a spy broker).
- [x] Email notifier with a templated message for every event type (tested with a fake SMTP server).
- [x] End-of-day summary (email plus `daily_performance`).
- [ ] **Operator step:** promote strategies to `paper` (a person, with written evidence), configure the Alpaca paper keys, `DATABASE_URL` and SMTP, then run `aq --env paper trade run`.

### M11 — API ✅
**Deliverables** (see [API.md](API.md)):
- `src/adaptive_quant/api/` and the read-only query layer `persistence/reads.py`.
- `aq api serve | openapi`.
- The committed schema `apps/api/openapi.json`.
- Config `api:` (localhost, explicit CORS, page size) and the `AQ_API_TOKEN` secret.

**Acceptance:**
- [x] FastAPI read endpoints for every dashboard page (Overview, Portfolio, Strategies, Signals, Risk, Performance, Backtests, Orders, Executions, System Health, Configuration), plus cycles and explain, reconciliation and shadow orders. Tested against a real completed cycle on PostgreSQL.
- [x] Kill-switch endpoints with typed confirmation phrases and a named operator, audited in the file log and the database.
- [x] Single-operator bearer-token auth (constant-time; a weak or missing token refuses start-up); every route except liveness requires it.
- [x] OpenAPI schema committed, with a drift test.
- [x] No endpoint can change the trading mode: route-level whitelist test, 404/405 on every mutation attempt, static import boundaries.

### M12 — Dashboard ✅
**Deliverables** (see [DASHBOARD.md](DASHBOARD.md)):
- `apps/dashboard`: a Next.js + TypeScript (strict) static export. It talks only to the operator API.
- API types generated from `apps/api/openapi.json`, with a drift check.
- Dependency-free SVG charts.
- Vitest unit tests and Playwright end-to-end tests with a mocked API.
- A `dashboard` CI job.

**Acceptance:**
- [x] Pages: Overview, Portfolio, Strategies, Signals, Risk, Performance, Backtests, Orders, Executions, System Health, Configuration, plus a cycle Explain view.
- [x] Prominent mode banner on every page: PAPER amber, SHADOW blue, LIVE / REAL MONEY red; a missing or unknown mode is shown as unsafe.
- [x] STOP AUTOMATED TRADING button on every page. It needs a named operator, a reason and the exact phrase; release is a separate flow needing `RE-ENABLE TRADING`.
- [x] Charts: equity, drawdown, daily returns, allocation over time, drawdown-band history and a signal heatmap. Each has tooltips and a table view.
- [x] No mode, order, configuration or promotion controls, enforced by static tests. The only writes are the two kill-switch calls.
- [x] The operator token is entered at runtime, kept only for the tab, sent only in the Authorization header, and never built in, persisted or logged.

### M13 — Deployment
**Acceptance:** Dockerfiles for api/scheduler/dashboard; docker-compose full stack; paper-vs-backtest report; AWS guide (ECS Fargate, RDS, EventBridge, Secrets Manager, CloudWatch, S3) with least-privilege IAM notes.
