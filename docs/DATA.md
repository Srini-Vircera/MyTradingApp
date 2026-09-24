# Market data

How the platform obtains, checks, stores and serves prices (Milestone 2).
Code lives in `src/adaptive_quant/quant/data/`.

## Conventions

| Topic | Rule | Why |
|---|---|---|
| Timestamps | Every bar is stamped at its **end** time, in UTC. A daily bar is stamped at its session close (21:00 UTC in winter, 20:00 in summer, 18:00 on a 13:00 ET early close). | A point-in-time filter `timestamp <= as_of` then can't show today's daily bar before today's close. Look-ahead bias is ruled out by construction. |
| Intraday | Providers stamp bars at their *start*; adapters convert to the end. Pre-/post-market bars are dropped. Frequencies: 1, 5, 15, 30 min. | One unambiguous grid per session. |
| Calendar | NYSE (XNYS) from the `exchange_calendars` project, covering 1990 to about one year ahead. It includes holidays, early closes and special closures. Queries outside the coverage raise an error instead of guessing. | Weekend, holiday and early-close logic lives in one place. |
| Columns | `open, high, low, close, volume` (float64); optional `vwap`, `trade_count`, `is_synthetic`. | One canonical format for every provider. |
| Adjustments | **raw** (as traded), **split** (split-adjusted price return), **all** (split + dividends = total return). Raw bars are stored; split and all are **derived locally** from corporate actions. | The adjustment method doesn't depend on which vendor supplied the data. Use raw for order sizing and fills, all for signals and research. |

### Adjustment math

This is the standard backward method (see `corporate_actions.py`).

- **Split with ratio r** (3-for-1 → r = 3; 1-for-4 reverse → r = 0.25): earlier prices are divided by r, and earlier volumes multiplied by r.
- **Cash distribution D**: earlier prices are multiplied by `1 − D / P`, where P is the raw close before the ex-date. This convention gives an ex-date return of `P_t/(P − D) − 1`.

Golden-number tests: `tests/regression/test_adjustment_golden.py`.

## Providers

