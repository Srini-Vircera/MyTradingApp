"""The most important backtest tests: no look-ahead, correct execution timing.

* scripted switches prove *which* bar each decision saw and *where* it filled;
* future-poisoning proves nothing after time T affects anything decided by T;
* the sign-of-return oracle proves the engine cannot trade the bar it learns from
  (a leaking engine would turn it into a money machine - we show that too);
* runtime guards raise ``LookAheadError`` for any fill that would use future data.
"""

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.core.enums import OrderSide
from adaptive_quant.quant.analytics.metrics import daily_returns, sharpe
from adaptive_quant.quant.backtest.engine import LookAheadError, Order
from adaptive_quant.quant.backtest.execution import FillPoint
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.strategies.registry import create
from tests.unit.backtest.helpers import (
    BT_START,
    ScriptedExposure,
    SignOfLastReturn,
    frames_from_returns,
    random_frames,
    run,
    session_list,
    zero_costs,
)

FRAMES = random_frames(seed=3)
SESS = session_list()
SWITCH = 30  # index of the session whose bar triggers a switch to 100% QQQ
SEEN = SESS[SWITCH].date
MODELS = ["near_close", "next_open", "next_close", "closing_auction"]


def first_fill(model: str, delay: int = 0):  # type: ignore[no-untyped-def]
    res = run(
        FRAMES,
        ScriptedExposure({SEEN: 1.0}),
        execution=model,
        execution_delay_bars=delay,
        **zero_costs(),
    )
    fills = [f for f in res.fills if f.symbol == "QQQ"]
    assert fills, "switch produced no fill"
    return res, fills[0]


@pytest.mark.parametrize(
    ("model", "decision_i", "fill_i", "point"),
    [
        # near_close: decision at close(t)-15m sees bars through close(t-1)=SEEN, fills close(t)
        ("near_close", SWITCH + 1, SWITCH + 1, "close"),
        # next_open: decision at close(SEEN), fills at the NEXT open
        ("next_open", SWITCH, SWITCH + 1, "open"),
        # next_close: decision at close(SEEN), fills at the NEXT close
        ("next_close", SWITCH, SWITCH + 1, "close"),
        # closing_auction: decision at close(SEEN) fills at that SAME close (explicit, flagged)
        ("closing_auction", SWITCH, SWITCH, "close"),
    ],
)
def test_each_model_fills_at_the_right_bar_and_price(
    model: str, decision_i: int, fill_i: int, point: str
) -> None:
    _, fill = first_fill(model)
    s_fill = SESS[fill_i]
    expected_time = s_fill.open if point == "open" else s_fill.close
    assert fill.time == expected_time
    bar_price = float(FRAMES["QQQ"][point].loc[pd.Timestamp(s_fill.close)])
    assert fill.ref_price == Decimal(repr(bar_price)).quantize(Decimal("0.000001"))
    assert fill.fill_price == fill.ref_price  # zero costs
    assert fill.data_timestamp == SESS[SWITCH].close  # the bar that triggered it
    if model == "near_close":
        assert fill.decision_time == SESS[decision_i].close - timedelta(minutes=15)
    else:
        assert fill.decision_time == SESS[decision_i].close


@pytest.mark.parametrize("model", MODELS)
def test_fills_never_precede_or_coincide_with_their_data(model: str) -> None:
    res = run(
        FRAMES,
        create("st_ema_cross", params={"max_long_exposure": 2.0}),
        execution=model,
        start=date(2020, 3, 2),
    )
    assert res.fills
    for f in res.fills:
        assert f.decision_time >= f.data_timestamp
        assert f.time >= f.decision_time
        if model == "closing_auction":
            assert f.time >= f.data_timestamp
        else:
            assert f.time > f.data_timestamp, (
                f"{model}: fill at {f.time} uses data from {f.data_timestamp}"
            )
    assert res.same_bar_close is (model == "closing_auction")


def test_same_bar_close_is_only_possible_when_explicitly_chosen() -> None:
    for model in MODELS:
        res = run(FRAMES, ScriptedExposure({SEEN: 1.0}), execution=model, **zero_costs())
        same_bar = [f for f in res.fills if f.time == f.data_timestamp]
        if model == "closing_auction":
            assert same_bar and res.same_bar_close
            assert any("closing_auction" in w for w in res.warnings)
        else:
            assert not same_bar
            assert not res.same_bar_close
    # a delay removes the same-bar property even for the auction model
    delayed = run(
        FRAMES, ScriptedExposure({SEEN: 1.0}), execution="closing_auction", execution_delay_bars=1
    )
    assert not delayed.same_bar_close
    assert all(f.time > f.data_timestamp for f in delayed.fills)


@pytest.mark.parametrize("delay", [0, 1, 3])
@pytest.mark.parametrize("model", ["near_close", "next_open", "next_close"])
def test_execution_delay_shifts_fills_by_exact_sessions(model: str, delay: int) -> None:
    _, base = first_fill(model, 0)
    _, delayed = first_fill(model, delay)
    idx = {s.close: i for i, s in enumerate(SESS)} | {s.open: i for i, s in enumerate(SESS)}
    assert idx[delayed.time] - idx[base.time] == delay


