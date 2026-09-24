"""Golden-number tests for price adjustment (hand-computed expected values)."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from adaptive_quant.quant.data.bars import Adjustment, make_bars
from adaptive_quant.quant.data.corporate_actions import (
    ActionType,
    CorporateAction,
    apply_adjustment,
)

pytestmark = pytest.mark.regression

# Four sessions; a 2-for-1 split effective day 3, a $1.00 dividend ex day 4.
DAYS = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
RAW = make_bars(
    [datetime(d.year, d.month, d.day, 21, tzinfo=UTC) for d in DAYS],
    open=[100.0, 102.0, 51.0, 49.0],
    high=[101.0, 104.0, 52.0, 50.0],
    low=[99.0, 101.0, 50.0, 48.0],
    close=[100.0, 104.0, 52.0, 49.5],
    volume=[1000.0, 1000.0, 2000.0, 2000.0],
)
SPLIT = CorporateAction(symbol="X", ex_date=DAYS[2], type=ActionType.SPLIT, ratio=2.0)
DIV = CorporateAction(symbol="X", ex_date=DAYS[3], type=ActionType.CASH_DIVIDEND, amount=1.0)


def test_split_adjustment() -> None:
    adj = apply_adjustment(RAW, [SPLIT, DIV], Adjustment.SPLIT)
    assert adj["close"].tolist() == pytest.approx([50.0, 52.0, 52.0, 49.5])
    assert adj["volume"].tolist() == pytest.approx([2000.0, 2000.0, 2000.0, 2000.0])


def test_total_return_adjustment() -> None:
    adj = apply_adjustment(RAW, [SPLIT, DIV], Adjustment.ALL)
    f = 1 - 1.0 / 52.0  # dividend factor uses raw close before ex-date
    assert adj["close"].tolist() == pytest.approx([50.0 * f, 52.0 * f, 52.0 * f, 49.5])
    # Standard multiplicative convention: ex-date return = P_t / (P_prev - D) - 1.
    # (Differs from (P_t + D) / P_prev - 1 only at second order.)
    r = adj["close"].iloc[3] / adj["close"].iloc[2] - 1
    assert r == pytest.approx(49.5 / (52.0 - 1.0) - 1)


def test_raw_is_unchanged() -> None:
    pd.testing.assert_frame_equal(apply_adjustment(RAW, [SPLIT, DIV], Adjustment.RAW), RAW)


def test_reverse_split() -> None:
    rev = CorporateAction(symbol="X", ex_date=DAYS[2], type=ActionType.SPLIT, ratio=0.25)
    adj = apply_adjustment(RAW, [rev], Adjustment.SPLIT)
    assert adj["close"].iloc[0] == pytest.approx(400.0)
    assert adj["volume"].iloc[0] == pytest.approx(250.0)
