# Strategies

Milestone 4. Code: `src/adaptive_quant/quant/strategies/`. Configuration: `config/strategies.yaml`.

> Every strategy here is a **hypothesis**. None is claimed to have an edge.
> Milestones 5–6 (backtests with costs, walk-forward, robustness,
> multiple-testing controls) decide which, if any, deserve paper trading.
> Scoring constants and parameters are candidates, not recommendations.

## Contract

```python
signal = strategy.generate_signal(view)   # view: point-in-time MarketDataView
```

Every strategy returns a `StrategySignal` containing:
- `normalized_score` in [−1, +1], with the direction (bullish / neutral / bearish) derived from it;
- `raw_score`, `confidence` in [0, 1], and `suggested_exposure` (net QQQ-equivalent exposure; the risk engine has the final say);
- a readable `reason`;
- the indicator values used, the strategy `version_id`, and `data_timestamp` (the last bar actually used).

The base class owns every safety-relevant step, so no strategy can bypass it:

| Guarantee | How |
|---|---|
| **No look-ahead** | Data comes only from `MarketDataView`, which shows rows with `timestamp ≤ as_of`. Daily bars are stamped at the close, so a 15:45 evaluation sees yesterday's bar. `data_timestamp ≤ timestamp` is enforced by the signal model. |
| **No partial warm-up** | Fewer than `warmup_bars` rows raises `InsufficientHistoryError`. An undefined (NaN) indicator raises `StrategyError`. |
| **Bounded output** | Score, confidence and exposure are checked, as are the strategy's own long/short limits. Bearish-only strategies can never score > 0; "never short" strategies can never suggest negative exposure. |
| **Isolation** | Strategies can't import broker, network, provider, store or secrets code. A static test scans the source for this. |
| **Fail closed** | `run_strategies` records any exception as a failure. A batch with any failure, a not-ready strategy or no signals is not `ok`, and the pre-trade gate then refuses with `SIGNAL_FAILURE`. |

Optional data (NDX) is used only when present *and* fully warmed up. Otherwise the reason text says it wasn't used; it's never an error.

## Catalogue

Twenty candidates plus two benchmarks, covering all 15 families:

| id | family | idea (score) |
|---|---|---|
| `ltt_sma_distance` | long-term trend | tanh((P/SMA_n − 1)/scale); halved if NDX disagrees (NDX optional) |
| `ltt_trend_slope` | long-term trend | tanh(annualized log slope / scale); confidence = fit R² |
| `it_ma_stack` | intermediate trend | mean vote of price vs fast, fast vs mid, mid vs slow, with a dead band against noise |
| `st_ema_cross` | short-term trend | tanh((EMA_fast/EMA_slow − 1)/scale) |
| `mom_multi_horizon` | momentum | mean volatility-scaled log momentum over 5/10/20/60/120 days |
| `mom_time_series` | momentum | 12-1 month momentum, volatility-scaled |
| `mom_acceleration` | momentum acceleration | tanh(ΔROC/scale); halved when opposing the current move |
| `bear_confirmed_trend` | bearish momentum | **bearish only**, when price < SMA, momentum < 0 and slope < 0. Inverse exposure is capped at 0.45 (~15% SQQQ) |
| `bo_donchian` | breakout | position of the close within the prior-N-day channel |
| `bo_momentum_confirmed` | breakout + momentum | channel position; cut to 25% unless momentum agrees |
| `bo_bollinger_squeeze` | breakout | %B direction; full strength only after a low-volatility squeeze |
| `bo_trend_volume` | breakout | trend-aligned break of the prior high/low; volume-confirmed = 1.0, else 0.6 |
| `mr_rsi_dip` | mean reversion (long) | short-RSI oversold *while above the 200-day SMA* |
| `mr_drawdown_dip` | mean reversion (long) | short-term pullback ≥ k ATR in an uptrend |
| `dmr_extension_trim` | defensive mean reversion | negative score when stretched far above trend. Trims toward cash and **never shorts** |
| `ext_bollinger_percent_b` | market extension | contrarian %B; negative side means cash, never short |
| `vol_regime` | volatility regime | low / normal / elevated / extreme regime mapped to a risk-appetite score (for sizing) |
| `trend_vol_filter` | trend + volatility | trend score, with the bullish side damped in elevated/extreme volatility |
| `dd_aware_trend` | drawdown-aware | bullish trend damped as the 1-year drawdown deepens (windowed peak, so it doesn't depend on where history starts) |
| `regime_transition` | regime transition | fresh cross of the long SMA, amplified by the change in volatility |
| `baseline_buy_hold` | benchmark | always +1 (long QQQ) |
| `baseline_cash` | benchmark | always 0 |

Run `aq strategies list` for versions and warm-up requirements. Each implementation's parameters (types, bounds, defaults, descriptions) are defined by its `param_specs` in code.

## Configuration and governance

`config/strategies.yaml` lists each strategy with its `id`, `implementation`, `family`, `lifecycle`, `params` and `param_grid`.

**Validation.** `aq strategies validate` and `aq config validate` check every entry:
- the implementation exists and its family matches;
- every parameter *and every param_grid value* passes the schema, including cross-parameter rules such as fast < slow.

All errors are reported together.

**Versions.** Each strategy gets a `version_id = implementation@code_version#param_hash`. It changes whenever the code version or any parameter changes, and it is stamped on every signal.

**Lifecycle and eligibility by trading mode**, enforced in `StrategyCatalog.eligible()`:

| Mode | Lifecycle states that may produce signals |
|---|---|
| backtest / research | research, validated, paper, shadow, live_approved |
| shadow, paper | paper, shadow, live_approved |
| live | **live_approved only** |

`disabled` strategies (or `enabled: false`) never run.

**Promotion rules:**
- Declaring `live_approved` in config requires an `approval:` block with `approved_by`, `approved_on` and a written `justification` of at least 20 characters. Software never promotes (see `governance/lifecycle.py`).
- The shipped configuration keeps everything in `research`, so **paper and live modes currently have zero eligible strategies**. Promotion is a deliberate human step, taken after Milestone 6 evidence.

## Research command

```bash
aq strategies signals --as-of 2024-06-28          # after that session's close
aq strategies signals --as-of 2024-06-28T15:45:00-04:00
aq strategies signals --mode paper                 # only strategies eligible for paper
```

- **Data:** it reads stored, validated, total-return-adjusted daily bars (`aq data download` first).
- **Read-only:** it prints signals and places **no orders**.
- **Modes:** `live` is not a selectable mode.
- **Exit code:** 2 if any strategy failed or wasn't warmed up.

## Known limitations

- **Performance:** a signal recomputes its indicators over the visible history each time. That's fine for daily live use. The Milestone 5 backtester should compute indicators once on the full series and slice them, which the M3 look-ahead tests show gives identical values.
- **Volume data:** `bo_trend_volume` depends on volume. The free Alpaca IEX feed reports partial volume, and synthetic history has none (the strategy then reports "volume unavailable" and uses 0.6 strength).
- **Scoring constants:** scales and multipliers (e.g. the 0.25 cut, the 0.6 strength) are design choices to be examined in the Milestone 6 parameter-robustness work.
