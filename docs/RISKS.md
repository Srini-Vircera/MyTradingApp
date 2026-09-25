# Risk register

Financial and implementation risks, and how the design mitigates each one.
Nothing here eliminates risk; historical performance is hypothetical.

## Financial risks

| Risk | Why it matters here | Mitigation |
|---|---|---|
| **Leveraged-ETF volatility drag** | TQQQ/SQQQ reset daily; in choppy markets they lose value even if QQQ ends flat. 3x leverage does *not* mean 3x the long-run return. | Synthetic history built from *daily* compounding with expense and financing costs (M2); volatility targeting and regime filters (M7); SQQQ capped (`max_inverse_net_exposure`). |
| **Gap / crash risk** | A −20% one-day index move (the S&P 500 fell ~20% on 19 Oct 1987) would be roughly −60% for a 3x daily ETF, before a daily system could react. | Net exposure caps, drawdown bands, Monte Carlo and stress scenarios (M6) that include synthetic crash days; position caps below 100% leveraged. |
| **Inverse-ETF decay** | SQQQ has lost well over 99% of its value since its 2010 inception; it is a short-horizon instrument. | Separate, conservative cap; bearish strategies must beat "cash" OOS, not just "long TQQQ". |
| **Overfitting / data mining** | 15–30 strategies × parameter grids = thousands of trials; the best backtest is almost certainly lucky. | Walk-forward only, parameter plateaus, Deflated Sharpe, PBO, bootstrap, ranking that penalises fragility (M6). |
| **Regime dependence** | 2010–2021 was an exceptional Nasdaq bull market; results may not generalise. | Report performance by regime and decade; include 2000–2002 and 2008 via synthetic LETF data (clearly labelled). |
| **Execution slippage vs. backtest** | Near-close signals use a proxy price; fills differ from the close. | Execution model matches the live methodology; paper-vs-backtest reconciliation report (M13); slippage Monte Carlo. |
| **Tax & turnover drag** | Frequent rebalancing of leveraged ETFs creates short-term gains. | Turnover in every report; rebalance threshold; turnover limits. (Tax modelling out of scope for v1.) |
| **Liquidity/halts** | ETF trading halts or LULD pauses near close. | Refuse on halted/untradeable assets (broker asset status); UNKNOWN orders investigated. |

## Implementation risks

| Risk | Mitigation |
|---|---|
| **Look-ahead bias** | `MarketDataView` exposes only rows ≤ `as_of`; `StrategySignal` rejects `data_timestamp > timestamp`; indicator tests compare streaming vs batch computation (M3). |
| **Same-bar execution** | Backtester enforces a configurable execution lag; using close T and filling at close T requires the explicit closing-auction model. |
| **Duplicate orders** (timeouts, restarts, retries) | Deterministic `client_order_id`; intents persisted before sending; `UNKNOWN` never resubmitted; planner nets pending open orders (M9). |
| **Stale/missing data** | Validation + staleness checks feed the pre-trade gate (fail closed). |
| **Broker/API outages** | Timeouts → `BrokerError`; unconfirmable state → refuse; alerts. |
| **Clock/timezone/DST errors** | UTC internally; naive datetimes rejected; exchange calendar for holidays/early closes. |
| **Configuration mistakes** | Strict schema (unknown keys rejected), cross-field validation, `aq config validate` in CI, config fingerprint stored with each decision. |
| **Accidental live trading** | Six independent opt-ins; broker/mode mismatch check; shipped configs never live. |
| **Secret leakage** | Secrets only via env; YAML secret scanner; log redaction; `.env` git-ignored. |
| **Silent failures** | No silent exception handling (lint rule `BLE`); every failure is logged, alerted, and blocks trading. |
| **Floating-point in money** | `Decimal` for orders/balances; quantities rounded toward zero. |
| **Database outage mid-cycle** | Intent must persist before transmission, so a DB outage blocks new orders (fail closed) (M8/M9). |
| **Operator error** | Readable errors with hints; kill switch in CLI and dashboard; deliberate confirmation to re-enable. |
