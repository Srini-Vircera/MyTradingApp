"""Engine-level accounting, costs, partial fills, turnover and determinism."""

import hashlib
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.core.enums import OrderSide
from adaptive_quant.quant.strategies.base import Strategy
from adaptive_quant.quant.strategies.registry import create
from tests.unit.backtest.helpers import (
    ScriptedExposure,
    frames_from_returns,
    random_frames,
    run,
    session_list,
    zero_costs,
)

FRAMES = random_frames(seed=11)
SESS = session_list()


def test_frictionless_buy_and_hold_tracks_the_price_exactly() -> None:
    """Zero costs, zero buffer: after the fill, equity moves exactly with QQQ (to the cent)."""
    res = run(
        FRAMES,
        ScriptedExposure(base=1.0),
        execution="next_close",
        sizing_cash_buffer=0.0,
        threshold=0.02,
        **zero_costs(),
    )
    [first] = res.fills  # the 2% band means no further rebalancing
    qty = first.quantity
    closes = FRAMES["QQQ"]["close"]
    for i, ts in enumerate(pd.DatetimeIndex(res.daily.index)):
        equity = float(res.daily["equity"].iloc[i])
        if ts < first.time:
            assert equity == pytest.approx(100_000)
            continue
        price = Decimal(repr(float(closes.loc[ts]))).quantize(Decimal("0.000001"))
        assert Decimal(repr(equity)) == pytest.approx(
            res.portfolio.cash + qty * price, abs=Decimal("0.000001")
        )
    assert res.portfolio.commissions == 0
    assert res.portfolio.implicit_costs == 0


def test_costs_reduce_equity_by_exactly_their_amount() -> None:
    free = run(FRAMES, ScriptedExposure(base=1.0), execution="next_close", **zero_costs())
    costly = run(
        FRAMES,
        ScriptedExposure(base=1.0),
        execution="next_close",
        costs={
            "half_spread_bps": {"default": 10.0},
            "slippage_bps": 5.0,
            "impact_coefficient_bps": 0.0,
            "commission_per_order": 2.0,
        },
    )
    assert costly.portfolio.commissions == Decimal("2.00") * len(costly.fills)
    fills = costly.fills
    assert (
        sum((f.spread_cost + f.slippage_cost + f.impact_cost for f in fills), Decimal(0))
        == costly.portfolio.implicit_costs
    )
    for f in fills:
        gross = (f.quantity * f.ref_price).quantize(Decimal("0.01"))
        assert f.spread_cost + f.slippage_cost + f.impact_cost == abs(f.notional - gross)
        if f.side is OrderSide.BUY:
            assert f.fill_price > f.ref_price
        else:
            assert f.fill_price < f.ref_price
    assert costly.daily["equity"].iloc[-1] < free.daily["equity"].iloc[-1]
    assert costly.daily["commissions"].sum() == pytest.approx(float(costly.portfolio.commissions))


def test_daily_invariants_hold_for_a_real_strategy() -> None:
    res = run(
        FRAMES,
        create("st_ema_cross", params={"max_long_exposure": 3.0, "max_short_exposure": 1.0}),
        start=date(2020, 3, 2),
        execution="next_open",
    )
    d = res.daily
    assert (d["cash"] >= 0).all()
    assert (d["gross_exposure"] <= 1.0 + 1e-9).all()
    for sym in ("QQQ", "TQQQ", "SQQQ"):
        assert (d[f"w_{sym}"] >= 0).all()
    assert (d["turnover"] >= 0).all()
    # (the engine itself raises if equity != cash + positions or the P&L identity breaks)
    p = res.portfolio
    assert (
        p.identity_gap(
            {
                s: Decimal(repr(float(FRAMES[s]["close"].iloc[-1]))).quantize(Decimal("0.000001"))
                for s in ("QQQ", "TQQQ", "SQQQ")
            }
        )
        == 0
    )


def test_turnover_is_traded_notional_over_equity() -> None:
    res = run(FRAMES, ScriptedExposure(base=1.0), execution="next_close", **zero_costs())
    day = res.fills[0].time
    row = res.daily.loc[pd.Timestamp(day)]
    traded = sum(float(f.notional) for f in res.fills if f.time == day)
    assert row["turnover"] == pytest.approx(traded / row["equity"])


def test_rebalance_threshold_suppresses_small_trades() -> None:
    wobble = {s.date: 1.0 + (0.01 if i % 2 else -0.01) for i, s in enumerate(SESS)}
    busy = run(FRAMES, ScriptedExposure(wobble), threshold=0.0, **zero_costs())
    calm = run(FRAMES, ScriptedExposure(wobble), threshold=0.02, **zero_costs())
    assert len(calm.fills) < len(busy.fills) / 10


