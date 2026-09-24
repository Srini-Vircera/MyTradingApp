from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.data.bars import Adjustment, make_bars
from adaptive_quant.quant.data.corporate_actions import (
    ActionType,
    CorporateAction,
    apply_adjustment,
    dedupe_actions,
)

BARS = make_bars(
    [datetime(2024, 1, d, 21, tzinfo=UTC) for d in (2, 3, 4)],
    open=[10.0, 10.0, 10.0],
    high=[11.0, 11.0, 11.0],
    low=[9.0, 9.0, 9.0],
    close=[10.0, 10.0, 10.0],
    volume=[1.0, 1.0, 1.0],
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": ActionType.SPLIT, "ratio": 0},
        {"type": ActionType.SPLIT, "ratio": 1},
        {"type": ActionType.SPLIT, "ratio": 2, "amount": 1},
        {"type": ActionType.CASH_DIVIDEND, "amount": -1},
        {"type": ActionType.CASH_DIVIDEND, "amount": 1, "ratio": 2},
        {"type": ActionType.CASH_DIVIDEND},
    ],
)
def test_invalid_actions_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CorporateAction(symbol="X", ex_date=date(2024, 1, 3), **kwargs)


def test_dedupe_across_sources() -> None:
    a = CorporateAction(
        symbol="X", ex_date=date(2024, 1, 3), type=ActionType.SPLIT, ratio=2, source="alpaca"
    )
    b = CorporateAction(
        symbol="X", ex_date=date(2024, 1, 3), type=ActionType.SPLIT, ratio=2, source="polygon"
    )
    assert len(dedupe_actions([a, b])) == 1


def test_actions_outside_range_are_ignored() -> None:
    early = CorporateAction(symbol="X", ex_date=date(2023, 1, 3), type=ActionType.SPLIT, ratio=2)
    late = CorporateAction(symbol="X", ex_date=date(2025, 1, 3), type=ActionType.SPLIT, ratio=2)
    same_day_as_first = CorporateAction(
        symbol="X", ex_date=date(2024, 1, 2), type=ActionType.SPLIT, ratio=2
    )
    out = apply_adjustment(BARS, [early, late, same_day_as_first], Adjustment.SPLIT)
    assert out["close"].tolist() == [10.0, 10.0, 10.0]


def test_dividend_larger_than_price_is_an_error() -> None:
    div = CorporateAction(
        symbol="X", ex_date=date(2024, 1, 3), type=ActionType.CASH_DIVIDEND, amount=10.0
    )
    with pytest.raises(DataQualityError, match="not smaller"):
        apply_adjustment(BARS, [div], Adjustment.ALL)


def test_split_mode_ignores_dividends() -> None:
    div = CorporateAction(
        symbol="X", ex_date=date(2024, 1, 3), type=ActionType.CASH_DIVIDEND, amount=1.0
    )
    assert apply_adjustment(BARS, [div], Adjustment.SPLIT)["close"].tolist() == [10.0, 10.0, 10.0]
