"""Risk engine: each rule exactly, fail-closed behaviour and randomized limit properties."""

import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from adaptive_quant.config.schema import RiskConfig
from adaptive_quant.core.models import TargetPortfolio
from adaptive_quant.quant.risk.engine import RiskEngine
from adaptive_quant.quant.risk.estimators import ewma_volatility, volatility_regime
from adaptive_quant.quant.risk.models import (
    ProposedPortfolio,
    RiskCalculationError,
    RiskContext,
    RiskDecision,
)
from adaptive_quant.trading.safety.preflight import BlockScope, RefusalReason
from adaptive_quant.trading.safety.risk_checks import RiskEngineCheck
from tests.unit.backtest.helpers import SETTINGS

NOW = datetime(2024, 3, 1, 20, tzinfo=UTC)
INST = SETTINGS.universe.by_symbol
QUIET = np.full(400, 0.0)  # zero volatility: vol targeting leaves exposure alone


def limits(**over: Any) -> RiskConfig:
    data = SETTINGS.risk.model_dump()
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict) and k != "max_position_weight":
            data[k] = {**data[k], **v}
        else:
            data[k] = v
    return RiskConfig.model_validate(data)


def ctx(**over: Any) -> RiskContext:
    base: dict[str, Any] = {
        "as_of": NOW,
        "equity": 100.0,
        "peak_equity": 100.0,
        "previous_equity": 100.0,
        "current_weights": {},
        "underlying_returns": QUIET,
    }
    return RiskContext(**{**base, **over})


def prop(**w: float) -> ProposedPortfolio:
    return ProposedPortfolio(NOW, dict(w))


def f(d: RiskDecision) -> dict[str, float]:
    return {s: float(v) for s, v in d.weights.items()}


def net(w: dict[str, float]) -> float:
    return sum(v * INST[s].leverage for s, v in w.items())


def test_within_limits_passes_unchanged() -> None:
    e = RiskEngine(limits(max_daily_turnover=4.0), INST)
    d = e.evaluate(prop(QQQ=0.5, TQQQ=0.25), ctx())
    assert f(d) == {"QQQ": 0.5, "TQQQ": 0.25}
    assert d.adjustments == () and d.band == "normal" and not d.risk_increasing_blocked
    assert isinstance(d.to_target("dec-1", "cfg"), TargetPortfolio)


@pytest.mark.parametrize(
    ("weights", "match"),
    [
        ({"XYZ": 0.1}, "unknown"),
        ({"NDX": 0.1}, "unknown or non-tradeable"),
        ({"QQQ": -0.2}, "invalid weight"),
        ({"QQQ": math.nan}, "invalid weight"),
        ({"QQQ": 0.7, "TQQQ": 0.4}, "sum"),
    ],
)
def test_sanity_refuses_bad_proposals(weights: dict[str, float], match: str) -> None:
    with pytest.raises(RiskCalculationError, match=match):
        RiskEngine(limits(), INST).evaluate(prop(**weights), ctx())


def test_volatility_target_scales_into_cash() -> None:
    r = np.random.default_rng(0).normal(0, 0.02, 400)
    lim = limits(max_daily_turnover=4.0)
    sigma = ewma_volatility(r, lim.volatility_target.lookback_days)
    d = RiskEngine(lim, INST).evaluate(prop(QQQ=0.4, TQQQ=0.3), ctx(underlying_returns=r))
    expected = min(lim.volatility_target.annualized_target / (sigma * 1.3), 1.0)
    assert d.vol_scale == pytest.approx(expected)
    assert f(d)["QQQ"] == pytest.approx(0.4 * expected, abs=2e-6)
    assert float(d.cash_weight) > 0.3
    assert d.adjustments[0].rule == "volatility_target"


def test_flat_history_is_a_normal_regime() -> None:
    assert volatility_regime(QUIET, limits().volatility_regimes)[0] == "normal"


def test_ewma_hand_value_and_history_requirements() -> None:
    r = np.array([0.01, -0.01, 0.02])
    a = 2 / 4
    w = (1 - a) ** np.array([2, 1, 0])
    assert ewma_volatility(r, 3) == pytest.approx(math.sqrt((w * r * r).sum() / w.sum() * 252))
    with pytest.raises(ValueError, match="needs 5"):
        ewma_volatility(r, 5)
    with pytest.raises(RiskCalculationError, match="underlying returns"):
        RiskEngine(limits(), INST).evaluate(prop(QQQ=0.5), ctx(underlying_returns=QUIET[:10]))
    bad = QUIET.copy()
    bad[-1] = math.nan
    with pytest.raises(RiskCalculationError, match="non-finite"):
        RiskEngine(limits(), INST).evaluate(prop(QQQ=0.5), ctx(underlying_returns=bad))


