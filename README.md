# Adaptive Quant

A risk-first, systematic trading platform for the Nasdaq-100 ecosystem
(signals from QQQ/NDX; allocations across TQQQ, SQQQ, QQQ and cash).

> **Paper trading only.** Live trading is disabled by default and cannot be
> enabled without six deliberate, human-made configuration changes
> ([docs/SAFETY.md](docs/SAFETY.md)). All historical performance produced by
> this software is hypothetical. Nothing here is investment advice or a
> promise of future returns.

## Status

Built milestone by milestone — see [docs/ROADMAP.md](docs/ROADMAP.md).

| Area | Status |
|---|---|
| Foundation: configuration, safety primitives, domain model, CLI (M1) | ✅ done |
| Market data: calendar, providers, validation, storage, synthetic history (M2) | ✅ done |
| Indicator library: 21 point-in-time indicators with look-ahead tests (M3) | ✅ done |
| Strategies: 20 candidates + 2 benchmarks, governance, research signals (M4) | ✅ done |
| Backtesting: point-in-time engine, costs, exact accounting, metrics, HTML report (M5) | ✅ done |
| Research robustness: sweeps, walk-forward, Monte Carlo, DSR/PBO/Reality Check/FDR, scorecard, trial registry (M6) | ✅ done |
| Ensemble, allocation policy, independent risk engine (M7) | ✅ done |
| PostgreSQL audit trail: schema, migrations, repositories, explain query (M8) | ✅ done |
| Broker (Alpaca paper, simulated), order planner/manager, reconciliation (M9) | ✅ done |
| Scheduler, trading cycle, shadow mode, email notifications (M10) | ✅ done |
| API, dashboard, deployment (M11–M13) | planned |

Sections below marked *(Milestone N)* describe commands that do not exist yet.

## Documentation

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, decisions, assumptions, repository layout, interfaces |
| [RISKS.md](docs/RISKS.md) | financial and implementation risks and mitigations |
| [RESEARCH_METHODOLOGY.md](docs/RESEARCH_METHODOLOGY.md) | strategy research, backtesting, walk-forward, overfitting controls |
| [RISK_MANAGEMENT.md](docs/RISK_MANAGEMENT.md) | the independent risk engine |
| [PAPER_TRADING.md](docs/PAPER_TRADING.md) | trading cycle, order management, reconciliation, shadow mode |
| [DATA.md](docs/DATA.md) | market data: conventions, providers, validation, storage, synthetic history |
| [INDICATORS.md](docs/INDICATORS.md) | indicator definitions, warm-up, point-in-time guarantees |
| [STRATEGIES.md](docs/STRATEGIES.md) | strategy contract, catalogue, governance, research signals |
| [BACKTESTING.md](docs/BACKTESTING.md) | execution timing, costs, accounting, metrics, synthetic separation |
| [RESEARCH.md](docs/RESEARCH.md) | parameter robustness, walk-forward, Monte Carlo, multiple-testing controls, scorecard, governance |
| [DATABASE.md](docs/DATABASE.md) | audit-trail schema |
| [SAFETY.md](docs/SAFETY.md) | kill switch, refusal conditions, live-trading lock |
| [ROADMAP.md](docs/ROADMAP.md) | milestones and acceptance criteria |

## 1. Install

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/) (or pip), Docker (for PostgreSQL).

```bash
git clone <this repo> && cd MyTradingApp
make install            # creates .venv with Python 3.12 and installs everything
source .venv/bin/activate
aq version
```

Without `make`: `uv venv --python 3.12 .venv && uv pip install --python .venv -e ".[dev]"`
(or `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`).

## 2. Configure

```bash
cp .env.example .env    # credentials ONLY; never commit .env
```

* **Credentials** go in `.env` (Alpaca *paper* keys, database URL, SMTP). Check
  what is set — values are never printed — with `aq secrets`.
* **Everything else** lives in `config/`:

  | File | Purpose |
  |---|---|
  | `base.yaml` | universe, data, schedule, broker endpoints, logging, notifications |
  | `risk.yaml` | all risk limits and drawdown bands (example values — research them) |
  | `strategies.yaml` | candidate strategies, parameter neighbourhoods, ensemble settings |
  | `development.yaml` / `paper.yaml` / `production.yaml` | per-environment overrides |

  Files load in the order base → risk → strategies → environment. Choose the
  environment with `--env` or `AQ_ENV`. Unknown keys are rejected on purpose.

Validate after every change:

```bash
aq --env paper config validate                       # structure + safety policy
aq --env paper config validate --require-secrets broker
aq --env paper config show                           # fully resolved config, secrets redacted
```

Each resolved configuration has a `config_version` (e.g. `paper-956afc4d3df7`)
that will be recorded with every trading decision.

## 3. Start PostgreSQL

```bash
make db-up        # docker compose up -d postgres (bound to localhost only)
make db-down      # stop; data volume is kept
```

Then set `DATABASE_URL` in `.env` (e.g. `postgresql://aq:<password>@127.0.0.1:5432/adaptive_quant`) and:

```bash
aq db upgrade     # apply migrations; record config version, instruments, strategy versions
aq db status      # reachable, schema at head, no drift
aq db explain <cycle-id>   # full stored decision chain of one trading cycle
```

The audit trail is append-only, and order intents are stored before any order is sent. See [docs/DATABASE.md](docs/DATABASE.md).

