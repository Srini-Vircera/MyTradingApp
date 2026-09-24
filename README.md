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
| Market data, indicators, strategies, backtesting, research (M2–M6) | planned |
| Ensemble, risk engine, persistence, broker/OMS, scheduler (M7–M10) | planned |
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

The schema and migrations arrive in Milestone 8.

## 4. Stop / resume automated trading (kill switch)

```bash
aq kill-switch status
aq kill-switch engage  --actor "Your Name" --reason "what happened"
aq kill-switch release --actor "Your Name" --reason "why it is safe" --confirm "RE-ENABLE TRADING"
```

A fresh installation starts **engaged**; release it deliberately once you are
ready to paper trade. Engaging never deletes history or configuration.

## 5. Research and trading workflows *(Milestones 2–13)*

| Task | Command (planned) | Milestone |
|---|---|---|
| Download market data | `aq data download --symbols QQQ TQQQ SQQQ SPY` | 2 |
| Validate market data | `aq data validate` | 2 |
| Run a backtest | `aq backtest run --strategy long_term_trend_sma` | 5 |
| Strategy research (sweeps, walk-forward, Monte Carlo) | `aq research run` | 6 |
| Start the API | `aq api` | 11 |
| Start the dashboard | `cd apps/dashboard && npm run dev` | 12 |
| Paper trading | `aq --env paper trade run` | 10 |
| Shadow mode | set `trading.mode: shadow`, then `aq trade run` | 10 |

## 6. Logs

Structured logs go to stderr — JSON in paper/production, readable console
output in development (`logging.format`). Every record carries a UTC timestamp
and, once trading runs exist, `run_id` and `config_version`. Secret-looking
fields are redacted. The kill-switch audit trail is
`var/state/kill_switch_audit.jsonl`.

## 7. Tests and quality checks

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
src/adaptive_quant/   core, config, observability, governance, notifications, trading/…
config/               YAML configuration
tests/                unit / integration / regression
docs/                 design and operating documentation
apps/                 api (M11) and dashboard (M12)
```
