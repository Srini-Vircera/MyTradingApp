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
| 2 | Market data: providers, calendar, validation, Parquet store, synthetic LETF | next |
| 3 | Indicator engine | |
| 4 | Strategy interface + candidate catalogue | |
| 5 | Backtesting engine + performance analytics + reports | |
| 6 | Research robustness: sensitivity, walk-forward, Monte Carlo, DSR/PBO, ranking, governance registry | |
| 7 | Ensemble + portfolio allocation + risk engine | |
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

### M2 — Market data
**Acceptance:** provider interface with CSV/Parquet, Alpaca and Polygon adapters (network adapters tested against recorded fixtures); NYSE calendar incl. holidays and early closes; validator detects every listed defect class (one test per class); Parquet store is idempotent and versioned by content hash; adjusted/unadjusted both stored; synthetic TQQQ/SQQQ reproduces real ETF daily returns over the overlap within a documented tracking-error bound and is always tagged `synthetic`; `aq data download` and `aq data validate` commands.

### M3 — Indicators
**Acceptance:** all listed indicators; each tested against hand-computed values; *no-look-ahead property test*: value at t computed on data[:t] equals value from full series; warm-up periods return NaN, never partial values.

### M4 — Strategies
**Acceptance:** `Strategy` ABC + registry; ~20 candidates across all families; every strategy passes a shared contract test suite (scores in range, deterministic, no look-ahead, readable reason, warm-up respected, handles missing optional NDX data); config parameters validated against each strategy's parameter schema.

### M5 — Backtesting & analytics
**Acceptance:** event-driven engine using the real risk-engine interface; four explicit execution-timing models; costs (commission, spread, slippage, impact, delay, partial fills); golden-number regression tests for every metric; benchmarks SPY/QQQ/TQQQ/cash; HTML report with all listed charts; test proving same-bar close execution is impossible unless `closing_auction` is selected.

### M6 — Research robustness
**Acceptance:** parameter sweeps + heatmaps + robustness score (a synthetic "sharp peak" fixture is flagged overfit, a plateau fixture is not); anchored & rolling walk-forward with IS/OOS clearly separated in charts; Monte Carlo over the listed dimensions with percentile tables; DSR, PBO (CSCV), block bootstrap, reality-check and BH-FDR implemented and tested against published examples; ranking scorecard; trial registry.

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
