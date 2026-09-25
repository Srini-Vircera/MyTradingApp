"""Multiple-testing statistics: published examples, hand cases and properties."""

import math
from itertools import combinations

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.research.stats import (
    benjamini_hochberg,
    bh_reject,
    bootstrap_ci,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    moments,
    pbo_cscv,
    probabilistic_sharpe_ratio,
    reality_check,
    sharpe_per_period,
    sharpe_pvalue,
    stationary_bootstrap_indices,
)


# ------------------------------------------------------------------ Deflated Sharpe
def test_deflated_sharpe_published_example() -> None:
    """Bailey & Lopez de Prado (2014), numerical example: 100 trials, annualised variance of
    trial Sharpes 1/2, best annualised SR 2.5 over 1,250 daily returns, skew -3, kurtosis 10.
    The paper reports SR0 ~ 0.1132 (daily) and DSR ~ 0.9004."""
    sr0 = expected_max_sharpe(100, 0.5 / 250)
    assert sr0 == pytest.approx(0.1132, abs=5e-5)
    dsr = probabilistic_sharpe_ratio(2.5 / math.sqrt(250), sr0, 1250, skew=-3.0, kurtosis=10.0)
    assert dsr == pytest.approx(0.9004, abs=5e-4)


def test_psr_basic_properties() -> None:
    assert probabilistic_sharpe_ratio(0.1, 0.1, 500) == pytest.approx(0.5)
    assert probabilistic_sharpe_ratio(0.1, 0.0, 500) > probabilistic_sharpe_ratio(0.1, 0.0, 100)
    # negative skew and fat tails widen the SR standard error -> lower confidence
    base = probabilistic_sharpe_ratio(0.1, 0.0, 250)
    assert probabilistic_sharpe_ratio(0.1, 0.0, 250, skew=-2, kurtosis=9) < base
    assert math.isnan(probabilistic_sharpe_ratio(math.nan, 0.0, 250))
    assert math.isnan(probabilistic_sharpe_ratio(0.1, 0.0, 1))


def test_expected_max_sharpe_grows_with_trials() -> None:
    assert expected_max_sharpe(1, 0.01) == 0.0
    assert expected_max_sharpe(10, 0.0) == 0.0
    vals = [expected_max_sharpe(n, 0.01) for n in (2, 10, 100, 1000)]
    assert vals == sorted(vals) and vals[0] > 0


def test_deflated_sharpe_from_returns_uses_sample_moments() -> None:
    r = np.random.default_rng(0).normal(0.001, 0.01, 2000)
    d = deflated_sharpe_ratio(r, n_trials=50, var_trial_sr=0.0004)
    skew, kurt = moments(r)
    assert d.sharpe == pytest.approx(sharpe_per_period(r))
    assert (d.skew, d.kurtosis) == (skew, kurt)
    assert abs(d.skew) < 0.2 and d.kurtosis == pytest.approx(3.0, abs=0.3)
    assert d.dsr == pytest.approx(probabilistic_sharpe_ratio(d.sharpe, d.sr0, 2000, skew, kurt))
    # more trials -> higher bar -> lower DSR
    assert deflated_sharpe_ratio(r, 5000, 0.0004).dsr < d.dsr


def test_sharpe_pvalue() -> None:
    rng = np.random.default_rng(1)
    assert sharpe_pvalue(rng.normal(0.002, 0.01, 1000)) < 0.001
    assert sharpe_pvalue(rng.normal(-0.002, 0.01, 1000)) > 0.999
    assert sharpe_pvalue(np.zeros(100)) == 1.0  # undefined -> no evidence


# ------------------------------------------------------------------ Benjamini-Hochberg
BH_1995 = [0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344, 0.0459,
           0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.0000]  # fmt: skip


def test_benjamini_hochberg_published_example() -> None:
    """Benjamini & Hochberg (1995), section 4: at q = 0.05 exactly the four smallest
    of these 15 p-values are rejected (Bonferroni would reject only three)."""
    assert bh_reject(BH_1995, 0.05) == [True] * 4 + [False] * 11
    assert sum(p <= 0.05 / 15 for p in BH_1995) == 3


def test_bh_adjusted_matches_definition_and_keeps_order() -> None:
    rng = np.random.default_rng(3)
    p = rng.random(25).tolist()
    adj = benjamini_hochberg(p)
    m = len(p)
    order = sorted(range(m), key=lambda i: p[i])
    for rank, i in enumerate(order, 1):
        expected = min(min(p[order[j - 1]] * m / j for j in range(rank, m + 1)), 1.0)
        assert adj[i] == pytest.approx(expected)
    assert benjamini_hochberg([]) == []
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        benjamini_hochberg([0.1, 1.5])


# ------------------------------------------------------------------ bootstrap
def test_stationary_bootstrap_structure() -> None:
    idx = stationary_bootstrap_indices(500, 10.0, 400, np.random.default_rng(0))
    assert idx.shape == (400, 500)
    assert idx.min() >= 0 and idx.max() < 500
    # blocks continue with the next index (wrapping); breaks occur with probability 1/L
    cont = (idx[:, 1:] == (idx[:, :-1] + 1) % 500).mean()
    assert cont == pytest.approx(1 - 1 / 10, abs=0.01)
    again = stationary_bootstrap_indices(500, 10.0, 400, np.random.default_rng(0))
    assert (idx == again).all()
    with pytest.raises(ValueError, match="mean_block"):
        stationary_bootstrap_indices(10, 0.5, 1, np.random.default_rng(0))


