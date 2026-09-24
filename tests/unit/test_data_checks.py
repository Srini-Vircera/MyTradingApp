from datetime import UTC, date, datetime

import pandas as pd

from adaptive_quant.config.schema import StalenessConfig
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import MissingDataError
from adaptive_quant.quant.data.bars import Frequency
from adaptive_quant.quant.data.validation import BarValidator
from adaptive_quant.trading.safety.data_checks import MarketDataCheck
from adaptive_quant.trading.safety.preflight import PreflightGate, RefusalReason
from tests.data_helpers import calendar, daily_bars

CLOCK = FrozenClock(datetime(2024, 1, 31, 20, 40, tzinfo=UTC))  # 15:40 ET
FRESH = daily_bars(date(2023, 11, 1), date(2024, 1, 30))


def check(frames: dict[str, pd.DataFrame], symbols=("QQQ", "TQQQ")) -> MarketDataCheck:  # type: ignore[no-untyped-def]
    def loader(symbol: str) -> pd.DataFrame:
        if symbol not in frames:
            raise MissingDataError(f"no {symbol}")
        return frames[symbol]

    return MarketDataCheck(
        symbols=symbols,
        frequency=Frequency.DAILY,
        loader=loader,
        validator=BarValidator(calendar()),
        calendar=calendar(),
        staleness=StalenessConfig(),
        clock=CLOCK,
    )


def test_fresh_valid_data_passes() -> None:
    result = check({"QQQ": FRESH, "TQQQ": FRESH}).run()
    assert result.passed, result.detail


def test_missing_symbol_refuses() -> None:
    result = check({"QQQ": FRESH}).run()
    assert result.reason is RefusalReason.MARKET_DATA_MISSING
    assert "TQQQ" in result.detail


def test_stale_data_refuses() -> None:
    old = daily_bars(date(2023, 11, 1), date(2024, 1, 25))
    result = check({"QQQ": FRESH, "TQQQ": old}).run()
    assert result.reason is RefusalReason.MARKET_DATA_STALE
    assert "TQQQ" in result.detail


def test_invalid_data_refuses() -> None:
    bad = FRESH.copy()
    bad.loc[bad.index[5], "close"] = 0.0
    result = check({"QQQ": bad, "TQQQ": FRESH}).run()
    assert result.reason is RefusalReason.MARKET_DATA_INVALID
    assert "non_positive_price" in result.detail


def test_no_symbols_refuses_and_gate_integration() -> None:
    assert not check({}, symbols=()).run().passed
    report = PreflightGate([check({"QQQ": FRESH})], CLOCK).evaluate()
    assert not report.may_reduce_risk  # data problems block everything
