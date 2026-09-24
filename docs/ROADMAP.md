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
| 7 | Ensemble + portfolio allocation + risk engine | next |
| 8 | PostgreSQL persistence + audit trail | |
| 9 | Broker abstraction, Alpaca paper, order planner/manager, reconciliation | |
| 10 | Scheduler, trading cycle, shadow mode, email notifications | |
| 11 | FastAPI | |
| 12 | Next.js dashboard | |
| 13 | Docker deployment, paper-vs-backtest report, AWS documentation | |

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

### M7 — Ensemble, portfolio, risk
**Acceptance:** equal/fixed/risk-adjusted/walk-forward weighting; correlation clustering and family caps; score→allocation policy; risk engine applying every rule in RISK_MANAGEMENT.md with an adjustment record per change; property tests: output never violates any configured limit for random inputs; failure inside any rule ⇒ refusal.

### M8 — Persistence
**Acceptance:** SQLAlchemy models + Alembic migrations for DATABASE.md; repositories with integration tests against real PostgreSQL (docker); "explain decision" query returns the full chain for a cycle; DB-down ⇒ order intents cannot be created (tested).

### M9 — Broker, OMS, reconciliation
**Acceptance:** Alpaca paper adapter; simulated broker for tests with fault injection; integration tests for: API timeout, duplicate submission, partial fill, rejection, restart mid-trade, unexpected position, existing unknown order, zero/insufficient buying power, price spike, large gap; the 1,500/1,000/500 example yields no order; reconciliation halts on discrepancy.

### M10 — Scheduler, shadow mode, notifications
**Acceptance:** cycle runner with minutes-before-close schedule; tests for weekends, holidays, early closes, halts; restart resumes the same cycle; shadow mode never calls `submit_order` (asserted with a spy broker); email notifier with templated messages for every event type; end-of-day summary.

### M11 — API
**Acceptance:** FastAPI read endpoints for every dashboard page; kill-switch endpoints with confirmation; auth (single-operator token) ; OpenAPI schema; no endpoint can change trading mode.

### M12 — Dashboard
**Acceptance:** Next.js/TypeScript pages: Overview, Portfolio, Strategies, Signals, Risk, Performance, Backtests, Orders, Executions, System Health, Configuration; prominent PAPER/LIVE banner; STOP AUTOMATED TRADING button with confirmation; charts per the spec.

### M13 — Deployment
**Acceptance:** Dockerfiles for api/scheduler/dashboard; docker-compose full stack; paper-vs-backtest report; AWS guide (ECS Fargate, RDS, EventBridge, Secrets Manager, CloudWatch, S3) with least-privilege IAM notes.
