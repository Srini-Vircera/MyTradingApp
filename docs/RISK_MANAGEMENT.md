# Risk-management architecture

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
