"""Parameter neighbourhoods and the robustness score.

A strategy's ``param_grid`` (``strategies.yaml``) defines a neighbourhood of
candidate values per parameter. The full grid is the Cartesian product; every
grid point is one *trial*. Neighbours of a point are the points that differ by
at most one grid step in every dimension (Moore neighbourhood).

Robustness score (``docs/RESEARCH.md``), evaluated at the *best* grid point x*:

* plateau    = median(neighbour metric) / metric(x*)      (clipped to [0, 1])
* dispersion = std(metric over x* and its neighbours) / |metric(x*)|
* negative   = share of neighbours whose metric is <= 0
* score      = plateau x (1 - negative) / (1 + dispersion)  in [0, 1]

A score below the threshold (or a non-positive best metric) is flagged
``potentially_overfit``: the best result is an isolated peak, not a plateau.
Selection prefers the **plateau centre** - the point whose neighbourhood median
(then mean) is highest - never the raw argmax.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from adaptive_quant.core.errors import ConfigurationError
from adaptive_quant.quant.strategies.params import ParamValue

Coord = tuple[int, ...]


@dataclass(frozen=True)
class ParamGrid:
    """The parameter neighbourhood of one strategy."""

    base: Mapping[str, ParamValue]  # configured params (grid dims override them)
    dims: tuple[str, ...]
    values: tuple[tuple[ParamValue, ...], ...]  # sorted values per dimension

    @classmethod
    def build(
        cls,
        base: Mapping[str, ParamValue],
        grid: Mapping[str, Sequence[ParamValue]],
        max_points: int,
    ) -> ParamGrid:
        dims = tuple(sorted(grid))
        values = tuple(tuple(sorted(dict.fromkeys(grid[d]), key=_sort_key)) for d in dims)
        size = math.prod(len(v) for v in values)
        if size > max_points:
            raise ConfigurationError(
                f"param_grid has {size} points (limit research.max_grid_points={max_points})",
                hint="narrow the neighbourhood; every point is a counted trial",
            )
        return cls(dict(base), dims, values)

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(len(v) for v in self.values)

    def coords(self) -> list[Coord]:
        if not self.dims:
            return [()]
        return list(itertools.product(*(range(n) for n in self.shape)))

    def params(self, coord: Coord) -> dict[str, ParamValue]:
        out = dict(self.base)
        for d, vals, i in zip(self.dims, self.values, coord, strict=True):
            out[d] = vals[i]
        return out

    def neighbours(self, coord: Coord) -> list[Coord]:
        out = []
        for delta in itertools.product((-1, 0, 1), repeat=len(coord)):
            if not any(delta):
                continue
            c = tuple(i + d for i, d in zip(coord, delta, strict=True))
            if all(0 <= i < n for i, n in zip(c, self.shape, strict=True)):
                out.append(c)
        return out

    def label(self, coord: Coord) -> str:
        if not self.dims:
            return "configured"
        return ", ".join(
            f"{d}={v[i]}" for d, v, i in zip(self.dims, self.values, coord, strict=True)
        )


def _sort_key(v: ParamValue) -> tuple[int, float, str]:
    if isinstance(v, bool):
        return (0, float(v), "")
    if isinstance(v, int | float):
        return (1, float(v), "")
    return (2, 0.0, str(v))


@dataclass(frozen=True)
class RobustnessResult:
    best: Coord
    best_metric: float
    plateau_ratio: float
    dispersion: float
    negative_share: float
    score: float
    potentially_overfit: bool
    plateau_centre: Coord
    plateau_centre_metric: float  # neighbourhood median at the centre
    n_points: int
    n_valid: int


def neighbourhood_median(grid: ParamGrid, metric: Mapping[Coord, float], c: Coord) -> float:
    vals = [metric[n] for n in [c, *grid.neighbours(c)] if _ok(metric.get(n))]
    return float(np.median(vals)) if vals else math.nan


def neighbourhood_mean(grid: ParamGrid, metric: Mapping[Coord, float], c: Coord) -> float:
    vals = [metric[n] for n in [c, *grid.neighbours(c)] if _ok(metric.get(n))]
    return float(np.mean(vals)) if vals else math.nan


def robustness(
    grid: ParamGrid, metric: Mapping[Coord, float], threshold: float
) -> RobustnessResult:
    """Robustness of ``metric`` (e.g. annualised Sharpe) over the grid. Missing or
    non-finite points (invalid parameter combinations) are ignored."""
    valid = {c: v for c, v in metric.items() if _ok(v)}
    if not valid:
        raise ValueError("no valid grid points")
    best = max(valid, key=lambda c: (valid[c], [-i for i in c]))
    m_best = valid[best]
    nb = [valid[n] for n in grid.neighbours(best) if n in valid]
    if m_best <= 0:
        plateau, dispersion, negative, score = 0.0, math.inf, 1.0, 0.0
    elif not nb:
        # a grid of one point has no neighbourhood: robustness is unknown, not proven
        plateau, dispersion, negative, score = math.nan, math.nan, math.nan, 0.0
    else:
        plateau = min(max(float(np.median(nb)) / m_best, 0.0), 1.0)
        dispersion = float(np.std([m_best, *nb])) / abs(m_best)
        negative = sum(v <= 0 for v in nb) / len(nb)
        score = plateau * (1.0 - negative) / (1.0 + dispersion)
    smoothed = {c: neighbourhood_median(grid, valid, c) for c in valid}
    means = {c: neighbourhood_mean(grid, valid, c) for c in valid}
    # ties on the median go to the point whose whole neighbourhood is strongest
    centre = max(smoothed, key=lambda c: (smoothed[c], means[c], valid[c], [-i for i in c]))
    return RobustnessResult(
        best=best,
        best_metric=m_best,
        plateau_ratio=plateau,
        dispersion=dispersion,
        negative_share=negative,
        score=score,
        potentially_overfit=score < threshold,
        plateau_centre=centre,
        plateau_centre_metric=smoothed[centre],
        n_points=len(metric),
        n_valid=len(valid),
    )


def _ok(v: float | None) -> bool:
    return v is not None and math.isfinite(v)
