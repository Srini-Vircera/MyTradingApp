"""Parameter grids and the robustness score (sharp peak vs plateau fixtures)."""

import math

import numpy as np
import pytest

from adaptive_quant.core.errors import ConfigurationError
from adaptive_quant.quant.research.robustness import (
    Coord,
    ParamGrid,
    neighbourhood_median,
    robustness,
)


def grid5() -> ParamGrid:
    return ParamGrid.build(
        {"a": 1, "b": 2, "keep": "x"}, {"a": [1, 2, 3, 4, 5], "b": [5, 4, 3, 2, 1]}, 64
    )


def surface(values: np.ndarray) -> dict[Coord, float]:
    return {
        (i, j): float(values[i, j]) for i in range(values.shape[0]) for j in range(values.shape[1])
    }


def test_grid_expansion_and_neighbours() -> None:
    g = grid5()
    assert g.dims == ("a", "b") and g.values[1] == (1, 2, 3, 4, 5)  # sorted, deduplicated
    assert len(g.coords()) == 25
    assert g.params((0, 4)) == {"a": 1, "b": 5, "keep": "x"}
    assert g.label((2, 0)) == "a=3, b=1"
    assert len(g.neighbours((0, 0))) == 3
    assert len(g.neighbours((2, 2))) == 8
    assert (2, 2) not in g.neighbours((2, 2))
    single = ParamGrid.build({"w": 3}, {}, 1)
    assert single.coords() == [()] and single.params(()) == {"w": 3}
    assert single.label(()) == "configured" and single.neighbours(()) == []


def test_grid_size_limit_is_refused_not_subsampled() -> None:
    with pytest.raises(ConfigurationError, match="max_grid_points"):
        ParamGrid.build({}, {"a": list(range(10)), "b": list(range(10))}, 64)


def test_sharp_peak_is_flagged_potentially_overfit() -> None:
    """SMA 247 good, 240/250 bad: an isolated spike on a flat, weak surface."""
    v = np.full((5, 5), 0.1)
    v[2, 2] = 1.5
    res = robustness(grid5(), surface(v), threshold=0.5)
    assert res.best == (2, 2)
    assert res.plateau_ratio == pytest.approx(0.1 / 1.5)
    assert res.potentially_overfit
    assert res.score < 0.1


def test_plateau_is_not_flagged() -> None:
    rng = np.random.default_rng(0)
    v = 0.9 + 0.1 * rng.random((5, 5))
    res = robustness(grid5(), surface(v), threshold=0.5)
    assert not res.potentially_overfit
    assert res.score > 0.8
    assert res.negative_share == 0.0


def test_negative_neighbours_are_penalised() -> None:
    v = np.full((5, 5), 1.0)
    v[1, 1] = v[1, 2] = v[1, 3] = -0.5
    res = robustness(grid5(), surface(v), threshold=0.5)
    clean = robustness(grid5(), surface(np.full((5, 5), 1.0)), threshold=0.5)
    assert clean.score == pytest.approx(1.0)
    assert res.plateau_centre not in {(1, 1), (1, 2), (1, 3)}


def test_plateau_centre_prefers_plateau_over_edge_spike() -> None:
    v = np.full((5, 5), 0.0)
    v[0, 4] = 2.0  # isolated spike at the edge
    v[2:5, 0:3] = 0.8  # broad plateau
    res = robustness(grid5(), surface(v), threshold=0.5)
    assert res.best == (0, 4)
    assert res.potentially_overfit
    assert res.plateau_centre == (3, 1)  # the middle of the plateau
    assert res.plateau_centre_metric == pytest.approx(0.8)


def test_edge_cases_fail_closed() -> None:
    g = grid5()
    neg = robustness(g, surface(np.full((5, 5), -0.2)), threshold=0.5)
    assert neg.score == 0.0 and neg.potentially_overfit
    one = robustness(ParamGrid.build({}, {}, 1), {(): 1.2}, threshold=0.5)
    assert one.score == 0.0 and one.potentially_overfit and math.isnan(one.plateau_ratio)
    partial = surface(np.full((5, 5), 1.0))
    partial[(2, 2)] = math.nan  # invalid combination
    res = robustness(g, partial, threshold=0.5)
    assert res.n_valid == 24 and res.n_points == 25
    assert neighbourhood_median(g, partial, (2, 2)) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="no valid"):
        robustness(g, {(0, 0): math.nan}, threshold=0.5)
