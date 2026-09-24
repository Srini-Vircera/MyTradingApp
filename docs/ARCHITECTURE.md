# Architecture

> Status: design baseline for version 1. Components marked *(M#)* are built in
> the milestone of that number; see [ROADMAP.md](ROADMAP.md).

## 1. System overview

```
                     ┌──────────────────────── research path (offline) ─────────────────────────┐
                     │                                                                           │
 Providers ──► Data validation ──► Parquet store ──► Indicator engine ──► Strategy engines ──► Ensemble
 (Alpaca, Polygon,    (gaps, dupes,     (normalised,       (point-in-time,     (continuous       engine
  CSV/Parquet)         stale, spikes)    versioned)          no look-ahead)      scores)            │
                                                                                                   ▼
                                                                                   Portfolio allocation
                                                                                   (score → weights)
                                                                                           │
            ┌──────────────────────────── live / paper / shadow path ──────────────────────▼───────────┐
            │                                                                                          │
            │   Pre-trade safety gate ──► Risk engine (final authority) ──► Target portfolio           │
            │   (refuses on stale data,       caps, vol target, drawdown        (weights, audited)     │
            │    unknown positions, kill       bands, turnover, loss limit             │               │
            │    switch, ...)                                                          ▼               │
            │                                                     Order planner (target − confirmed    │
            │                                                     − pending open orders = delta)       │
            │                                                                          │               │
            │                                  shadow mode: record only ◄──────────────┤               │
            │                                                                          ▼               │
            │   Reconciliation ◄── Fill confirmation ◄── Order manager (idempotent) ──► BrokerAdapter  │
            └──────────────────────────────────────────────────────────────────────────────────────────┘
                        │                                   │
                        ▼                                   ▼
                  PostgreSQL (audit trail) ──► FastAPI ──► Next.js dashboard      Notifications (email…)
```

The **same code path** — data → indicators → strategies → ensemble → risk →
target portfolio — is used by the backtester, shadow mode, paper trading and
(eventually) live trading. Only the component below the target portfolio
differs (simulated fills vs. recorded-only vs. broker). This is the single most
important design decision against backtest/live divergence.

### Layering rules

| Layer | May depend on | Must never depend on |
|---|---|---|
| `core` (types, enums, errors, clock, money) | stdlib, pydantic | anything else |
| `config`, `observability` | `core` | research / trading |
| `quant.*` (data, indicators, strategies, ensemble, portfolio, risk, backtest, analytics) | `core`, `config` | `trading`, brokers, database |
| `trading.*` (safety, orders, brokers, reconciliation, scheduler) | `core`, `config`, `quant` outputs (TargetPortfolio) | strategy internals |
| `persistence` | `core` | business logic |
| `apps/api`, `apps/dashboard` | everything via service interfaces | direct broker calls |

Strategies cannot import broker code; the risk engine cannot be bypassed
because the order planner only accepts a `TargetPortfolio` that carries a risk
decision ID (M7/M9).

## 2. Key architectural decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Target-portfolio architecture** (weights), not BUY/SELL signals | Idempotent by construction: re-running a cycle recomputes the same target, and the delta vs. confirmed positions + open orders is the only thing ever ordered. |
| D2 | **Risk engine is a separate module with final authority** | Strategies *suggest* exposure; only risk-approved targets reach execution. |
| D3 | **Fail closed everywhere** | Any error in data, signals, risk, broker state or even in a safety check itself ⇒ no risk-increasing trades. Kill-switch state that cannot be read ⇒ engaged. |
| D4 | **Deterministic client order IDs** from (cycle, symbol, side, sequence) | A retry, restart or delayed response regenerates the same ID; the broker rejects duplicates. |
| D5 | **Order intents persisted before transmission**; `UNKNOWN` state is investigated, never resubmitted | Required for crash safety. This forces persistence (M8) to land **before** the order manager (M9) — a deliberate reordering of the suggested build order. |
| D6 | **Configuration only in versioned YAML**; secrets only in env vars; each resolved config hashed into `config_version` | Every decision is reproducible and auditable; secrets can't leak via git. |
| D7 | **Live trading needs six independent opt-ins** (see [SAFETY.md](SAFETY.md)) | No single edit, env var or bug can enable real money. |
| D8 | **Schedule expressed as minutes-before-close** | Early-close days (13:00 ET) are handled without special cases. |
| D9 | **UTC internally; US/Eastern only at boundaries**; naive datetimes rejected | Eliminates DST and timezone bugs. |
| D10 | **Decimal for money/quantities; float for statistics** | Order sizes and balances must be exact; research math must be fast. |
| D11 | **Modular monolith**, not microservices | One deployable Python package + a dashboard. Scheduler and API are separate *processes* of the same codebase. Simpler to operate, test and audit. |
| D12 | **Long-only, account-unlevered in v1** | Bearish exposure only by *holding* SQQQ; weights ≥ 0 and sum ≤ 1. No margin, no shorting. |

## 3. Important assumptions

1. **Daily-frequency strategies.** Signals are computed once per session near the close; intraday bars are used only to estimate the "near-close" price. Holding periods are days to months.
2. **Execution timing.** Default live methodology: compute signals at ~T-15 min using the latest intraday price as a proxy for the close, and trade before the close. The backtest models exactly this (signal on a *near-close* proxy, fill at close ± slippage) or, alternatively, signal on close T and fill at open/close T+1. It never both *uses* the close of T and *fills* at the close of T unless the closing-auction model is explicitly chosen.
3. **Universe.** QQQ (signal + tradeable), TQQQ, SQQQ, cash; SPY as benchmark; NDX as optional signal data. ETFs only. Survivorship bias is limited for this universe, but pre-inception TQQQ/SQQQ history must be *synthetic* and labelled as such.
4. **Broker.** Alpaca paper first; supports fractional shares and `client_order_id` uniqueness. MOC orders may not be available on all accounts, so the default is a marketable order placed ~10 minutes before the close (configurable).
5. **Account size** is small relative to ETF liquidity (TQQQ/QQQ trade billions per day), so market impact is modelled as a simple spread + slippage function, not a full impact model.
6. **Data vendors are fallible.** Adjusted prices can be revised; bars can be missing or late. The system validates, versions and refuses to trade on doubt.
7. **Operator availability.** A single owner-operator who may not be watching at 15:50 ET. The system must be safe unattended: alerts on anomalies, halts on doubt, never "guesses".
8. **No claim of edge.** Every strategy is a hypothesis until walk-forward, robustness and multiple-testing analyses say otherwise, and even then results are hypothetical.

## 4. Repository structure

```
MyTradingApp/                      (the repo; the platform is called "adaptive-quant")
├── src/adaptive_quant/            single installable package (src layout)
│   ├── core/                      enums, errors, clock, money, ids, domain models   [M1]
│   ├── config/                    YAML schema, loader, secrets, fingerprinting       [M1]
│   ├── observability/             structured logging (metrics later)                 [M1]
│   ├── governance/                strategy lifecycle & approvals                      [M1]
│   ├── notifications/             event taxonomy, router, channels                    [M1 contract, M10 email]
│   ├── quant/
│   │   ├── data/                  calendar, bars, providers, validation, store, synthetic  [M2]
│   │   ├── indicators/            causal indicator functions, spec registry, engine  [M3]
│   │   ├── strategies/            Strategy base, 22 candidates, catalogue, runner   [M4]
│   │   ├── backtest/              event loop, execution & cost models                [M5]
│   │   ├── analytics/             metrics, reports, charts                           [M5]
│   │   ├── optimization/          sensitivity, walk-forward, Monte Carlo, DSR/PBO    [M6]
│   │   ├── ensemble/              signal combination, correlation control           [M7]
│   │   ├── portfolio/             score → weights, rebalance bands                   [M7]
│   │   └── risk/                  independent risk engine                            [M7]
│   ├── trading/
│   │   ├── safety/                kill switch, pre-trade refusal gate                [M1]
│   │   ├── orders/                state machine [M1], order planner & OMS [M9]
│   │   ├── brokers/               BrokerAdapter contract [M1], Alpaca paper [M9]
│   │   ├── reconciliation/                                                            [M9]
│   │   └── scheduler/             market-aware trading cycle                         [M10]
│   ├── persistence/               SQLAlchemy models, repositories, Alembic           [M8]
│   └── cli.py                     `aq` operator CLI
├── apps/
│   ├── api/                       FastAPI app (thin layer over services)            [M11]
│   └── dashboard/                 Next.js + TypeScript                               [M12]
├── config/                        base / risk / strategies / development / paper / production .yaml
├── tests/{unit,integration,regression}/
├── docs/                          this documentation
├── infrastructure/                Dockerfiles, AWS notes                             [M13]
├── scripts/                       one-off operational scripts
├── docker-compose.yml, Makefile, pyproject.toml, .env.example, README.md
```

**Deviations from the suggested layout, and why**

* **`src/adaptive_quant/…` instead of top-level `quant/` and `trading/`.** Two
  top-level packages with generic names would collide with PyPI packages of the
  same name and allow accidental imports from the working directory. The
  `quant` / `trading` split is preserved *inside* one namespace.
* **`governance/`, `notifications/`, `observability/`, `persistence/` added** as
  first-class packages because model governance, alerting, logging and the audit
  store are cross-cutting and shouldn't hide inside `trading/`.
* **Repository name.** The repo is `MyTradingApp`; the Python distribution is
  `adaptive-quant` and the CLI is `aq`.

## 5. Core Python interfaces

Implemented in M1: `Clock`, `StrategySignal`, `TargetPortfolio`,
`Instrument`, `AccountSnapshot`, `Position`, `OrderRequest`, `OrderSnapshot`,
`MarketClock`, `BrokerAdapter`, `KillSwitchStore`, `PreflightCheck`,
`Notifier`, lifecycle `transition()`, order `assert_transition()`.

Implemented in M2 (`quant/data`, see [DATA.md](DATA.md)):

```python
class MarketDataProvider(ABC):                       # providers/base.py
    capabilities: ProviderCapabilities               # frequencies, adjustments, corporate actions
    def fetch_bars(self, request: BarRequest) -> pd.DataFrame: ...          # canonical, unvalidated
    def fetch_corporate_actions(self, symbol, start, end) -> list[CorporateAction]: ...
    # raises CorporateActionsUnavailable when unknown; [] means "confirmed none"

class TradingCalendar:                               # calendar.py (NYSE via exchange_calendars)
    def sessions_in_range(self, start: date, end: date) -> list[Session]: ...
    def session(self, d: date) -> Session | None: ...
    def last_completed_session(self, as_of: datetime) -> Session | None: ...
    def is_open(self, at: datetime) -> bool: ...

class BarValidator:                                  # validation.py
    def validate(self, bars, *, frequency, subject, corporate_action_dates=(),
                 as_of=None, expected_start=None, expected_end=None) -> ValidationReport: ...

class ParquetBarStore:                               # store.py (content-addressed, versioned)
    def write(self, key: SeriesKey, bars, *, provider, validation, is_synthetic=False) -> SnapshotInfo: ...
    def read(self, key: SeriesKey, *, require_valid=True) -> pd.DataFrame: ...

class MarketDataView:                                # view.py - the only way strategies see prices
    def bars(self, symbol: str, lookback: int | None = None) -> pd.DataFrame: ...  # timestamp <= as_of
    def data_timestamp(self, symbol: str) -> datetime: ...
```

Implemented in M3 (`quant/indicators`, see [INDICATORS.md](INDICATORS.md)):

```python
@dataclass(frozen=True)
class IndicatorSpec:                                 # specs.py - validated at construction
    kind: str; params: Mapping[str, ParamValue]; source: str | None; name: str
    warmup: int                                      # leading NaN rows (verified by tests)
    def compute(self, bars: pd.DataFrame) -> pd.Series: ...   # value at t uses rows <= t only

class IndicatorEngine:                               # engine.py
    def compute(self, bars) -> pd.DataFrame: ...                          # whole history
    def snapshot_view(self, view: MarketDataView, symbol) -> IndicatorSnapshot: ...  # as of view.as_of
```

Implemented in M4 (`quant/strategies`, see [STRATEGIES.md](STRATEGIES.md)):

```python
class Strategy(ABC):                                 # base.py
    implementation: ClassVar[str]; family: ClassVar[StrategyFamily]; version: ClassVar[str]
    param_specs: ClassVar[tuple[ParamSpec, ...]]     # typed, bounded, validated at construction
    def indicators(self) -> list[IndicatorSpec]: ...                 # declares warm-up
    def evaluate(self, ctx: StrategyContext) -> Evaluation: ...      # the only strategy-specific logic
    def generate_signal(self, view: MarketDataView) -> StrategySignal: ...  # final; owns all checks

class StrategyCatalog:                               # catalog.py - config -> strategies + versions
    def eligible(self, mode: TradingMode) -> list[Strategy]: ...    # live => live_approved only

def run_strategies(strategies, view) -> SignalBatch: ...            # runner.py - fails closed
```

Specified here, implemented in later milestones:

```python
# quant/ensemble + portfolio + risk (M7) ----------------------------------------
class EnsembleEngine:
    def combine(self, signals: Sequence[StrategySignal], weights: StrategyWeights) -> EnsembleScore: ...

class AllocationPolicy(Protocol):
    def allocate(self, score: EnsembleScore, regime: RegimeState) -> ProposedPortfolio: ...

class RiskEngine:
    def evaluate(self, proposal: ProposedPortfolio, ctx: RiskContext) -> RiskDecision: ...
    # RiskDecision = approved TargetPortfolio + list[RiskAdjustment(rule, before, after, why)]

# trading (M9) ------------------------------------------------------------------
class OrderPlanner:
    def plan(self, target: TargetPortfolio, account: AccountSnapshot,
             positions: list[Position], open_orders: list[OrderSnapshot],
             prices: Mapping[str, Decimal]) -> list[OrderRequest]: ...

class OrderManager:
    def execute(self, plan: list[OrderRequest]) -> ExecutionReport: ...   # persist → submit → track

class Reconciler:
    def reconcile(self, expected: PortfolioState, broker: BrokerAdapter) -> ReconciliationReport: ...

# persistence (M8) --------------------------------------------------------------
class AuditRepository(Protocol):
    def record_decision(self, record: DecisionRecord) -> None: ...
    def record_order_intent(self, req: OrderRequest) -> None: ...   # BEFORE transmission
```

## 6. Future options module (not in v1)

Options are **rejected by v1 configuration** (`Instrument` refuses
`asset_class: option|future`). The design leaves room for them without
contaminating v1:

* `OptionContract(Instrument-like)` with underlying, expiry, strike, right, multiplier — a *separate* model, not extra optional fields on `Instrument`.
* `quant/options/` for chains, IV surfaces, Greeks (pricing models behind an interface).
* Risk engine gains Greek-based rules (portfolio delta/gamma/vega limits, assignment and early-exercise risk) as additional `RiskRule`s.
* `OrderRequest` gains a multi-leg sibling (`ComboOrderRequest`) handled by brokers that advertise `supports_multi_leg` in `BrokerCapabilities`.
* Target-portfolio exposure is generalised from "weights" to "delta-equivalent exposure", which the current `net_underlying_exposure` already approximates for leveraged ETFs.