def test_participation_cap_creates_partial_fills() -> None:
    n = len(SESS)
    frames = frames_from_returns(np.random.default_rng(3).normal(0, 0.01, n), volume=10_000.0)
    res = run(
        frames,
        ScriptedExposure(base=1.0),
        execution="next_close",
        start=date(2020, 3, 2),
        costs={"max_participation": 0.01, "half_spread_bps": {"default": 0.0}},
    )
    first = res.fills[0]
    assert first.limited_by == "participation"
    assert first.quantity == Decimal("100.000000")  # 1% of 10,000 shares ADV
    assert any("participation" in c.reason for c in res.cancelled)
    # the system keeps working towards the target on later sessions
    assert sum(f.quantity for f in res.fills if f.symbol == "QQQ") > first.quantity


def test_unknown_volume_policy() -> None:
    n = len(SESS)
    frames = frames_from_returns(np.random.default_rng(3).normal(0, 0.01, n), volume=0.0)
    liquid = run(
        frames,
        ScriptedExposure(base=1.0),
        costs={"unknown_volume": "assume_liquid", "half_spread_bps": {"default": 1.0}},
    )
    assert liquid.fills and liquid.fills[0].limited_by == ""
    strict = run(
        frames,
        ScriptedExposure(base=1.0),
        costs={"unknown_volume": "reject", "half_spread_bps": {"default": 1.0}},
    )
    assert not strict.fills
    assert all(c.reason == "volume unknown" for c in strict.cancelled)


def test_whole_shares_when_fractional_disabled() -> None:
    res = run(FRAMES, ScriptedExposure(base=1.0), fractional=False, **zero_costs())
    assert all(f.quantity == f.quantity.to_integral_value() for f in res.fills)


def test_cash_interest_accrues_on_idle_cash() -> None:
    res = run(FRAMES, ScriptedExposure(base=0.0), cash_interest_annual=0.0365, **zero_costs())
    # 1e-4 per calendar day, compounded per session, credited in cents
    days = (SESS[-1].date - res.daily.index[0].tz_convert("America/New_York").date()).days
    expected = 100_000 * (1.0001**days)
    assert res.daily["equity"].iloc[-1] == pytest.approx(expected, rel=1e-5)
    assert res.portfolio.interest > 0


def test_inverse_exposure_holds_sqqq_never_shorts() -> None:
    res = run(FRAMES, ScriptedExposure(base=-0.9), threshold=0.02, **zero_costs())
    assert {f.symbol for f in res.fills} == {"SQQQ"}
    assert res.fills[0].side is OrderSide.BUY  # bearish exposure = *holding* the inverse ETF
    assert (res.daily["w_SQQQ"].iloc[5:] > 0).all()
    assert (res.daily[["w_QQQ", "w_TQQQ"]] == 0).all().all()
    # the 2% weight band (x3 leverage = 0.06 net) is enforced at decision time; the
    # session's own move happens before the close mark, so allow one day of drift on top
    dev = (res.daily["net_exposure"].iloc[1:] - (-0.9 * 0.995)).abs()
    assert dev.mean() < 0.03
    assert dev.max() < 0.12


def _fingerprint(res) -> str:  # type: ignore[no-untyped-def]
    h = hashlib.sha256()
    h.update(pd.util.hash_pandas_object(res.daily, index=True).to_numpy().tobytes())
    for f in res.fills:
        h.update(repr(f).encode())
    for d in res.decisions:
        h.update(repr((d.decision_time, d.allocation.weights, d.order_ids)).encode())
    return h.hexdigest()


def test_results_are_deterministic() -> None:
    def strat() -> Strategy:
        return create("bo_donchian", params={"window": 20, "max_long_exposure": 2.0})

    a = run(FRAMES, strat(), start=date(2020, 3, 2))
    b = run(FRAMES, strat(), start=date(2020, 3, 2))
    shuffled = dict(reversed(list(FRAMES.items())))
    c = run(shuffled, strat(), start=date(2020, 3, 2))
    assert _fingerprint(a) == _fingerprint(b) == _fingerprint(c)


def test_backtest_does_not_mutate_input_frames() -> None:
    before = {k: v.copy() for k, v in FRAMES.items()}
    run(FRAMES, create("st_ema_cross"), start=date(2020, 3, 2))
    for k, v in FRAMES.items():
        pd.testing.assert_frame_equal(v, before[k])


def test_missing_bars_for_a_tradeable_instrument_refuse_to_run() -> None:
    from adaptive_quant.core.errors import DataQualityError

    holey = dict(FRAMES)
    holey["TQQQ"] = FRAMES["TQQQ"].drop(index=FRAMES["TQQQ"].index[100])
    with pytest.raises(DataQualityError, match="TQQQ has no bar"):
        run(holey, ScriptedExposure())
