"""Walk-forward validation with strict in-sample / out-of-sample separation.

Windows are measured in sessions (``years x 252``). For fold ``k``::

    rolling:   [ train ][ validate ][ test ]            (all windows slide)
    anchored:  [ train ------------][ validate ][ test ] (train starts at the beginning)

Selection for a fold sees **only** rows before the end of its validation
window (the slice is cut before the selector is called):

1. rank grid points by their *neighbourhood-median* train Sharpe (plateau-aware);
2. keep the ``top_k``;
3. choose the one with the best validation Sharpe.

The test window is never touched during selection. Test-window returns of the
chosen parameters are stitched into one out-of-sample (OOS) return series.
Each trial is one continuous causal backtest, so its returns inside any window
depend only on data up to that day; switching parameter sets at a fold boundary
is approximated (the transition trade is not simulated) - see RESEARCH.md.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.quant.research.robustness import (
    Coord,
    ParamGrid,
    neighbourhood_mean,
    neighbourhood_median,
)

P = 252
MIN_PARTIAL_TEST = 21


@dataclass(frozen=True)
class WalkForwardSettings:
    scheme: str = "rolling"  # rolling | anchored
    train_years: float = 8.0
    validate_years: float = 2.0
    test_years: float = 1.0
    step_years: float = 1.0
    top_k: int = 3

    def __post_init__(self) -> None:
        if self.scheme not in ("rolling", "anchored"):
            raise ValueError("scheme must be 'rolling' or 'anchored'")
        if min(self.train_years, self.validate_years, self.test_years) <= 0:
            raise ValueError("walk-forward windows must be positive")
        if self.step_years < self.test_years:
            raise ValueError("step_years must be >= test_years (OOS windows may not overlap)")
        if self.top_k < 1:
            raise ValueError("top_k must be >= 1")


@dataclass(frozen=True)
class Window:
    fold: int
    train: tuple[int, int]  # [start, end) row positions
    validate: tuple[int, int]
    test: tuple[int, int]


def windows(n_rows: int, s: WalkForwardSettings) -> list[Window]:
    tr, va, te, st = (
        max(1, round(y * P)) for y in (s.train_years, s.validate_years, s.test_years, s.step_years)
    )
    out = []
    test_start = tr + va
    k = 0
    while test_start < n_rows:
        test_end = min(test_start + te, n_rows)
        if test_end - test_start < min(te, MIN_PARTIAL_TEST):
            break
        train_start = 0 if s.scheme == "anchored" else test_start - va - tr
        out.append(
            Window(
                k,
                (train_start, test_start - va),
                (test_start - va, test_start),
                (test_start, test_end),
            )
        )
        test_start += st
        k += 1
    return out


@dataclass(frozen=True)
class Fold:
    window: Window
    # first and last session (inclusive) of train, validate and test
    dates: tuple[str, str, str, str, str, str]
    chosen: Coord
    label: str
    train_sharpe: float
    validate_sharpe: float
    test_sharpe: float
    test_return: float
    test_max_drawdown: float
    candidates: tuple[Coord, ...]


@dataclass(frozen=True)
class WalkForwardResult:
    settings: WalkForwardSettings
    folds: tuple[Fold, ...]
    oos_returns: pd.Series  # stitched test-window returns
    mean_train_sharpe: float
    mean_validate_sharpe: float
    oos_sharpe: float
    decay_ratio: float  # OOS Sharpe / mean train Sharpe (nan when train Sharpe <= 0)
    parameter_changes: int


def annual_sharpe(r: np.ndarray) -> float:
    if len(r) < 2:
        return math.nan
    sd = float(np.std(r, ddof=1))
    return float(np.mean(r)) / sd * math.sqrt(P) if sd > 1e-15 else math.nan


def select(
    grid: ParamGrid,
    coords: Sequence[Coord],
    history: np.ndarray,
    train: tuple[int, int],
    validate: tuple[int, int],
    top_k: int,
) -> tuple[Coord, tuple[Coord, ...]]:
    """Choose parameters using ``history`` rows only (the caller cuts it at validate end)."""
    if history.shape[0] < validate[1]:
        raise ValueError("selection history does not cover the validation window")
    train_s = {c: annual_sharpe(history[train[0] : train[1], j]) for j, c in enumerate(coords)}
    valid = {c: v for c, v in train_s.items() if math.isfinite(v)}
    if not valid:
        # nothing has a defined Sharpe (e.g. never invested): fall back to grid order
        valid = dict.fromkeys(coords, 0.0)
    smooth = {c: neighbourhood_median(grid, valid, c) for c in valid}
    means = {c: neighbourhood_mean(grid, valid, c) for c in valid}
    ranked = sorted(valid, key=lambda c: (-_finite(smooth[c]), -_finite(means[c]), -valid[c], c))[
        :top_k
    ]
    col = {c: j for j, c in enumerate(coords)}
    val_s = {c: annual_sharpe(history[validate[0] : validate[1], col[c]]) for c in ranked}
    chosen = min(ranked, key=lambda c: (-_finite(val_s[c]), ranked.index(c)))
    return chosen, tuple(ranked)


def walk_forward(
    grid: ParamGrid, coords: Sequence[Coord], returns: pd.DataFrame, s: WalkForwardSettings
) -> WalkForwardResult:
    """``returns``: rows = sessions, one column per coord (same order as ``coords``)."""
    if returns.shape[1] != len(coords):
        raise ValueError("one returns column per coord is required")
    m = returns.to_numpy(dtype=float)
    idx = pd.DatetimeIndex(returns.index)
    folds: list[Fold] = []
    pieces: list[pd.Series] = []
    for w in windows(len(m), s):
        chosen, cands = select(
            grid, coords, m[: w.validate[1]].copy(), w.train, w.validate, s.top_k
        )
        j = list(coords).index(chosen)
        test = m[w.test[0] : w.test[1], j]
        eq = np.cumprod(1 + test)
        dd = float((eq / np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:] - 1).min())
        folds.append(
            Fold(
                window=w,
                dates=(
                    _d(idx[w.train[0]]),
                    _d(idx[w.train[1] - 1]),
                    _d(idx[w.validate[0]]),
                    _d(idx[w.validate[1] - 1]),
                    _d(idx[w.test[0]]),
                    _d(idx[w.test[1] - 1]),
                ),
                chosen=chosen,
                label=grid.label(chosen),
                train_sharpe=annual_sharpe(m[w.train[0] : w.train[1], j]),
                validate_sharpe=annual_sharpe(m[w.validate[0] : w.validate[1], j]),
                test_sharpe=annual_sharpe(test),
                test_return=float(eq[-1] - 1),
                test_max_drawdown=min(dd, 0.0),
                candidates=cands,
            )
        )
        pieces.append(pd.Series(test, index=idx[w.test[0] : w.test[1]]))
    oos = pd.concat(pieces) if pieces else pd.Series(dtype=float)
    mean_train = _nanmean([f.train_sharpe for f in folds])
    oos_sharpe = annual_sharpe(oos.to_numpy())
    changes = sum(1 for a, b in itertools.pairwise(folds) if a.chosen != b.chosen)
    return WalkForwardResult(
        settings=s,
        folds=tuple(folds),
        oos_returns=oos,
        mean_train_sharpe=mean_train,
        mean_validate_sharpe=_nanmean([f.validate_sharpe for f in folds]),
        oos_sharpe=oos_sharpe,
        decay_ratio=oos_sharpe / mean_train
        if mean_train > 0 and math.isfinite(oos_sharpe)
        else math.nan,
        parameter_changes=changes,
    )


def _finite(v: float) -> float:
    return v if math.isfinite(v) else -math.inf


def _nanmean(v: list[float]) -> float:
    x = [a for a in v if math.isfinite(a)]
    return float(np.mean(x)) if x else math.nan


def _d(ts: pd.Timestamp) -> str:
    return f"{pd.Timestamp(ts).tz_convert(MARKET_TZ):%Y-%m-%d}"
