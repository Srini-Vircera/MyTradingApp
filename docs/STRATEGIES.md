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

Twenty-one candidates plus two benchmarks, covering all 15 families:

| id | family | idea (score) |
|---|---|---|
| `ltt_sma_distance` | long-term trend | tanh((P/SMA_n − 1)/scale); halved if NDX disagrees (NDX optional) |
| `ltt_trend_slope` | long-term trend | tanh(annualized log slope / scale); confidence = fit R² |
| `it_ma_stack` | intermediate trend | mean vote of price vs fast, fast vs mid, mid vs slow, with a dead band against noise |
| `st_ema_cross` | short-term trend | tanh((EMA_fast/EMA_slow − 1)/scale) |
| `golden_death_cross` | long-term trend | **Golden Cross / Death Cross**: +1 while the fast MA is above the slow MA (bullish regime), −1 below (bearish); default 50/200 SMA. See [below](#golden-cross--death-cross-golden_death_cross) |
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

## Golden Cross / Death Cross (`golden_death_cross`)

> A **technical-analysis hypothesis**, not a proven edge. Backtests are hypothetical;
> historical performance does not establish future profitability; parameter searches
> can overfit; leveraged or inverse variants materially increase risk.

- **Golden Cross:** the fast moving average crosses **above** the slow one.
- **Death Cross:** the fast moving average crosses **below** the slow one.
- **Default:** 50-session SMA / 200-session SMA — the classic configuration. Any other
  pair, or EMA, is a *moving-average crossover variant*; reports label it VARIANT.

**Event vs regime.** The strategy keeps both concepts separate:

| Concept | Definition | Reported as |
|---|---|---|
| Regime (persistent) | `fast > slow` bullish, `fast < slow` bearish; stays until the relationship flips | `regime` (+1/−1; 0 only before the averages have ever differed) |
| Cross event (once) | Golden: `prev fast <= prev slow` and `fast > slow`; Death: `prev fast >= prev slow` and `fast < slow`. Days of exact equality are looked through, so below → equal → above is **one** Golden Cross (on the "above" bar) and above → equal → above is **no** event | `cross_event` (+1/−1 on that bar), `last_cross`, `bars_since_cross`, and the reason text ("most recent: Golden Cross on 2019-03-25") |

Staying above the slow MA never reports another Golden Cross. Signal values also carry
`fast_ma` and `slow_ma`.

**Parameters** (validated; invalid values fail closed with a clear error):

| Parameter | Default | Allowed |
|---|---|---|
| `fast_period` | 50 | integer 2 … 250 |
| `slow_period` | 200 | integer 3 … 400, **greater than** `fast_period` |
| `ma_type` | `SMA` | `SMA`, `EMA` (the platform's SMA-seeded EMA, α = 2/(n+1)) |
| `bearish_action` | `cash` | `cash`, `qqq_reduced`, `sqqq` |
| `reduced_exposure` | 0.5 | 0 … 1 (QQQ kept in the bearish regime with `qqq_reduced`; ≤ `max_long_exposure`) |
| `max_long_exposure` | 1.0 | common parameter: bullish exposure in × QQQ (above 1 would need TQQQ) |
| `max_short_exposure` | 0 | common parameter: must be > 0 with `bearish_action: sqqq`, and 0 otherwise |

**Exposure** is only a suggestion; the ensemble, allocation policy and risk engine
(exposure limits, volatility targeting, drawdown bands, leveraged/inverse ETF caps,
pre-trade gates) decide the portfolio. Defaults: bullish → 1.0 × QQQ, bearish → cash.
Nothing defaults to TQQQ or SQQQ; an SQQQ variant must be chosen explicitly and needs
TQQQ/SQQQ data (the backtest fails clearly if it is missing — no silent substitution).

**Timing and warm-up.** Values come from the point-in-time view (bars up to the
decision time only); the platform's execution timing then applies. No signal is
produced until both averages are fully warmed up **and** the previous bar's averages
exist: 201 bars for 50/200 (no partial-period averages, no back-filling).

**Governance.** It starts in `research` like every strategy and follows the same
lifecycle; selecting it, saving its parameters or promoting it never starts
automation, releases the kill switch or grants `live_approved`.

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

## Strategy Manager (dashboard)

The dashboard's Strategy Manager ([DASHBOARD.md](DASHBOARD.md)) shows each strategy's
id, family, implementation and version, parameters, research grid, warm-up,
enabled flag, lifecycle, eligible modes, approval and its latest research and
backtest evidence. Its changes are stored as audited **runtime overrides**
(`runtime_config` in PostgreSQL) on top of `config/strategies.yaml`, which stays
the reviewed source; every change produces a new `config_version`.

| Action | Rule |
|---|---|
| Parameters | A form generated from the implementation's parameter schema (types, bounds, choices); no code editing. Allowed only in `research` or `disabled`, because a new parameter set is a new version without evidence. Validated by the strategy registry before it is saved |
| Enable / disable | Controls whether the strategy may run in backtests and research. **Enabled is not eligible**: trading in shadow/paper additionally needs lifecycle `paper` or later |
| Lifecycle | research → validated → paper → shadow (and back / disabled), validated by `governance.lifecycle.transition` with a **human** actor, recorded in `strategy_lifecycle_events` and `control_events`. Promotions need the operator's name, a justification of ≥ 20 characters and the phrase `APPROVE PROMOTION` |
| `live_approved` | **Never from the UI.** It remains a reviewed change to `config/strategies.yaml` with an `approval:` block |

A running scheduler keeps the strategy set it started with; stop and start it to
apply lifecycle changes.

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
