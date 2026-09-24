"""Walk-forward windows, plateau-aware selection and strict IS/OOS separation."""

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.research.robustness import ParamGrid
from adaptive_quant.quant.research.walkforward import (
    WalkForwardSettings,
    annual_sharpe,
    select,
    walk_forward,
    windows,
)

Y = 1 / 252  # one session, in years


def settings(**kw: float | int | str) -> WalkForwardSettings:
    base: dict[str, float | int | str] = {
        "train_years": 100 * Y,
        "validate_years": 50 * Y,
        "test_years": 40 * Y,
        "step_years": 40 * Y,
        "top_k": 2,
    }
    return WalkForwardSettings(**{**base, **kw})  # type: ignore[arg-type]


def test_rolling_and_anchored_windows_are_exact() -> None:
    w = windows(400, settings())
    assert [(x.train, x.validate, x.test) for x in w] == [
        ((0, 100), (100, 150), (150, 190)),
        ((40, 140), (140, 190), (190, 230)),
        ((80, 180), (180, 230), (230, 270)),
        ((120, 220), (220, 270), (270, 310)),
        ((160, 260), (260, 310), (310, 350)),
        ((200, 300), (300, 350), (350, 390)),
    ]  # the last 10 rows are shorter than a full test window and than 21 sessions: dropped
    a = windows(400, settings(scheme="anchored"))
    assert all(x.train[0] == 0 for x in a)
    assert [x.test for x in a] == [x.test for x in w]
    for x in w:  # OOS never overlaps IS or other OOS windows
        assert x.train[1] <= x.validate[0] and x.validate[1] <= x.test[0]


def test_settings_validation() -> None:
    with pytest.raises(ValueError, match="overlap"):
        settings(step_years=10 * Y)
    with pytest.raises(ValueError, match="scheme"):
        settings(scheme="expanding")
    with pytest.raises(ValueError, match="top_k"):
        settings(top_k=0)


def grid3() -> ParamGrid:
    return ParamGrid.build({}, {"n": [1, 2, 3, 4, 5]}, 10)


def matrix(n: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(rng.normal(0.0003, 0.01, (n, 5)), index=idx)


def test_selection_prefers_plateau_to_isolated_spike() -> None:
    g = grid3()
    coords = g.coords()
    rng = np.random.default_rng(1)
    h = rng.normal(0, 0.01, (150, 5))
    h[:, 0] += 0.004  # isolated spike at n = 1 (its neighbour n = 2 is weak)
    h[:, 2:5] += 0.0025  # plateau over n = 3, 4, 5
    chosen, cands = select(g, coords, h, (0, 100), (100, 150), top_k=1)
    assert chosen in {(2,), (3,), (4,)}  # inside the plateau, never the spike
    assert annual_sharpe(h[:100, 0]) > annual_sharpe(h[:100, coords.index(chosen)])
    assert cands == (chosen,)
    with pytest.raises(ValueError, match="validation window"):
        select(g, coords, h[:120], (0, 100), (100, 150), top_k=1)


def test_walk_forward_stitches_exactly_the_chosen_test_slices() -> None:
    g, m = grid3(), matrix()
    res = walk_forward(g, g.coords(), m, settings())
    pieces = []
    for f in res.folds:
        j = g.coords().index(f.chosen)
        pieces.append(m.iloc[f.window.test[0] : f.window.test[1], j])
        assert f.test_sharpe == pytest.approx(annual_sharpe(pieces[-1].to_numpy()))
        assert f.chosen in f.candidates
    pd.testing.assert_series_equal(res.oos_returns, pd.concat(pieces), check_names=False)
    assert len(res.oos_returns) == 240
    assert res.parameter_changes == sum(
        a.chosen != b.chosen for a, b in zip(res.folds, res.folds[1:], strict=False)
    )


def test_no_look_ahead_future_poisoning_cannot_change_past_choices() -> None:
    """Replacing everything from fold k's test window onwards leaves folds <= k identical."""
    g, m = grid3(), matrix(seed=4)
    base = walk_forward(g, g.coords(), m, settings())
    for k in range(len(base.folds)):
        cut = base.folds[k].window.test[0]
        poisoned = m.copy()
        poisoned.iloc[cut:, :] = np.random.default_rng(k).normal(0, 0.2, (len(m) - cut, 5))
        poisoned.iloc[cut:, 4] += 0.5  # an absurdly good future for one column
        res = walk_forward(g, g.coords(), poisoned, settings())
        for a, b in zip(base.folds[: k + 1], res.folds[: k + 1], strict=True):
            assert a.chosen == b.chosen
            assert a.train_sharpe == b.train_sharpe and a.validate_sharpe == b.validate_sharpe


def test_too_short_history_gives_no_folds() -> None:
    g = grid3()
    res = walk_forward(g, g.coords(), matrix(n=120), settings())
    assert res.folds == () and len(res.oos_returns) == 0
    with pytest.raises(ValueError, match="one returns column"):
        walk_forward(g, g.coords()[:2], matrix(), settings())


def test_fold_dates_are_inclusive_and_never_overlap() -> None:
    g, m = grid3(), matrix()
    for f in walk_forward(g, g.coords(), m, settings()).folds:
        tr0, tr1, va0, va1, te0, te1 = f.dates
        assert tr0 <= tr1 < va0 <= va1 < te0 <= te1
