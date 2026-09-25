# Backtesting

Milestone 5. Code: `src/adaptive_quant/quant/backtest/` (engine) and `src/adaptive_quant/quant/analytics/` (metrics, report).
Configuration: `config/base.yaml → backtest`.

> **All backtest results are hypothetical.** Fills, costs and data quality are modelled
> assumptions. The shipped strategies are untested hypotheses. No parameter has been
> optimised, and no performance claim is made — especially for synthetic or unvalidated data.

## Running

```bash
aq data download                                   # validated, stored history first
aq backtest run --strategy ltt_sma_distance        # real data only
aq backtest run --strategy ltt_sma_distance --synthetic         # + labelled synthetic pre-2010 history
aq backtest run --strategy st_ema_cross --execution next_open --delay 1
aq backtest run --strategy a --strategy b          # several strategies: naive equal-weight average
```

**Output directory.** Each run writes to `var/reports/<timestamp>-<ids>/`:

| File | Contents |
|---|---|
| `report.html` | Self-contained report: charts and tables; no external assets or network requests |
| `metrics.json` | Every metric |
| `daily.csv` | One row per session |
| `fills.csv` | Every fill, with decision time, data time and cost breakdown |
| `trades.csv` | Round trips |
| `decisions.csv` | Every decision's signals, target weights and caps |
| `cancelled.csv` | Unfilled and trimmed order quantities, with reasons |

**Defaults.**
- The default start is the first session at which every strategy is warmed up and every tradeable instrument has a *prior* close (needed for sizing).
- Command-line overrides are recorded in the report's provenance.

## Execution timing (no look-ahead)

| Model | Decision time (data visible) | Fill |
|---|---|---|
| `near_close` (default) | close(t) − 15 min; with daily data this sees bars through close(t−1) | close(t + delay) |
| `next_open` | close(t) | open(t + 1 + delay) |
| `next_close` | close(t) | close(t + 1 + delay) |
| `closing_auction` | close(t) | close(t + delay); with delay 0, **same bar**, flagged |

- **Same-bar fills.** A signal computed from today's close can only trade at today's close under the explicit `closing_auction` model. Every result flags it, as optimistic and requiring a real closing-auction process.
- **Daily-data equivalence.** On daily data, `near_close` and `next_close` produce the same fills; only the decision timestamp differs. With intraday data (future work), near-close decisions would use a fresher proxy price.

### Guarantees and how they are enforced

| Guarantee | Enforcement |
|---|---|
| Strategies see only `timestamp ≤ decision_time` | `Strategy.signal_at` slices indicators precomputed on full history. The M3 tests prove causality, and a test proves the engine's signals equal the live `generate_signal()` path. |
| Orders sized with prices known at the decision | Sizing uses the last close visible at the decision time. The engine raises `LookAheadError` if a sizing price is later than the decision. |
| Fills never use data they preceded | Every fill checks `fill_time > data_timestamp` (equality only for `closing_auction` with delay 0), otherwise `LookAheadError`. |
| Average daily volume uses only past volumes | Average volume comes from the last N sessions visible at the decision time. |
| Pending orders netted | Delayed orders count as projected positions when new orders are sized, so a strategy never double-trades. |
| Determinism | No randomness; stable ordering; bit-identical results, tested. |

## Costs, fills and partial fills

For `q` shares at reference price `P` (the open or close of the fill bar):

```
impact bps  = impact_coefficient_bps * sqrt(q / ADV)          (ADV known at decision time)
fill price  = P * (1 ± (half_spread_bps[symbol] + slippage_bps + impact bps) / 10 000)
commission  = max(commission_minimum, commission_per_order + commission_per_share * q)
```

- **Cost breakdown.** Spread, slippage and impact are attributed in dollars and sum *exactly* to `|notional − q·P|`.
- **Partial fills.** One order fills at most `max_participation × ADV` shares. The remainder is cancelled, and the next decision re-targets.
- **Unknown volume.** If volume is unknown (e.g. synthetic bars), `unknown_volume: assume_liquid` (flagged) or `reject` applies.
- **Cash limits.** Buys are trimmed to the available cash. Sells execute before buys at the same fill point, and the ledger can never go negative.
- **Sizing buffer.** `sizing_cash_buffer` (0.5%) keeps room for costs and overnight moves.
- **Rebalance band.** `trading.rebalance_threshold_weight` suppresses small rebalances; full exits always go through.

## Accounting (exact Decimal)

