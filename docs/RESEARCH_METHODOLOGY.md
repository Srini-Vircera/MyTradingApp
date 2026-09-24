# Strategy research and backtesting methodology

Goal: find strategies whose *out-of-sample, cost-adjusted, risk-adjusted*
behaviour is robust — not the highest historical CAGR. Every result is
hypothetical until validated in paper trading.

## 1. Data foundation (M2)

* **Sources:** daily OHLCV for QQQ, TQQQ, SQQQ, SPY; NDX where available; intraday bars for the near-close proxy.
* **Adjusted vs. unadjusted:** signals use split/dividend-*adjusted* series (total-return consistent); order sizing and fills use *unadjusted* prices as the broker sees them. Both are stored.
* **Validation:** missing sessions vs. exchange calendar, duplicates, non-positive or impossible prices (high < low, close outside [low, high]), out-of-order timestamps, suspicious jumps (|return| > `data.max_abs_daily_return` without a corporate action), staleness.
* **Synthetic leveraged history.** Before TQQQ/SQQQ inception (Feb 2010) returns are synthesised *daily* from the underlying:

  `r_LETF(t) = L · r_under(t) − expense_ratio/252 − (L − 1) · (rf(t) + swap_spread)/252`

  where `rf` is the daily risk-free rate. For TQQQ (L = 3) the fund pays financing on 2× borrowed notional; for SQQQ (L = −3) the same formula yields a financing *credit* on 4× (collateral plus short swap), reduced by the swap spread. `swap_spread` is a configurable assumption, calibrated on the real overlap period. Daily compounding makes volatility drag appear naturally. The series is calibrated on the overlapping real period (tracking-error report) and **stored under a separate `synthetic` source tag**. Reports always show synthetic and real periods separately.
* **Survivorship:** the v1 universe is fixed ETFs, so survivorship bias is minor; the pre-inception period is the real issue and is handled above.

## 2. Candidate strategy catalogue (M4)

~20 candidates across all requested families. Each outputs a continuous score in
[−1, +1] with `confidence`, `suggested_exposure`, `reason` and the indicator
values used. Examples (parameters are *neighbourhoods*, not choices):

| Family | Candidate | Core idea |
|---|---|---|
| Long-term trend | `ltt_sma_distance` | tanh-scaled distance of price from SMA(200…260) |
| Long-term trend | `ltt_sma_slope` | sign & magnitude of SMA slope |
| Intermediate trend | `it_ma_stack` | ordering of MA(20)/MA(50)/MA(100) |
| Short-term trend | `st_ema_cross` | EMA(5/10) vs EMA(20) |
| Momentum | `mom_multi_horizon` | vol-scaled returns over 5/10/20/60/120 d, blended |
| Momentum | `mom_time_series_12_1` | classic TSMOM excluding last month |
| Momentum acceleration | `mom_accel` | change in 20 d momentum vs its value 10 d ago |
| Breakout | `bo_donchian` | close vs rolling N-day high/low |
| Breakout + momentum | `bo_mom_confirmed` | breakout only if momentum positive |
| Breakout + volatility | `bo_bb_squeeze` | Bollinger width percentile low → breakout direction |
| Long mean reversion | `mr_rsi_dip_in_uptrend` | RSI(2–5) oversold while above long SMA |
| Long mean reversion | `mr_drawdown_dip` | N-day drawdown beyond k·ATR in bull regime |
| Defensive mean reversion | `dmr_extension_trim` | reduce (not short) when price ≫ SMA in z-score terms |
| Market extension | `ext_bb_percent` | %B / distance-from-MA extremes |
| Bearish momentum | `bear_confirmed_trend` | small SQQQ only when trend *and* momentum bearish |
| Volatility regime | `vol_regime_percentile` | realised vol percentile → low/normal/elevated/extreme |
| Trend + volatility | `trend_vol_filter` | trend score damped in elevated/extreme vol |
| Drawdown-aware | `dd_aware_trend` | trend score scaled by index drawdown depth |
| Regime transition | `regime_transition` | detects trend flips + vol spikes (reduces exposure early) |
| Benchmark controls | `always_long_qqq`, `always_cash` | sanity baselines every candidate must beat |

## 3. Backtesting methodology (M5)

**Event loop.** For each session *t*: build a point-in-time `MarketDataView(as_of=t_signal)` → strategies → ensemble → allocation → **the same risk engine used live** → target → simulated orders → fills at *t_fill* → mark-to-market.

**Execution timing models** (explicit, never implicit):