def test_drawdown_bands_and_hysteresis() -> None:
    e = RiskEngine(limits(max_daily_turnover=4.0), INST)
    p = prop(QQQ=0.34, TQQQ=0.66)  # net 2.32 -> capped by net limit 2.0 anyway
    d = e.evaluate(p, ctx(equity=85.0, previous_equity=85.0))  # 15% drawdown: caution 1.5x
    assert d.band == "caution" and net(f(d)) == pytest.approx(1.5, abs=1e-5)
    assert f(d)["QQQ"] == pytest.approx(0.34)  # TQQQ (most leveraged) reduced first
    d = e.evaluate(p, ctx(equity=75.0, previous_equity=75.0))  # 25%: defensive 0.75x
    assert d.band == "defensive" and net(f(d)) == pytest.approx(0.75, abs=1e-5)
    # recovering to 18% drawdown: hysteresis 5% keeps "defensive" until below 15%
    d2 = e.evaluate(p, ctx(equity=82.0, previous_equity=82.0, previous_band="defensive"))
    assert d2.band == "defensive"
    d3 = e.evaluate(p, ctx(equity=86.0, previous_equity=86.0, previous_band="defensive"))
    assert d3.band == "caution"


def test_emergency_band_blocks_increases_and_goes_flat() -> None:
    e = RiskEngine(limits(), INST)
    d = e.evaluate(
        prop(QQQ=1.0), ctx(equity=60.0, previous_equity=60.0, current_weights={"QQQ": 0.2})
    )
    assert d.band == "emergency" and d.risk_increasing_blocked
    assert f(d) == {}  # max net exposure 0 -> cash
    assert any("drawdown band emergency" in x for x in d.flags)


@pytest.mark.parametrize("state", ["kill", "daily_loss"])
def test_freeze_never_increases_a_position(state: str) -> None:
    e = RiskEngine(limits(max_daily_turnover=4.0), INST)
    c = ctx(current_weights={"QQQ": 0.3, "TQQQ": 0.1})
    c = ctx(
        current_weights={"QQQ": 0.3, "TQQQ": 0.1},
        kill_switch_engaged=state == "kill",
        previous_equity=100.0 if state == "kill" else 110.0,  # 9.1% daily loss > 6%
    )
    d = e.evaluate(prop(QQQ=0.1, TQQQ=0.5), c)
    assert d.risk_increasing_blocked
    assert f(d) == pytest.approx({"QQQ": 0.1, "TQQQ": 0.1})  # reduction allowed, increase not


def test_freeze_holds_instead_of_raising_net_exposure() -> None:
    e = RiskEngine(limits(max_daily_turnover=4.0), INST)
    c = ctx(current_weights={"TQQQ": 0.1, "SQQQ": 0.1}, kill_switch_engaged=True)
    d = e.evaluate(prop(SQQQ=0.1), c)  # cutting TQQQ alone would move net from 0 to -0.3
    assert f(d) == pytest.approx({"TQQQ": 0.1, "SQQQ": 0.1})


def test_regime_cap() -> None:
    rng = np.random.default_rng(1)
    r = np.concatenate([rng.normal(0, 0.005, 380), rng.normal(0, 0.05, 20)])  # vol spike
    lim = limits(max_daily_turnover=4.0, volatility_target={"enabled": False})
    regime, pct = volatility_regime(r, lim.volatility_regimes)
    assert regime == "extreme" and pct > 0.95
    d = RiskEngine(lim, INST).evaluate(prop(QQQ=0.5, TQQQ=0.5), ctx(underlying_returns=r))
    assert d.regime == "extreme" and net(f(d)) == pytest.approx(0.75, abs=1e-5)


def test_static_caps() -> None:
    e = RiskEngine(limits(max_daily_turnover=4.0), INST)
    d = e.evaluate(prop(SQQQ=0.5), ctx())
    assert f(d)["SQQQ"] == pytest.approx(0.15, abs=1e-5)  # inverse cap 0.45 / 3
    d = e.evaluate(prop(TQQQ=0.9), ctx())
    assert f(d)["TQQQ"] == pytest.approx(0.66, abs=1e-5)
    d = RiskEngine(
        limits(max_daily_turnover=4.0, max_gross_exposure=0.8, min_cash_weight=0.2), INST
    ).evaluate(prop(QQQ=0.5, TQQQ=0.5), ctx())
    assert sum(f(d).values()) <= 0.8 + 1e-6


def test_turnover_limits_increases_but_never_reductions() -> None:
    e = RiskEngine(limits(max_daily_turnover=0.5), INST)
    d = e.evaluate(prop(QQQ=0.9), ctx(current_weights={"TQQQ": 0.2}))
    # reducing TQQQ 0.2 is free of the limit's priority; the increase gets the 0.3 left
    assert f(d) == pytest.approx({"QQQ": 0.3})
    d = e.evaluate(prop(QQQ=0.2), ctx(current_weights={"TQQQ": 0.66}))
    assert f(d) == {}  # 0.66 of reductions exhaust the budget: no increase at all


