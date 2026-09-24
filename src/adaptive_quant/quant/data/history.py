"""Research-facing history services built on the store.

* :func:`build_synthetic` creates and stores the synthetic series for one
  leveraged product (source ``synthetic``) and measures tracking error against
  the real fund over their overlap. A tracking error above the configured
  bound stores the snapshot as *failed validation*, so it is never served for
  research until the assumptions are fixed (fail closed).
* :func:`extended_history` returns real history extended backwards with
  synthetic data, with a per-row ``is_synthetic`` flag, for backtests that
  need pre-inception periods.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from adaptive_quant.config.schema import Settings
from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.store import ParquetBarStore, SnapshotInfo
from adaptive_quant.quant.data.synthetic import (
    FinancingAssumptions,
    LeveragedProductSpec,
    TrackingReport,
    align_risk_free,
    constant_risk_free,
    load_risk_free_csv,
    splice_with_real,
    synthesize_leveraged_bars,
    tracking_report,
)
from adaptive_quant.quant.data.validation import (
    DataIssue,
    IssueKind,
    IssueSeverity,
    ValidationReport,
)

SYNTHETIC_SOURCE = "synthetic"
UNDERLYING_ADJUSTMENT = Adjustment.SPLIT  # price return: leveraged funds track index price
PRODUCT_ADJUSTMENT = Adjustment.ALL  # compare with the fund's total return


@dataclass(frozen=True)
class SyntheticBuild:
    snapshot: SnapshotInfo
    tracking: TrackingReport | None
    wipeout_days: int
    assumptions: dict[str, str]

    @property
    def ok(self) -> bool:
        return self.snapshot.validation_passed


def product_spec(settings: Settings, symbol: str) -> LeveragedProductSpec:
    product = settings.data.synthetic.products.get(symbol)
    if product is None:
        raise DataQualityError(
            f"{symbol} has no synthetic product configuration",
            hint="add it under data.synthetic.products in config/base.yaml",
        )
    inst = settings.universe.by_symbol[symbol]
    return LeveragedProductSpec(symbol, inst.leverage, product.expense_ratio, product.inception)


def build_synthetic(
    store: ParquetBarStore,
    settings: Settings,
    symbol: str,
    *,
    source: str,
    resolve_path: Callable[[Path], Path] | None = None,
) -> SyntheticBuild:
    """Synthesize ``symbol`` from the stored underlying of ``source`` and store it."""
    syn = settings.data.synthetic
    spec = product_spec(settings, symbol)
    underlying = store.read(
        SeriesKey(source, syn.underlying_symbol, Frequency.DAILY, UNDERLYING_ADJUSTMENT)
    )
    rf, rf_note = _risk_free(settings, pd.DatetimeIndex(underlying.index), resolve_path)
    financing = FinancingAssumptions(
        swap_spread=syn.swap_spread_annual,
        underlying_expense_addback=syn.underlying_expense_addback,
    )
    bars, wipeouts = synthesize_leveraged_bars(underlying, spec, rf, financing)
    assumptions = {
        "underlying": f"{source}:{syn.underlying_symbol}:{UNDERLYING_ADJUSTMENT}",
        "leverage": str(spec.leverage),
        "expense_ratio": str(spec.expense_ratio),
        "underlying_expense_addback": str(syn.underlying_expense_addback),
        "swap_spread_annual": str(syn.swap_spread_annual),
        "risk_free": rf_note,
        "real_inception": spec.inception.isoformat(),
        "method": "daily-reset compounding; see docs/DATA.md",
    }

    issues: list[DataIssue] = []
    tracking = None
    real_key = SeriesKey(source, symbol, Frequency.DAILY, PRODUCT_ADJUSTMENT)
    if store.latest(real_key) is not None:
        real = store.read(real_key)
        tracking = tracking_report(
            symbol, real["close"], bars["close"], syn.max_tracking_error_annual
        )
        assumptions["tracking"] = tracking.summary()
        if not tracking.passed:
            issues.append(
                DataIssue(
                    IssueKind.TRACKING_ERROR_EXCEEDED,
                    IssueSeverity.ERROR,
                    1,
                    "synthetic model exceeds tracking-error bound: " + tracking.summary(),
                )
            )
    else:
        assumptions["tracking"] = "not measured: no real series stored for comparison"
    if wipeouts:
        assumptions["wipeouts"] = f"{wipeouts} day(s) with modelled loss beyond -100% (floored)"

    report = ValidationReport(
        f"{SYNTHETIC_SOURCE}:{symbol}",
        len(bars),
        bars.index[0].to_pydatetime(),
        bars.index[-1].to_pydatetime(),
        tuple(issues),
    )
    snapshot = store.write(
        SeriesKey(SYNTHETIC_SOURCE, symbol, Frequency.DAILY, PRODUCT_ADJUSTMENT),
        bars,
        provider=f"synthetic<-{source}",
        validation=report,
        is_synthetic=True,
        notes=assumptions,
    )
    return SyntheticBuild(snapshot, tracking, wipeouts, assumptions)


def extended_history(store: ParquetBarStore, symbol: str, *, source: str) -> pd.DataFrame:
    """Real total-return history of ``symbol`` extended back with validated synthetic data.

    Every row carries ``is_synthetic``. If no synthetic series exists, the real
    series is returned with ``is_synthetic=False`` throughout.
    """
    real = store.read(SeriesKey(source, symbol, Frequency.DAILY, PRODUCT_ADJUSTMENT))
    syn_key = SeriesKey(SYNTHETIC_SOURCE, symbol, Frequency.DAILY, PRODUCT_ADJUSTMENT)
    if store.latest(syn_key) is None:
        out = real.copy()
        out["is_synthetic"] = False
        return out
    return splice_with_real(real, store.read(syn_key))


def _risk_free(
    settings: Settings,
    index: pd.DatetimeIndex,
    resolve_path: Callable[[Path], Path] | None,
) -> tuple[pd.Series[float], str]:
    cfg = settings.data.synthetic.risk_free
    if cfg.csv_path is not None:
        path = resolve_path(cfg.csv_path) if resolve_path else cfg.csv_path
        return align_risk_free(load_risk_free_csv(path), index), f"csv:{cfg.csv_path}"
    rate = cfg.constant_annual_rate
    return constant_risk_free(index, rate), f"constant {rate:.2%} (ASSUMPTION - supply a series)"
