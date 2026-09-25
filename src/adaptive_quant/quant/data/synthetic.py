"""Synthetic leveraged-ETF history (e.g. TQQQ/SQQQ before their 2010 inception).

This is **not** "annual Nasdaq return x 3". Each synthetic day compounds a
daily-reset leveraged return, so volatility drag emerges naturally::

    r_idx(t)  = r_underlying(t) + underlying_expense_addback * dt
    r_LETF(t) = L * r_idx(t)
                - expense_ratio * dt
                - (L - 1) * rf(t-1) * dt           # TQQQ pays on 2x borrowed, SQQQ earns on 4x
                - swap_notional(L) * swap_spread * dt
    swap_notional(L) = L - 1  if L > 0 else |L|
    dt = calendar days since the previous session / 365

``r_underlying`` should be a *price* return (split-adjusted, no dividends)
because leveraged funds track the index price return. For QQQ, adding back
its expense ratio approximates the index. ``rf(t-1)`` is the annual
risk-free rate known on the previous day. A daily loss beyond -100% is floored
at -100% (the fund would be wiped out) and flagged.

Synthetic data is always labelled: synthetic-only frames carry
``is_synthetic=True`` on every row; spliced frames flag each row, and the store
records ``is_synthetic`` in the manifest and in the Parquet metadata.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.data.bars import require_canonical_sorted, to_canonical

TRADING_DAYS_PER_YEAR = 252
SYNTHETIC_BASE_PRICE = 100.0


@dataclass(frozen=True)
class LeveragedProductSpec:
    symbol: str
    leverage: float
    expense_ratio: float  # annual, e.g. 0.0095
    inception: date  # first real trading day


@dataclass(frozen=True)
class FinancingAssumptions:
    swap_spread: float = 0.0  # annual spread over rf paid on swap notional
    underlying_expense_addback: float = 0.0  # annual; converts ETF price return to index return


def swap_notional(leverage: float) -> float:
    return leverage - 1.0 if leverage > 0 else abs(leverage)


def _dt_years(index: pd.DatetimeIndex) -> np.ndarray:
    days = np.array([ts.astimezone(MARKET_TZ).date().toordinal() for ts in index], dtype=float)
    return np.asarray(np.diff(days, prepend=np.nan) / 365.0, dtype=float)


def synthesize_leveraged_returns(
    underlying_close: pd.Series[float],
    spec: LeveragedProductSpec,
    risk_free_annual: pd.Series[float],
    financing: FinancingAssumptions,
) -> tuple[pd.Series[float], int]:
    """Daily synthetic returns aligned to ``underlying_close`` (first value NaN).

    Returns ``(returns, wipeout_days)``.
    """
    if not underlying_close.index.equals(risk_free_annual.index):
        raise DataQualityError("risk-free series must be aligned to the underlying series")
    if risk_free_annual.isna().any():
        raise DataQualityError("risk-free series contains gaps; fill or extend it first")
    lev = spec.leverage
    dt = _dt_years(pd.DatetimeIndex(underlying_close.index))
    r_under = underlying_close.pct_change().to_numpy()
    r_idx = r_under + financing.underlying_expense_addback * dt
    rf_prev = risk_free_annual.shift(1).to_numpy()
    r = (
        lev * r_idx
        - spec.expense_ratio * dt
        - (lev - 1.0) * rf_prev * dt
        - swap_notional(lev) * financing.swap_spread * dt
    )
    wipeouts = int(np.sum(r[1:] < -1.0))
    r = np.where(r < -1.0, -1.0, r)
    return pd.Series(r, index=underlying_close.index, dtype="float64"), wipeouts


def synthesize_leveraged_bars(
    underlying: pd.DataFrame,
    spec: LeveragedProductSpec,
    risk_free_annual: pd.Series[float],
    financing: FinancingAssumptions,
) -> tuple[pd.DataFrame, int]:
    """Synthetic daily OHLC bars for ``spec`` from underlying daily bars.

    Closes compound the synthetic daily returns from a base of 100. Open/high/low
    apply the leveraged move of the underlying's open/high/low relative to its
    previous close (intraday financing ignored); for inverse products high and
    low swap. Volume is 0 - synthetic bars have no traded volume.
    """
    require_canonical_sorted(underlying, "underlying bars")
    if len(underlying) < 2:
        raise DataQualityError("need at least two underlying bars to synthesize returns")
    rets, wipeouts = synthesize_leveraged_returns(
        underlying["close"], spec, risk_free_annual, financing
    )
    growth = (1.0 + rets.iloc[1:]).cumprod()
    close = SYNTHETIC_BASE_PRICE * growth.to_numpy()
    prev_close = np.concatenate([[SYNTHETIC_BASE_PRICE], close[:-1]])
    u_prev = underlying["close"].to_numpy()[:-1]
    lev = spec.leverage

    def move(col: str) -> np.ndarray:
        x = underlying[col].to_numpy()[1:] / u_prev - 1.0
        return np.asarray(np.maximum(prev_close * (1.0 + lev * x), 0.0), dtype=float)

    o = move("open")
    a, b = move("high"), move("low")
    hi_raw, lo_raw = (a, b) if lev > 0 else (b, a)
    high = np.maximum.reduce([hi_raw, o, close])
    low = np.minimum.reduce([lo_raw, o, close])
    frame = pd.DataFrame(
        {
            "open": o,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.zeros(len(close)),
            "is_synthetic": np.ones(len(close), dtype=bool),
        },
        index=underlying.index[1:],
    )
    return to_canonical(frame), wipeouts


def splice_with_real(real: pd.DataFrame, synthetic: pd.DataFrame) -> pd.DataFrame:
    """Extend ``real`` backwards with ``synthetic`` history.

    The synthetic segment is rescaled so that its close on the first real day
    equals the real close, keeping the price path continuous; only synthetic
    rows *before* the first real bar are used. Every row gets ``is_synthetic``.
    """
    require_canonical_sorted(real, "real bars")
    require_canonical_sorted(synthetic, "synthetic bars")
    if real.empty:
        raise DataQualityError("cannot splice onto an empty real series")
    first_real = real.index[0]
    if first_real not in synthetic.index:
        raise DataQualityError(
            f"synthetic series does not cover the first real bar ({first_real:%Y-%m-%d})"
        )
    scale = float(real["close"].iloc[0] / synthetic.loc[first_real, "close"])
    early = synthetic[synthetic.index < first_real].copy()
    for col in ("open", "high", "low", "close"):
        early[col] = early[col] * scale
    early["is_synthetic"] = True
    real_part = real.drop(columns=[c for c in real.columns if c == "is_synthetic"]).copy()
    real_part["is_synthetic"] = False
    common = [c for c in real_part.columns if c in early.columns]
    return to_canonical(pd.concat([early[common], real_part[common]]))


@dataclass(frozen=True)
class TrackingReport:
    """How well the synthetic model reproduced the real fund over their overlap."""

    symbol: str
    start: date
    end: date
    days: int
    tracking_error_annual: float  # stdev of daily return differences x sqrt(252)
    mean_daily_difference: float  # synthetic - real
    correlation: float
    real_total_return: float
    synthetic_total_return: float
    bound: float

    @property
    def passed(self) -> bool:
        return (
            math.isfinite(self.tracking_error_annual) and self.tracking_error_annual <= self.bound
        )

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"{self.symbol} synthetic vs real {self.start}..{self.end} ({self.days} days): "
            f"tracking error {self.tracking_error_annual:.2%}/yr "
            f"(bound {self.bound:.2%}) {verdict}; "
            f"correlation {self.correlation:.4f}; total return real {self.real_total_return:+.1%} "
            f"vs synthetic {self.synthetic_total_return:+.1%}"
        )


def tracking_report(
    symbol: str,
    real_close: pd.Series[float],
    synthetic_close: pd.Series[float],
    bound: float,
) -> TrackingReport:
    """Compare daily returns over the overlapping period (inner join on timestamp)."""
    joined = pd.concat(
        [real_close.rename("real"), synthetic_close.rename("syn")], axis=1, join="inner"
    )
    rets = joined.pct_change().dropna()
    if len(rets) < 20:
        raise DataQualityError(
            f"{symbol}: only {len(rets)} overlapping days; need at least 20 to measure tracking"
        )
    diff = rets["syn"] - rets["real"]
    idx = pd.DatetimeIndex(joined.index)
    return TrackingReport(
        symbol=symbol,
        start=idx[0].astimezone(MARKET_TZ).date(),
        end=idx[-1].astimezone(MARKET_TZ).date(),
        days=len(rets),
        tracking_error_annual=float(diff.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)),
        mean_daily_difference=float(diff.mean()),
        correlation=float(rets["syn"].corr(rets["real"])),
        real_total_return=float(joined["real"].iloc[-1] / joined["real"].iloc[0] - 1.0),
        synthetic_total_return=float(joined["syn"].iloc[-1] / joined["syn"].iloc[0] - 1.0),
        bound=bound,
    )


# ------------------------------------------------------------------ risk-free rate
def constant_risk_free(index: pd.DatetimeIndex, annual_rate: float) -> pd.Series[float]:
    return pd.Series(annual_rate, index=index, dtype="float64")


def load_risk_free_csv(path: Path) -> pd.Series[float]:
    """Load an annual risk-free rate series in percent, e.g. FRED ``DTB3``.

    Accepts FRED's CSV layout (``DATE``/``observation_date`` + one value column,
    ``.`` for missing). Returns decimal annual rates indexed by date.
    """
    if not path.exists():
        raise DataQualityError(f"risk-free rate file not found: {path}")
    df = pd.read_csv(path, na_values=["."])
    date_col = next((c for c in df.columns if c.lower() in {"date", "observation_date"}), None)
    value_cols = [c for c in df.columns if c != date_col]
    if date_col is None or len(value_cols) != 1:
        raise DataQualityError(
            f"{path.name}: expected a date column (DATE/observation_date) and one value column"
        )
    series = pd.Series(
        pd.to_numeric(df[value_cols[0]], errors="coerce").to_numpy() / 100.0,
        index=pd.to_datetime(df[date_col]).dt.date,
        dtype="float64",
    )
    series = series[~series.index.duplicated(keep="last")].sort_index().ffill()
    if series.isna().all():
        raise DataQualityError(f"{path.name}: no numeric rate values")
    return series


def align_risk_free(dated_rates: pd.Series[float], index: pd.DatetimeIndex) -> pd.Series[float]:
    """Align a date-indexed rate series to bar timestamps (as-of: last known value)."""
    dates = [ts.astimezone(MARKET_TZ).date() for ts in index]
    rates = dated_rates.dropna()
    keys = list(rates.index)
    if not keys or dates[0] < keys[0]:
        raise DataQualityError(
            f"risk-free series starts {keys[0] if keys else 'never'}, after the bars ({dates[0]})"
        )
    key_ord = np.array([d.toordinal() for d in keys])
    bar_ord = np.array([d.toordinal() for d in dates])
    positions = np.searchsorted(key_ord, bar_ord, side="right") - 1
    return pd.Series(rates.to_numpy()[positions], index=index, dtype="float64")