| Model | Signal uses | Fill at | Use |
|---|---|---|---|
| `near_close` (default) | intraday price at close − N min (proxy) | close(t) ± slippage | matches the live schedule |
| `next_open` | close(t) | open(t+1) ± slippage | conservative daily model |
| `next_close` | close(t) | close(t+1) ± slippage | most conservative |
| `closing_auction` | close(t) | close(t) | only if explicitly selected; flagged in reports |

When intraday data is unavailable for history, `near_close` falls back to a
documented approximation (e.g. signal on close(t−1) with today's open-to-close
path excluded) and the report says so.

**Costs:** commission (per share / per order), half-spread, slippage (bps, optionally volatility-scaled), square-root market-impact approximation, execution delay, optional partial fills. All from config; defaults conservative.

**Accounting:** Decimal cash ledger, fractional shares if allowed, daily NAV, trade list with entry/exit, holding periods.

**Metrics** for every run: CAGR, total return, volatility, downside volatility, max drawdown & duration, Sharpe, Sortino, Calmar, win rate, profit factor, average/median/best/worst trade, average holding period, turnover, exposure (gross and beta-equivalent), best/worst month and year, % winning months, VaR/ES (historical, 95%/99%), benchmark-relative return, beta, correlation to QQQ.

**Benchmarks:** SPY, QQQ, TQQQ buy-and-hold (real + synthetic segments), cash (T-bill proxy).

**Charts:** equity (linear and log), drawdown, rolling return, rolling Sharpe, monthly heatmap, annual bars, exposure and allocation over time.

## 4. Walk-forward validation (M6)

Anchored and rolling schemes, e.g. train 8 y → validate 2 y → test 1 y, step 1 y.
Parameters and ensemble weights are chosen **only** on train/validate data; the
test slice is never touched during selection. Out-of-sample slices are stitched
into one OOS equity curve. Reports show in-sample, validation and OOS
performance side by side, with the in-sample/OOS Sharpe decay ratio.

## 5. Parameter robustness (M6)

* Evaluate each strategy over its `param_grid` neighbourhood; heatmaps for 2-D grids.
* **Robustness score** = median neighbourhood metric / best metric, penalised by the dispersion across neighbours and by how many neighbours are negative. A sharp peak (SMA 247 good, 240/250 bad) scores low and is flagged `potentially_overfit`.
* Prefer the *centre of a plateau*, not the argmax.

## 6. Multiple-testing protection (M6)

* Count every trial (strategy × parameter set × variant) in a trial registry.
* **Deflated Sharpe Ratio** (Bailey & López de Prado) using the number of trials and the variance of trial Sharpes, plus skew/kurtosis.
* **Probability of Backtest Overfitting** via Combinatorially Symmetric Cross-Validation.
* **Stationary block bootstrap** confidence intervals for Sharpe/CAGR/MDD.
* **White's Reality Check / Hansen SPA–style** test of the best strategy against the benchmark across the trial set.
* **False discovery awareness:** report Benjamini–Hochberg adjusted p-values for "Sharpe > 0" across candidates.

## 7. Monte Carlo (M6)

Vary: trade-sequence (bootstrap of trade returns), block-bootstrapped daily returns, slippage and fill prices, parameter jitter within the plateau, start date, signal delay (0/1/2 days). Report median, 75th/90th/95th percentile and worst case for CAGR, max drawdown, worst year, Sharpe and ending equity — rounded to avoid false precision.

## 8. Strategy selection and ensemble (M6/M7)

A strategy is ranked on a scorecard, not on CAGR:

| Criterion | Weight (initial, configurable) |
|---|---|
| Walk-forward OOS Sharpe / Sortino | high |
| OOS max drawdown & Calmar | high |
| Parameter robustness score | high |
| Deflated Sharpe / PBO | gate (must pass) |
| Consistency across regimes & decades | medium |
| Marginal contribution to ensemble (correlation) | medium |
| Turnover & execution feasibility | medium |

Ensemble weighting: equal, fixed, inverse-volatility/risk-parity, and
walk-forward-optimised (fit on train only, shrunk towards equal weight).
Concentration controls: `max_family_weight`, and strategies with pairwise
return correlation above `max_pairwise_correlation` are clustered and share one
cluster weight.

## 9. Governance

Every strategy version has an ID, version, parameters, creation date, notes,
backtest and walk-forward results, and a lifecycle state
(RESEARCH → VALIDATED → PAPER → SHADOW → LIVE_APPROVED, or DISABLED).
Automation may only move RESEARCH → VALIDATED (when gates pass) and may always
disable; all other promotions require a named human and a written
justification (`governance/lifecycle.py`, implemented in M1).
