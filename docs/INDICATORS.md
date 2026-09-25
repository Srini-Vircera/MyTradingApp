# Indicator library

Milestone 3. Code: `src/adaptive_quant/quant/indicators/`.

## Guarantees

1. **Point-in-time (no look-ahead).** The value at row *t* uses rows ≤ *t* only. This is verified for every configuration by two independent tests (`tests/unit/indicators/test_lookahead.py`):
   - **Truncation test:** computing on `bars[:t+1]` must equal the full-history value at *t*.
   - **Future-perturbation test:** changing every row after *t* must leave all values up to *t* unchanged.
2. **Strict warm-up.** An indicator is `NaN` until its full window exists; it never produces a partial-window value. Each indicator declares its warm-up (the number of leading NaN rows on clean input), and tests check the declaration against the actual output.
3. **Fail loudly on bad input.**
   - Allowed: leading NaN, such as an upstream indicator's warm-up.
   - Errors: NaN after the first valid value (a gap), infinite values, non-positive prices where logs or ratios are taken, and invalid parameters.
   - Degenerate cases (zero volatility, flat bands) return `NaN`, not ±∞.
4. **Two evaluation paths, same answer.**
   - `IndicatorEngine.compute()` runs over the whole history. It is fast and meant for backtests.
   - `IndicatorEngine.snapshot(bars, as_of)` truncates the bars to `timestamp ≤ as_of` *before* computing, so it is point-in-time by construction. Strategies use this path through `MarketDataView`.

## Definitions

In the table, *n* is the window; *x* the input series (default `close`); *r* a return. The warm-up column gives the index of the first valid value.

| Kind (registry name) | Definition | Warm-up |
|---|---|---|
| `sma` | mean of the last *n* values | n − 1 |
| `ema` | α = 2/(n+1); seeded with the SMA of the first *n* values, then e_t = α·x_t + (1−α)·e_{t−1} | n − 1 |
| `distance_from_ma` | x / MA_n − 1 (MA = SMA or EMA) | n − 1 |
| `rolling_zscore` | (x − mean_n) / std_n (sample, ddof 1) | n − 1 |
| `trend_slope` | OLS slope of ln(x) vs bar number over *n* bars; ×252 when annualized | n − 1 |
| `trend_r2` | R² of that fit (0..1); NaN for a perfectly flat window | n − 1 |
| `rate_of_change` | x_t / x_{t−n} − 1 | n |
| `momentum` | ln(x_{t−skip} / x_{t−n}): log return, excluding the last *skip* bars (12-1 momentum: n=252, skip=21) | n |
| `momentum_acceleration` | ROC_n(t) − ROC_n(t − lag) | n + lag |
| `rsi` | Wilder: average gain and loss smoothed by RMA (seeded with the mean of the first *n* changes); 100 − 100/(1+RS). 100 when there are no losses; 50 when flat | n |
| `rolling_std` | rolling standard deviation (ddof 1 by default, or 0) | n − 1 |
| `historical_volatility` | sample std of the last *n* log returns × √252 | n |
| `true_range` | max(H−L, \|H−C_{t−1}\|, \|L−C_{t−1}\|); NaN on the first bar | 1 |
| `atr` | Wilder RMA of the true range, seeded with the mean of the first *n* TRs | n |
| `bollinger` (output = middle / upper / lower / width / percent_b) | middle = SMA_n; bands = middle ± k·σ_pop (ddof 0, Bollinger's convention); width = (U−L)/middle; %B = (x−L)/(U−L) | n − 1 |
| `volatility_percentile` | share of the last *lookback* historical-volatility readings (including the current one) that are ≤ the current reading; range (0, 1] | vol_window + lookback − 1 |
| `rolling_high` / `rolling_low` | max/min of the last *n* bars (default sources: high / low). `include_current=False` gives the prior *n* bars, i.e. the level a breakout must cross | n − 1 (n if excluding current) |
| `drawdown` | x / peak − 1 ≤ 0. Peak is expanding by default, or the max of the last *n* bars | 0 (n − 1) |
| `drawdown_duration` | bars since the running peak | 0 |
| `rolling_sharpe` | mean(r − rf/252) / std(r) × √252 over the last *n* simple returns; NaN at zero volatility | n |

## Using indicators

```python
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.indicators.engine import IndicatorEngine

engine = IndicatorEngine([
    IndicatorSpec("sma", {"window": 200}),                         # -> "sma_200"
    IndicatorSpec("rsi", {"window": 14}),                          # -> "rsi_14"
    IndicatorSpec("rolling_high", {"window": 20, "include_current": False}),
    IndicatorSpec("bollinger", {"window": 20, "output": "width"}), # -> "bollinger_20_2_width"
])
engine.warmup                       # history rows needed before every value exists
snap = engine.snapshot_view(view, "QQQ")   # values known at view.as_of
snap.values                         # {"sma_200": 402.1, "rsi_14": 55.3, ...}; None = warming up
snap.require("sma_200", "rsi_14")   # raises MissingDataError if any is unavailable
```

Specs are validated when they are created. Unknown kinds, unknown parameters, wrong types and invalid values raise immediately, so configuration errors surface at startup. `snap.values` has the same shape as `StrategySignal.indicator_values`, which keeps the audit trail direct.

## Assumptions and caveats

- **Path-dependent indicators depend on where history starts.** EMA, RSI, ATR (recursive smoothing) and the expanding `drawdown`/`drawdown_duration` are causal, but their values depend on the first bar of the series. Recursive smoothing converges within a few windows; expanding drawdown never "forgets". Backtests and live evaluation must therefore run on the same stored series with the same start, which the M2 store provides. Milestone 5 will make both paths load identical history.
- **Choice of input series.** Indicators are computed on whatever frame they are given. For signals on history that spans splits and dividends, use the **total-return (`all`) adjusted** series. For price-level comparisons against live quotes (e.g. a breakout above yesterday's high), use a series on the same basis as the live price.
- **Annualisation.** It uses 252 trading periods per year for daily data.
- **RSI reference values.** The RSI matches a hand calculation of Wilder's method exactly. On the widely cited StockCharts worked example it gives 70.46 where StockCharts publishes 70.53. The difference (≤ 0.07) is because that spreadsheet uses unrounded prices while displaying rounded ones; the tests use the displayed prices.
- **Performance.** All 34 test configurations together take ~0.07 s on 27 years of daily bars.