## 4. Stop / resume automated trading (kill switch)

```bash
aq kill-switch status
aq kill-switch engage  --actor "Your Name" --reason "what happened"
aq kill-switch release --actor "Your Name" --reason "why it is safe" --confirm "RE-ENABLE TRADING"
```

A fresh installation starts **engaged**; release it deliberately once you are
ready to paper trade. Engaging never deletes history or configuration.

## 5. Market data

Full details are in [docs/DATA.md](docs/DATA.md).

**Without an API key**, put CSV files in `var/data/import/`, for example
`QQQ.csv` with the columns `date,open,high,low,close,volume` and raw prices.
Add a `QQQ_actions.csv` (`ex_date,type,ratio,amount`) if the prices span
splits or dividends. Then run the commands below with the default
`data.primary_provider: file`.

**With Alpaca or Polygon**, put the keys in `.env` and add
`--provider alpaca` (or set `data.primary_provider`).

```bash
aq data download                 # every configured instrument, full history, up to the last completed session
aq data download --provider alpaca --symbols QQQ TQQQ SQQQ SPY --start 2010-01-01
aq data list                     # what is stored, whether it is valid, SYNTHETIC flags
aq data validate                 # re-check stored data; reports freshness
aq data validate --require-fresh # also fail on stale data (trading enforces this)
aq data synthesize               # synthetic pre-2010 TQQQ/SQQQ + tracking-error check vs real
```

Data that fails validation is stored for inspection but never used. Downloads
are idempotent, so re-running them is always safe.

## 6. Strategies (research only)

```bash
aq strategies list                         # 22 configured strategies, versions, warm-up
aq strategies validate                     # config/strategies.yaml against each strategy's schema
aq strategies signals --as-of 2024-06-28   # signals from stored data - places NO orders
```

- **Lifecycle:** every strategy starts in `research`. Paper and live modes run only strategies a human has promoted; `live_approved` needs a written approval in the config. See [docs/STRATEGIES.md](docs/STRATEGIES.md).
- **Hypotheses only:** none of these strategies has been shown to work yet.

## 7. Backtesting (hypothetical results)

```bash
aq backtest run --strategy ltt_sma_distance                    # real data only
aq backtest run --strategy ltt_sma_distance --synthetic        # + labelled synthetic pre-2010 history
aq backtest run --strategy st_ema_cross --execution next_open --delay 1
```

- **Output:** each run writes `var/reports/<timestamp>-<strategy>/report.html` plus CSV/JSON exports.
- **Hypothetical:** results are simulations with modelled costs and fills.
- **Synthetic data:** synthetic periods are labelled and reported separately.
- **Details:** [docs/BACKTESTING.md](docs/BACKTESTING.md).

## 8. Research robustness (hypothetical results)

```bash
aq research run                                   # all candidates: sweeps, walk-forward, statistics, Monte Carlo
aq research run --strategy st_ema_cross --workers 4
aq research trials                                # every trial ever run on this data (the DSR "N")
aq research status                                # evidence and automated lifecycle changes
```

- **Output:** `var/research/<timestamp>-<ids>/report.html` plus CSV/JSON exports.
- **Honest counting:** every trial is appended to `var/research/trials.jsonl`, and the Deflated Sharpe uses that count.
- **Ranking:** a scorecard of out-of-sample risk-adjusted metrics and robustness, never CAGR. Strict gates must all pass.
- **Governance:** software can mark a strategy `validated` (and undo it) in `var/research/governance.jsonl`. It never edits `strategies.yaml` and never promotes to paper, shadow or live.
- **Details:** [docs/RESEARCH.md](docs/RESEARCH.md).

## 9. Paper and shadow trading

```bash
aq --env paper trade status        # today's schedule (early closes shown) and cycle state
aq --env paper trade run --once    # run the steps that are due now
aq --env paper trade run           # long-running market-aware scheduler
```

`trade run` refuses unless:
- a **person** has promoted strategies to lifecycle `paper` in `strategies.yaml`;
- the Alpaca **paper** keys, `DATABASE_URL` and (optionally) SMTP settings are configured.

Set `trading.mode: shadow` to run the full pipeline without sending any order. See [docs/PAPER_TRADING.md](docs/PAPER_TRADING.md).

## 9b. Later milestones

| Task | Command (planned) | Milestone |
|---|---|---|
| Start the API | `aq api` | 11 |
| Start the dashboard | `cd apps/dashboard && npm run dev` | 12 |

## 10. Logs

Structured logs go to stderr — JSON in paper/production, readable console
output in development (`logging.format`). Every record carries a UTC timestamp
and, once trading runs exist, `run_id` and `config_version`. Secret-looking
fields are redacted. The kill-switch audit trail is
`var/state/kill_switch_audit.jsonl`.

## 11. Tests and quality checks

```bash
make test        # pytest
make cov         # with coverage
make lint        # ruff
make typecheck   # mypy --strict
make check       # all of the above (what CI runs)
make validate    # validate every shipped environment
```

## Project layout

```
src/adaptive_quant/   core, config, observability, governance, notifications, persistence, quant/{data,indicators,strategies,backtest,analytics,research,ensemble,portfolio,risk}, trading/…
config/               YAML configuration
tests/                unit / integration / regression
docs/                 design and operating documentation
apps/                 api (M11) and dashboard (M12)
```