| Provider | Key needed | Bars | Adjustments served | Corporate actions | Notes |
|---|---|---|---|---|---|
| `file` | none | CSV/Parquet in `data.file_import.directory` | whatever the files are (`data.file_import.adjustment`) | optional `<SYMBOL>_actions.csv` | Offline research. Intraday files need timezone-aware timestamps. Files with an `Adj Close` column are refused as ambiguous. |
| `alpaca` | `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | daily + intraday | raw / split / all | yes (v1 corporate actions) | The free `iex` feed reports IEX-only volume. No index data. |
| `polygon` | `POLYGON_API_KEY` | daily + intraday | raw / split | yes (splits + dividends) | Bearer-header auth. `NDX` maps to `I:NDX` (requires an indices plan). The base URL is configurable. |

The network adapters retry timeouts, HTTP 429 (honouring `Retry-After`) and 5xx responses with exponential backoff. HTTP 401/403 stops immediately with a credentials hint. Keys are sent only in headers, never in URLs, logs or errors. They are tested against recorded-format fixtures in `tests/fixtures/providers/`.

**File layout for `file`:**

```
var/data/import/QQQ.csv            date,open,high,low,close,volume
var/data/import/QQQ_actions.csv    ex_date,type,ratio,amount   (type: split | cash_dividend)
var/data/import/QQQ_5min.csv       timestamp,open,high,low,close,volume  (bar START, with offset)
```

If raw files span splits or dividends, provide the `_actions.csv` file; use a header-only file when there are none. Without it, split and total-return series aren't derived, and the pipeline records why.

## Validation

`validation.py` reports every defect it finds, grouped by kind. **Errors** make the data unusable; **warnings** are recorded but don't block.

| Kind | Severity | Detects |
|---|---|---|
| `empty` | error | no rows |
| `missing_value` | error | NaN/inf in OHLCV |
| `non_positive_price` | error | zero or negative prices |
| `impossible_price` | error | high < low; open/close outside [low, high] |
| `negative_volume` | error | volume < 0 |
| `bad_timestamp` | error | daily bar not at the session close; intraday bar off-grid or outside the session |
| `future_timestamp` | error | bar ends after the evaluation time (look-ahead) |
| `out_of_order` | error | timestamps decrease as delivered |
| `duplicate` | error | repeated timestamps |
| `non_session` | error | daily bar on a weekend or holiday |
| `missing_bar` | error | a session with no daily bar |
| `missing_intraday_bar` | warning | an absent intraday bar |
| `unexplained_jump` | warning | move above `data.max_abs_daily_return` with no corporate action on that date |
| `vwap_outside_range` | warning | vwap outside [low, high] |
| `tracking_error_exceeded` | error | synthetic series is too far from the real fund |

**Staleness** (`freshness.py`) depends on *when* the data is used, so it is checked separately:

- **Daily data** is stale when more than `data.staleness.max_daily_bar_age_sessions` sessions have closed after the newest bar. Weekends, holidays and early closes are accounted for.
- **Intraday data** is stale when the newest bar lags by more than `max_intraday_bar_age_seconds`.

The pre-trade gate runs `trading/safety/data_checks.py::MarketDataCheck`. It refuses with `market_data_missing`, `market_data_invalid` or `market_data_stale`, and each of these blocks **all** orders.

## Storage

```
var/data/bars/<source>/<frequency>/<adjustment>/<SYMBOL>/<hash16>.parquet
var/data/bars/<source>/<frequency>/<adjustment>/<SYMBOL>/manifest.json
var/data/corporate_actions/<source>/<SYMBOL>.json
```

- **Content-addressed and idempotent.** A snapshot file is named by the SHA-256 of its data. Writing identical data again is a no-op, so re-running a download is always safe.
- **Versioned.** The manifest keeps every snapshot with its provenance: provider, time, rows, range, validation outcome, issues, notes, synthetic flag. Older versions stay readable for audits.
- **Integrity-checked.** Every read recomputes the hash; a modified or corrupted file is refused.
- **Fail closed.** Invalid snapshots are kept for inspection but are never served by default.
- **Vendor revisions.** When re-downloaded bars differ from stored ones, the count is reported.
- **Metadata mirror.** PostgreSQL will mirror this metadata in Milestone 8.

## Synthetic leveraged-ETF history

TQQQ and SQQQ started trading on 2010-02-11. Research that needs earlier periods (2000–2002, 2008) uses **synthetic** history. It is never simply "annual return × 3": each day compounds a daily-reset return, so volatility drag appears naturally.

```
dt        = calendar days since the previous session / 365
r_index   = r_QQQ(price return, split-adjusted) + QQQ_expense_ratio · dt
r_LETF    = L · r_index − expense_ratio · dt − (L − 1) · rf(t−1) · dt − swap_notional · swap_spread · dt
swap_notional = L − 1 (L > 0)  |  |L| (L < 0)
```

- **Financing:** TQQQ (L = 3) pays financing on 2× borrowed notional. SQQQ (L = −3) earns a credit on 4× (collateral plus short swap), less the swap spread.
- **Price bars:** open, high and low apply the leveraged intraday move of the underlying. Volume is 0.
- **Wipeouts:** a modelled daily loss beyond −100% is floored at −100% and flagged.
- **Assumptions** are in `config/base.yaml → data.synthetic` and are recorded on every synthetic snapshot:
  - expense ratios: TQQQ 0.84% and SQQQ 0.95%. Verify these against the current prospectuses.
  - swap spread: 0%, to be calibrated.
  - risk-free rate: a constant 2% fallback. **Supply a daily series**, e.g. FRED DTB3 CSV via `data.synthetic.risk_free.csv_path`. Historic rates ranged from 0% to 6%, so a constant misstates financing.
- **Tracking check:** `aq data synthesize` compares synthetic and real total returns over their overlap (2010 onward). The annualised tracking error must stay within `data.synthetic.max_tracking_error_annual` (default 3%/yr). If it doesn't, the synthetic snapshot is stored as **failed** and is never used to extend history until the assumptions are fixed.
- **Labelling:** synthetic data is always labelled:
  - `is_synthetic` is set on every row;
  - the manifest and the Parquet file metadata record it;
  - `aq data list` shows `SYNTHETIC`;
  - `history.extended_history()` returns real history extended backwards with per-row flags, so reports can separate synthetic and real periods.

> **Calibration status:** the model and the tracking check are implemented and tested on generated data. They have **not yet been calibrated on real TQQQ/SQQQ history**, because the build environment has no market-data access. Do this first with your provider keys: run `aq data download` and then `aq data synthesize`. Record the tracking-error result before trusting any pre-2010 backtest.

## Operator commands

```bash
aq data download                         # all instruments, primary provider, full history
aq data download --provider alpaca --symbols QQQ TQQQ SQQQ SPY --start 2010-01-01
aq data download --frequency 5min --symbols QQQ --start 2024-06-01
aq data list                             # what is stored, validity, SYNTHETIC flags
aq data validate                         # re-validate + freshness report
aq data validate --require-fresh         # fail on stale data (what trading enforces)
aq data synthesize                       # synthetic TQQQ/SQQQ + tracking-error check
```

- **Default end date:** the last *completed* session. A partial "today" bar is never downloaded.
- **Exit codes:** `0` on success; `2` if anything failed validation.
