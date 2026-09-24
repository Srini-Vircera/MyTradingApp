# Risk-management architecture

> **Implemented in Milestone 7:** `quant/risk/engine.py` (rules), `quant/ensemble/` (strategy
> weighting), `quant/portfolio/` (allocation policy and the decision chain), and
> `trading/safety/risk_checks.py` (pre-trade gate). Backtests and research use the same code by
> default (`backtest.allocation.risk_engine: true`). The implementation details are in
> [Implementation notes](#implementation-notes-m7) below.

The risk engine (M7) is a separate module with **final authority**. Strategies
and the ensemble only *propose* a portfolio; the risk engine returns an
approved `TargetPortfolio` plus an ordered list of `RiskAdjustment`s explaining
every change (rule, value before, value after, reason). If any risk
calculation fails, the cycle is refused (fail closed).

All limits live in `config/risk.yaml`, are validated at startup (bands must
tighten as drawdown deepens, etc.) and are fingerprinted into `config_version`.
The shipped numbers are **examples for research**, not recommendations.

## Pipeline (applied in this order)

```
ProposedPortfolio
  │
  ├─ 1. Sanity          weights finite, ≥ 0, known & tradeable symbols; sum ≤ 1
  ├─ 2. Hard state      kill switch / emergency band / daily loss limit
  │                      → freeze risk-increasing changes (risk-reducing still allowed)
  ├─ 3. Volatility target   scale = clamp(target_vol / est_vol, min_scale, max_scale)
  │                      est_vol = forecast vol of the *proposed* portfolio (EWMA of
  │                      underlying × beta-equivalent exposure); max_scale ≤ 1 by
  │                      default: vol targeting may only reduce exposure
  ├─ 4. Drawdown band   current drawdown from peak equity → band → cap on net
  │                      underlying exposure (normal / caution / defensive / emergency)
  ├─ 5. Volatility regime   optional extra cap per regime (low/normal/elevated/extreme)
  ├─ 6. Exposure caps   max_net_underlying_exposure, max_inverse_net_exposure,
  │                      max_leveraged_etf_weight, per-symbol max_position_weight,
  │                      max_gross_exposure, min_cash_weight
  ├─ 7. Turnover        limit Σ|Δw| to max_daily_turnover (scale deltas proportionally,
  │                      risk-reducing deltas prioritised)
  └─ 8. Order-level     max_order_notional, buying power (checked by the order planner)
  ▼
RiskDecision { approved TargetPortfolio, adjustments[], band, vol_scale, flags }
```

Scaling down a leveraged position moves weight into **cash**, never into
another risky asset. Reductions are applied to the most leveraged exposure first.

### Risk-increasing vs. risk-reducing

An order is *risk-reducing* if it moves the portfolio's absolute net underlying
exposure **and** gross exposure toward zero without increasing any single
position. Only these may proceed when the kill switch is engaged, the emergency
band is active or the daily loss limit is hit. The `OrderRequest.risk_increasing`
flag is computed by the planner and stored with every order.

## Drawdown bands (example)

| Band | Drawdown ≥ | Max net exposure | Risk-increasing orders |
|---|---|---|---|
| normal | 0% | 2.0× | allowed |
| caution | 10% | 1.5× | allowed |
| defensive | 20% | 0.75× | allowed |
| emergency | 30% | 0× | **blocked** |

Band thresholds and caps are research parameters: M6 backtests compare
alternatives. Hysteresis (e.g. must recover to 25% to leave emergency) is
configurable to avoid flapping.

## Independent safety layers (defence in depth)

1. **Config validation** — impossible or inconsistent limits cannot load.
2. **Risk engine** — portfolio-level limits.
3. **Order planner** — per-order caps, buying power, lot sizes (quantities rounded toward zero).
4. **Pre-trade gate** — refuses on stale/missing data, unconfirmed positions/equity, duplicates, uncertain open orders, signal/risk failure, reconciliation failure, kill switch (implemented in M1: `trading/safety/preflight.py`).
5. **Order state machine** — UNKNOWN is never resubmitted (M1: `trading/orders/state_machine.py`).
6. **Reconciliation** — post-cycle; discrepancies halt normal trading and alert.
7. **Kill switch** — manual or automatic; fail-closed (M1: `trading/safety/kill_switch.py`).

## Implementation notes (M7)

**Decision chain** (`PortfolioManager.decide`): strategy signals → ensemble weights and
combination → allocation policy → risk engine. The same chain runs in the backtester, the
research pipeline and (from M10) the live cycle.

**Ensemble** (`strategies.yaml → ensemble`):
- **Weighting methods:**
  - `equal`, `fixed`;
  - `risk_adjusted`: inverse volatility;
  - `walk_forward`: max(Sharpe, 0), refitted every `refit_sessions` on past data only, and shrunk towards equal by `shrinkage`.
- **Shadow returns.** Weights come from point-in-time *shadow returns*: each strategy's own previous suggestion × the underlying's next return. This is a frictionless proxy, used only for weighting.
- **Short history.** With fewer than `min_history` shadow returns, data-driven methods fall back to equal weight.
- **Clustering.** Strategies whose shadow-return correlation exceeds `max_pairwise_correlation` share one cluster weight.
- **Family caps.** No family may exceed `max_family_weight`. The excess goes to the other families; if every family is capped, it stays in cash.

**Allocation policy.** Exposure is mapped to QQQ / TQQQ / SQQQ (see `portfolio/policy.py`).
- `min_abs_exposure` sets a dead band: smaller exposures are held as cash.
- Regime awareness is applied by the risk engine, not by the policy.

**Rule details:**

| Rule | Implementation |
|---|---|
| Sanity | Unknown or non-tradeable symbols, negative or non-finite weights, or a sum above 1 → **refuse** (never silently repaired) |
| Hard state | Kill switch, a blocking drawdown band, or daily loss ≥ `max_daily_loss` (equity vs the previous session's close) → *freeze*: every weight is capped at its current value. If cutting one leg would raise \|net\| (e.g. selling only the long side of a hedged book), everything is held instead |
| Volatility target | Forecast = EWMA (span `lookback_days`) volatility of the underlying × \|net exposure\|; scale never above 1 while frozen |
| Drawdown bands | Deepest band whose threshold is reached; a deeper previous band is kept until drawdown recovers `drawdown_hysteresis` below its threshold |
| Volatility regime | Mid-rank percentile of the 20-day realised volatility within `lookback_days`; optional `max_net_exposure` per regime (shipped example: elevated 1.5×, extreme 0.75×) |
| Exposure caps | Position caps → leveraged-ETF weight → gross/cash floor (most leveraged reduced first) → **combined net caps last** (configured, band and regime). The order matters: scaling both leveraged legs, or cutting SQQQ for the gross cap, can move net exposure |
| Turnover | Increases get whatever budget is left after decreases. Decreases (risk-reducing) are never blocked. Turnover and caps are iterated; if they do not converge, all increases are dropped |
| Verification | Every limit is re-checked on the rounded-down result, including "no increase while frozen" and the turnover budget. Any violation → `RiskCalculationError` |

**Failures.** Any exception inside a rule, or a failed verification, raises
`RiskCalculationError`.
- **Backtest:** the decision is recorded as **refused**. No orders are placed, positions are kept, and the report counts the refusals.
- **Live (M10):** `RiskEngineCheck` turns it into `risk_calculation_failure` (blocks all orders). It reports `drawdown_emergency` or `daily_loss_limit` as a block on risk-increasing orders only.

**Warm-up.** The engine needs `max(vol-target lookback, regime lookback + vol window − 1)`
underlying returns: 271 with the shipped config. Default backtest and research start dates
include this warm-up.

**Order-level limits.** `max_order_notional` and buying power belong to the order planner (M9).

**Tests.**
- `tests/unit/risk/test_risk_engine.py`:
  - every rule, with exact values;
  - fail-closed injection;
  - a 3,000-case randomized property test: output never violates any configured limit.
- `tests/unit/ensemble/`: the ensemble.
- `tests/unit/backtest/test_risk_integration.py`:
  - the backtester goes through the chain;
  - refuses without history;
  - a crash escalates the drawdown bands;
  - future poisoning cannot change earlier decisions.