def test_any_rule_failure_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    e = RiskEngine(limits(), INST)

    def boom(*_a: object, **_k: object) -> None:
        raise ZeroDivisionError("injected")

    monkeypatch.setattr(e, "_exposure_caps", boom)
    with pytest.raises(RiskCalculationError, match="ZeroDivisionError: injected"):
        e.evaluate(prop(QQQ=0.5), ctx())


def test_verification_catches_a_broken_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    e = RiskEngine(limits(), INST)
    monkeypatch.setattr(e, "_exposure_caps", lambda *_a: None)  # rule silently skipped
    with pytest.raises(RiskCalculationError, match="verification failed"):
        e.evaluate(prop(TQQQ=0.95), ctx())


def test_property_output_never_violates_any_limit() -> None:
    """Thousands of random proposals, account states, markets and limit sets."""
    rng = np.random.default_rng(42)
    syms = ["QQQ", "TQQQ", "SQQQ"]
    checked = 0
    for _ in range(3000):
        lim = limits(
            max_net_underlying_exposure=float(rng.uniform(0, 3)),
            max_inverse_net_exposure=float(rng.uniform(0, 3)),
            max_leveraged_etf_weight=float(rng.uniform(0, 1)),
            max_position_weight={s: float(rng.uniform(0, 1)) for s in syms},
            max_gross_exposure=float(rng.uniform(0.3, 1)),
            min_cash_weight=0.0,
            max_daily_turnover=float(rng.uniform(0, 2)),
            volatility_target={
                "enabled": bool(rng.random() < 0.7),
                "max_scale": float(rng.uniform(0.5, 1.5)),
            },
        )
        raw = rng.dirichlet(np.ones(4))[:3] * rng.uniform(0, 1)
        cur = rng.dirichlet(np.ones(4))[:3] * rng.uniform(0, 1)
        peak = 100.0
        equity = float(rng.uniform(55, 100))
        c = ctx(
            equity=equity,
            peak_equity=peak,
            previous_equity=float(equity * rng.uniform(0.9, 1.1)),
            current_weights=dict(zip(syms, cur.tolist(), strict=True)),
            underlying_returns=rng.normal(0, rng.uniform(0.001, 0.05), 400),
            kill_switch_engaged=bool(rng.random() < 0.1),
            previous_band=str(rng.choice(["normal", "caution", "defensive", "emergency"])),
        )
        d = RiskEngine(lim, INST).evaluate(prop(**dict(zip(syms, raw.tolist(), strict=True))), c)
        w = f(d)
        n = net(w)
        band = next(b for b in lim.drawdown_bands if b.name == d.band)
        cap = min(lim.max_net_underlying_exposure, band.max_net_exposure)
        rcap = lim.volatility_regimes.max_net_exposure.get(d.regime or "", math.inf)
        assert n <= min(cap, rcap) + 1e-5
        assert -n <= min(lim.max_inverse_net_exposure, band.max_net_exposure, rcap) + 1e-5
        assert sum(w.values()) <= lim.max_gross_exposure + 1e-5
        assert all(0 <= v <= lim.position_cap(s) + 1e-5 for s, v in w.items())
        assert w.get("TQQQ", 0) + w.get("SQQQ", 0) <= lim.max_leveraged_etf_weight + 1e-5
        current = c.current_weights
        if d.risk_increasing_blocked:
            assert all(v <= current.get(s, 0) + 1e-5 for s, v in w.items())
        inc = sum(max(v - current.get(s, 0), 0) for s, v in w.items())
        dec = sum(max(current.get(s, 0) - w.get(s, 0), 0) for s in current)
        if inc > 1e-5:
            assert inc + dec <= lim.max_daily_turnover + 1e-5
        checked += 1
    assert checked == 3000


def test_preflight_check_scopes() -> None:
    e = RiskEngine(limits(), INST)
    ok = RiskEngineCheck(lambda: e.evaluate(prop(QQQ=0.5), ctx()), 0.06).run()
    assert ok.passed

    def fail() -> RiskDecision:
        raise RiskCalculationError("boom")

    res = RiskEngineCheck(fail, 0.06).run()
    assert res.reason is RefusalReason.RISK_CALCULATION_FAILURE and res.scope is BlockScope.ALL
    emerg = RiskEngineCheck(
        lambda: e.evaluate(prop(), ctx(equity=60.0, previous_equity=60.0)), 0.06
    ).run()
    assert (
        emerg.reason is RefusalReason.DRAWDOWN_EMERGENCY
        and emerg.scope is BlockScope.RISK_INCREASING
    )
    loss = RiskEngineCheck(lambda: e.evaluate(prop(), ctx(previous_equity=110.0)), 0.06).run()
    assert (
        loss.reason is RefusalReason.DAILY_LOSS_LIMIT and loss.scope is BlockScope.RISK_INCREASING
    )