- **Cash.** Every cash flow is in cents (banker's rounding). Prices use a 1e-6 tick; quantities are 1e-6 shares (fractional) or whole shares.
- **Positions.** Positions are FIFO lots; realized P&L is FIFO. Round trips are net of all costs and feed the win/loss statistics.
- **Identity.** This identity is checked **every session**; the engine raises if it is off by even one cent:
  - `equity = cash + market value`
  - `equity = initial + realized + unrealized − commissions + interest`
- **Price basis.** Prices are **total-return adjusted** for both signals and accounting. Cash dividends are therefore reinvested implicitly (returns are correct; share counts differ from the historical raw share counts).

## Allocation and risk (Milestone 7)

By default (`backtest.allocation.risk_engine: true`) every decision goes through the same chain as live trading:

1. **Ensemble.** Strategy weighting, clustering and family caps.
2. **Allocation policy.** Exposure is mapped to QQQ / TQQQ / SQQQ; inverse exposure is held, never shorted.
3. **Risk engine.** Volatility target, drawdown bands with hysteresis, regime caps, exposure caps and turnover. See [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md#implementation-notes-m7).

**Point-in-time.** The risk context is built from data visible at the decision:
- equity at the decision's sizing prices;
- peak equity and the previous session's equity from past marks;
- underlying returns through the decision time;
- current weights including pending orders.

**Refusals.** A risk failure (e.g. too little history) **refuses** the decision: no orders are placed, and the refusal is recorded and counted in the report.

**Records.** Each decision records its ensemble weights, band, volatility scale, flags and every adjustment (in `decisions.csv`).

**Other modes.**
- `risk_engine: false` keeps the M5 interim static caps, for comparison only.
- `apply_risk_limits: false` disables limits entirely; engine unit tests only.

## Metrics

The definitions are in `quant/analytics/metrics.py`, and regression tests with hand-derived values are in `tests/regression/test_metrics_golden.py`.

| Group | Metrics |
|---|---|
| Returns | total return, CAGR (calendar days / 365.25), annualized return (252 periods) |
| Risk | volatility, downside volatility, Sharpe, Sortino, max drawdown and its duration, Calmar, historical VaR and ES at 95% / 99% |
| Periods | best/worst month and year, % winning months (exchange-local calendar periods) |
| Trading | average exposure, time invested, annual turnover; trades, win rate, profit factor, average / median / best / worst trade, average holding period |
| Relative | beta, correlation and CAGR difference vs QQQ |

Undefined values show as "-", never ∞.

**Benchmarks:**
- SPY, QQQ and TQQQ buy-and-hold (total return, no costs), plus cash at `cash_interest_annual`.
- A benchmark without data for the whole period is omitted with a note; it is never partially plotted.

## Synthetic data separation

With `--synthetic`, TQQQ/SQQQ histories are extended with **validated** synthetic data (M2 tracking-error gate). Failed synthetic series are never used. Every row keeps its origin:

- the report banner states the synthetic date range;
- time charts shade and label the synthetic period;
- metrics are reported three times: full period, **real data only**, and **synthetic period only**;
- the provenance table lists the number of synthetic rows per symbol.

## Tests that guard the backtester

| Area | Tests |
|---|---|
| Look-ahead / timing | `tests/unit/backtest/test_lookahead_timing.py`: exact fill bar and price per model; `fill_time > data_timestamp`; same-bar only under the auction model; exact delay shifts; future-price poisoning leaves everything up to T bit-identical; the sign-of-return oracle has no edge on i.i.d. data (and a leaking engine would score Sharpe > 5); decision-time sizing; fast path equals live path; runtime guards |
| Accounting | `test_portfolio.py`, `test_engine_accounting.py`: hand-computed round trip; FIFO splits exact to the cent; identity over 300 random trades; frictionless buy-and-hold tracks price exactly; costs reduce equity by exactly their amount |
| Costs | `test_costs.py`: hand-calculated quotes; exact attribution sums; participation cap |
| Determinism | identical fingerprints across runs and input orderings; inputs never mutated |
| Metrics | `tests/regression/test_metrics_golden.py` |
| End-to-end | `tests/integration/test_cli_backtest.py`: real-only, synthetic-labelled, closing auction, delay, multi-strategy, no-secret-leak |

## Known limitations

- Daily bars only. Intraday near-close proxies, auction-imbalance and halt modelling are future work.
- Ensemble weights use frictionless shadow returns (a proxy); the kill switch is never engaged in simulations.
- Commission defaults are 0 (typical for US ETF brokers today); spreads and slippage are assumptions to calibrate against paper-trading fills (Milestone 13).
- Taxes, borrow costs and dividend withholding are not modelled.
- Results on generated or synthetic data say nothing about real markets.