def test_bootstrap_ci_is_deterministic_and_sensible() -> None:
    r = np.random.default_rng(4).normal(0.001, 0.01, 1500)
    ci = bootstrap_ci(r, np.mean, samples=500, mean_block=5, confidence=0.95, seed=9)
    assert ci.lower < ci.point < ci.upper
    se = r.std(ddof=1) / math.sqrt(len(r))
    assert ci.upper - ci.lower == pytest.approx(2 * 1.96 * se, rel=0.25)
    assert ci == bootstrap_ci(r, np.mean, samples=500, mean_block=5, confidence=0.95, seed=9)


# ------------------------------------------------------------------ Reality Check / SPA
def _models(n: int, k: int, seed: int, means: dict[int, float] | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    d = rng.normal(0.0, 0.01, (n, k))
    for j, mu in (means or {}).items():
        d[:, j] += mu
    return pd.DataFrame(d, columns=[f"m{j}" for j in range(k)])


def test_reality_check_detects_a_real_edge() -> None:
    res = reality_check(_models(1000, 20, 0, {7: 0.003}), samples=500, mean_block=5, seed=1)
    assert res.best == "m7"
    assert res.reality_check_p < 0.01 and res.spa_p < 0.01


def test_reality_check_does_not_reward_luck() -> None:
    # 20 zero-mean models: the best one looks good in-sample but is not significant
    ps = [
        reality_check(_models(750, 20, s), samples=400, mean_block=5, seed=s).reality_check_p
        for s in range(8)
    ]
    assert min(ps) > 0.05 or sum(p < 0.05 for p in ps) <= 1
    assert np.mean(ps) > 0.25


def test_spa_is_less_distorted_by_poor_models_than_rc() -> None:
    means = {0: 0.0012, **{j: -0.01 for j in range(1, 60)}}
    res = reality_check(_models(800, 60, 5, means), samples=600, mean_block=5, seed=2)
    assert res.spa_p <= res.reality_check_p


def test_reality_check_input_validation() -> None:
    with pytest.raises(ValueError, match="10 observations"):
        reality_check(_models(5, 2, 0), samples=10, mean_block=2, seed=0)


# ------------------------------------------------------------------ PBO / CSCV
def test_pbo_hand_example_always_overfit() -> None:
    """Two trials, two partitions: A wins the first half and loses the second, B the reverse.
    Whichever half is in-sample, its winner is the out-of-sample loser: rank 1 of 2,
    omega = 1/3, logit = ln(1/2) < 0 in both combinations -> PBO = 1."""
    a = np.r_[np.full(10, 0.01), np.full(10, -0.01)] + np.tile([0.001, -0.001], 10)
    b = -a + np.tile([0.0005, -0.0005], 10)
    res = pbo_cscv(pd.DataFrame({"A": a, "B": b}), partitions=2)
    assert res.n_combinations == 2
    assert res.pbo == 1.0
    assert res.logits == pytest.approx((math.log(0.5), math.log(0.5)))
    assert res.prob_oos_loss == 1.0


def test_pbo_noise_is_near_half_and_skill_is_near_zero() -> None:
    rng = np.random.default_rng(11)
    noise = pd.DataFrame(rng.normal(0, 0.01, (800, 20)))
    assert 0.3 <= pbo_cscv(noise, 10).pbo <= 0.7
    skilled = noise.copy()
    skilled[5] = skilled[5] + 0.004
    res = pbo_cscv(skilled, 10)
    assert res.pbo < 0.05
    assert res.prob_oos_loss < 0.05


def test_pbo_fast_path_equals_reference_implementation() -> None:
    rng = np.random.default_rng(12)
    m = pd.DataFrame(rng.normal(0.0002, 0.01, (240, 7)))

    def sharpe_cols(x: np.ndarray) -> np.ndarray:
        return x.mean(axis=0) / x.std(axis=0, ddof=1)

    fast = pbo_cscv(m, 6)
    slow = pbo_cscv(m, 6, metric=sharpe_cols)
    assert fast.logits == pytest.approx(slow.logits)
    # and against a from-scratch loop over the combinations
    blocks = np.array_split(np.arange(240), 6)
    ref = []
    for combo in combinations(range(6), 3):
        is_rows = np.concatenate([blocks[i] for i in combo])
        oos_rows = np.concatenate([blocks[i] for i in range(6) if i not in combo])
        best = int(np.argmax(sharpe_cols(m.to_numpy()[is_rows])))
        oos = sharpe_cols(m.to_numpy()[oos_rows])
        rank = 1 + sum(o < oos[best] for o in oos)
        w = rank / 8
        ref.append(math.log(w / (1 - w)))
    assert fast.logits == pytest.approx(ref)


def test_pbo_input_validation() -> None:
    m = pd.DataFrame(np.zeros((40, 3)))
    with pytest.raises(ValueError, match="two trials"):
        pbo_cscv(m[[0]], 4)
    with pytest.raises(ValueError, match="even"):
        pbo_cscv(m, 3)
    with pytest.raises(ValueError, match="observations"):
        pbo_cscv(m.iloc[:20], 16)