@pytest.mark.parametrize("model", MODELS)
def test_future_poisoning_changes_nothing_decided_before_it(model: str) -> None:
    """Replace every bar after T with wild prices: all decisions made by T, all fills
    completed by T and the equity curve through T must be bit-for-bit identical."""
    strategy = create(
        "mom_multi_horizon",
        params={"horizons": "5,10,20", "max_long_exposure": 2.0, "max_short_exposure": 1.0},
    )
    t_index = 150
    cut = pd.Timestamp(SESS[t_index].close)
    poisoned = {}
    rng = np.random.default_rng(99)
    for sym, df in FRAMES.items():
        p = df.copy()
        after = p.index > cut
        shock = np.exp(rng.normal(0, 0.3, int(after.sum())))
        for col in ("open", "high", "low", "close"):
            p.loc[after, col] = p.loc[after, col] * shock
        p.loc[after, "high"] = p.loc[after, ["open", "high", "low", "close"]].max(axis=1)
        p.loc[after, "low"] = p.loc[after, ["open", "high", "low", "close"]].min(axis=1)
        poisoned[sym] = p
    a = run(FRAMES, strategy, execution=model, start=date(2020, 4, 1))
    b = run(poisoned, strategy, execution=model, start=date(2020, 4, 1))
    pd.testing.assert_frame_equal(a.daily.loc[:cut], b.daily.loc[:cut])
    da = [d for d in a.decisions if d.decision_time <= cut]
    db = [d for d in b.decisions if d.decision_time <= cut]
    assert [(d.decision_time, d.allocation.weights, d.order_ids) for d in da] == [
        (d.decision_time, d.allocation.weights, d.order_ids) for d in db
    ]
    assert [f for f in a.fills if f.time <= cut] == [f for f in b.fills if f.time <= cut]
    # and the poison really did change the future (the test has teeth)
    assert not a.daily["equity"].equals(b.daily["equity"])


def test_sign_oracle_has_no_edge_on_iid_returns() -> None:
    """If the engine leaked the traded bar, 'long after an up day' would earn |r| every day."""
    n = len(SESS)
    rng = np.random.default_rng(2024)
    r = rng.normal(0.0, 0.02, n)
    frames = frames_from_returns(r)
    for model in MODELS:
        res = run(frames, SignOfLastReturn(), execution=model, **zero_costs())
        s = sharpe(daily_returns(res.daily["equity"]))
        assert s is not None and abs(s) < 2.0, f"{model}: Sharpe {s:.2f} suggests look-ahead"
    # power check: the P&L a *leaking* engine would book (trade the bar you learn from)
    leak = pd.Series(np.where(r > 0, r, 0.0))
    leak_sharpe = leak.mean() / leak.std() * np.sqrt(252)
    assert leak_sharpe > 5.0


def test_orders_are_sized_with_decision_time_prices_not_fill_prices() -> None:
    n = len(SESS)
    r = np.zeros(n)
    r[SWITCH + 1] = 0.25  # the fill session closes 25% higher than the decision-time close
    frames = frames_from_returns(r)
    res = run(
        frames,
        ScriptedExposure({SEEN: 1.0}),
        execution="next_close",
        sizing_cash_buffer=0.0,
        **zero_costs(),
    )
    fill = next(f for f in res.fills if f.symbol == "QQQ")
    decision_price = Decimal(repr(float(frames["QQQ"]["close"].iloc[SWITCH]))).quantize(
        Decimal("0.000001")
    )
    expected_qty = (Decimal("100000") / decision_price).quantize(
        Decimal("0.000001"), rounding="ROUND_DOWN"
    )
    assert fill.requested_quantity == expected_qty
    # the higher fill price means the full order is not affordable: it is trimmed, never overdrawn
    assert fill.limited_by == "cash"
    assert res.portfolio.cash >= 0


def test_precomputed_backtest_signals_equal_live_path_signals() -> None:
    """The engine's fast path must reproduce generate_signal() on a point-in-time view exactly."""
    for sid, params in (
        ("ltt_sma_distance", {"window": 50}),
        ("regime_transition", {"window": 50, "vol_lookback": 60}),
        ("bo_trend_volume", {"trend_window": 50}),
    ):
        strategy = create(sid, params=params)
        res = run(FRAMES, strategy, start=date(2020, 6, 1))
        for d in res.decisions[::15]:
            live = strategy.generate_signal(MarketDataView(FRAMES, d.decision_time))
            assert d.signals[0] == live


def test_runtime_guard_rejects_fills_that_use_future_data() -> None:
    from adaptive_quant.quant.backtest.engine import BacktestEngine, EngineSettings
    from tests.data_helpers import calendar
    from tests.unit.backtest.helpers import SETTINGS, backtest_config

    engine = BacktestEngine(
        frames=FRAMES,
        strategies=[ScriptedExposure()],
        instruments=SETTINGS.universe.by_symbol,
        calendar=calendar(),
        settings=EngineSettings(backtest_config(), 0.0, True, None),
    )
    s = SESS[40]
    order = Order(1, "QQQ", OrderSide.BUY, Decimal(1), s.close, s.close, 40, FillPoint.CLOSE, None)
    with pytest.raises(LookAheadError, match="using data from"):
        engine._check_timing(order, s.close, same_bar=False)  # same bar, not auction
    engine._check_timing(order, s.close, same_bar=True)  # allowed only for the explicit auction
    early = Order(2, "QQQ", OrderSide.BUY, Decimal(1), s.close, s.open, 40, FillPoint.OPEN, None)
    with pytest.raises(LookAheadError, match="before it was decided"):
        engine._check_timing(early, s.open, same_bar=False)


def test_backtest_refuses_when_strategy_lacks_history_at_start() -> None:
    from adaptive_quant.core.errors import InsufficientHistoryError

    with pytest.raises(InsufficientHistoryError):
        run(FRAMES, create("ltt_sma_distance"), start=BT_START)  # needs 200 bars
