from datetime import UTC, date, datetime, timedelta

import pytest

from adaptive_quant.core.errors import DataQualityError, MissingDataError
from adaptive_quant.quant.data.view import MarketDataView
from tests.data_helpers import daily_bars

BARS = daily_bars(date(2024, 1, 2), date(2024, 1, 31))
JAN_10_1545 = datetime(2024, 1, 10, 20, 45, tzinfo=UTC)  # 15:45 ET, before the close


def test_todays_daily_bar_is_invisible_before_the_close() -> None:
    view = MarketDataView({"QQQ": BARS}, JAN_10_1545)
    assert view.data_timestamp("QQQ") == datetime(2024, 1, 9, 21, 0, tzinfo=UTC)
    assert view.bars("QQQ").index.max() < JAN_10_1545


def test_bar_visible_exactly_at_close() -> None:
    view = MarketDataView({"QQQ": BARS}, datetime(2024, 1, 10, 21, 0, tzinfo=UTC))
    assert view.data_timestamp("QQQ") == datetime(2024, 1, 10, 21, 0, tzinfo=UTC)


def test_never_exposes_future_rows_at_any_point() -> None:
    view = MarketDataView({"QQQ": BARS}, BARS.index[0].to_pydatetime())
    for ts in BARS.index:
        t = ts.to_pydatetime() - timedelta(seconds=1)
        assert (view.at(t).bars("QQQ").index <= t).all()


def test_lookback_and_latest() -> None:
    view = MarketDataView({"QQQ": BARS}, JAN_10_1545)
    assert len(view.bars("QQQ", lookback=3)) == 3
    assert view.close("QQQ", lookback=2).index[-1] == view.bars("QQQ").index[-1]
    assert view.latest("QQQ")["close"] == view.bars("QQQ")["close"].iloc[-1]


def test_modifying_returned_frame_does_not_corrupt_history() -> None:
    view = MarketDataView({"QQQ": BARS.copy()}, JAN_10_1545)
    visible = view.bars("QQQ")
    visible.loc[visible.index[0], "close"] = -999.0
    assert view.bars("QQQ")["close"].iloc[0] != -999.0


def test_missing_symbol_and_no_visible_data() -> None:
    view = MarketDataView({"QQQ": BARS}, datetime(2023, 12, 1, tzinfo=UTC))
    assert not view.has("QQQ")
    with pytest.raises(MissingDataError):
        view.latest("QQQ")
    with pytest.raises(MissingDataError):
        view.data_timestamp("QQQ")
    with pytest.raises(MissingDataError, match="no market data loaded"):
        view.bars("TQQQ")
    assert view.symbols == ["QQQ"]


def test_unsorted_input_rejected() -> None:
    with pytest.raises(DataQualityError):
        MarketDataView({"QQQ": BARS.iloc[::-1]}, JAN_10_1545)


def test_naive_as_of_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        MarketDataView({"QQQ": BARS}, datetime(2024, 1, 10))  # noqa: DTZ001
