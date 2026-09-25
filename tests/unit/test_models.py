from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from adaptive_quant.core.enums import AssetClass, OrderSide, OrderType, SignalDirection
from adaptive_quant.core.models import (
    Instrument,
    OrderRequest,
    StrategySignal,
    TargetPortfolio,
    direction_for_score,
)

T0 = datetime(2024, 1, 2, 20, 45, tzinfo=UTC)


def signal(**overrides: Any) -> StrategySignal:
    base: dict[str, Any] = {
        "strategy_name": "long_term_trend_sma",
        "strategy_version": "1.0.0",
        "timestamp": T0,
        "data_timestamp": T0 - timedelta(minutes=5),
        "direction": SignalDirection.BULLISH,
        "raw_score": 0.042,
        "normalized_score": 0.6,
        "confidence": 0.7,
        "suggested_exposure": 1.2,
        "reason": "close 4.2% above SMA(200)",
        "indicator_values": {"sma_200": 400.0, "close": 416.8},
    }
    base.update(overrides)
    return StrategySignal(**base)


class TestStrategySignal:
    def test_valid_signal(self) -> None:
        s = signal()
        assert s.direction is SignalDirection.BULLISH

    def test_is_immutable(self) -> None:
        with pytest.raises(ValidationError):
            signal().normalized_score = 0.1  # type: ignore[misc]

    def test_future_data_is_rejected_as_lookahead(self) -> None:
        with pytest.raises(ValidationError, match="look-ahead"):
            signal(data_timestamp=T0 + timedelta(seconds=1))

    @pytest.mark.parametrize("score", [1.01, -1.01])
    def test_score_bounds(self, score: float) -> None:
        with pytest.raises(ValidationError):
            signal(normalized_score=score)

    def test_direction_must_match_score(self) -> None:
        with pytest.raises(ValidationError, match="inconsistent"):
            signal(normalized_score=-0.5)

    def test_non_finite_indicator_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not finite"):
            signal(indicator_values={"x": float("nan")})

    def test_missing_indicator_may_be_none(self) -> None:
        assert signal(indicator_values={"x": None}).indicator_values["x"] is None

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="naive"):
            signal(timestamp=datetime(2024, 1, 2))  # noqa: DTZ001

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            signal(extra_field=1)

    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0.0, SignalDirection.NEUTRAL),
            (0.05, SignalDirection.NEUTRAL),
            (0.051, SignalDirection.BULLISH),
            (-0.051, SignalDirection.BEARISH),
        ],
    )
    def test_direction_mapping(self, score: float, expected: SignalDirection) -> None:
        assert direction_for_score(score) is expected


INSTRUMENTS = {
    "QQQ": Instrument(symbol="QQQ", asset_class=AssetClass.ETF),
    "TQQQ": Instrument(symbol="TQQQ", asset_class=AssetClass.ETF, leverage=3, underlying="QQQ"),
    "SQQQ": Instrument(symbol="SQQQ", asset_class=AssetClass.ETF, leverage=-3, underlying="QQQ"),
}


def target(weights: dict[str, str]) -> TargetPortfolio:
    return TargetPortfolio(
        as_of=T0,
        weights={k: Decimal(v) for k, v in weights.items()},
        decision_id="dec-1",
        config_version="paper-abc",
    )


class TestTargetPortfolio:
    def test_cash_is_the_remainder(self) -> None:
        t = target({"TQQQ": "0.55", "QQQ": "0.10"})
        assert t.cash_weight == Decimal("0.35")

    def test_net_underlying_exposure(self) -> None:
        t = target({"TQQQ": "0.5", "QQQ": "0.1"})
        assert t.net_underlying_exposure(INSTRUMENTS) == pytest.approx(1.6)
        assert target({"SQQQ": "0.1"}).net_underlying_exposure(INSTRUMENTS) == pytest.approx(-0.3)

    def test_negative_weights_rejected(self) -> None:
        with pytest.raises(ValidationError, match="shorting"):
            target({"TQQQ": "-0.1"})

    def test_account_leverage_rejected(self) -> None:
        with pytest.raises(ValidationError, match="> 1"):
            target({"TQQQ": "0.7", "QQQ": "0.4"})

    def test_rounding_tolerance(self) -> None:
        t = target({"TQQQ": "0.3333333", "QQQ": "0.3333333", "SQQQ": "0.3333334"})
        assert t.invested_weight <= Decimal("1.00001")

    def test_explicit_cash_rejected(self) -> None:
        with pytest.raises(ValidationError, match="CASH"):
            target({"CASH": "0.5"})

    def test_unknown_instrument_in_exposure(self) -> None:
        with pytest.raises(KeyError):
            target({"ABC": "0.1"}).net_underlying_exposure(INSTRUMENTS)


class TestInstrument:
    def test_leveraged_requires_underlying(self) -> None:
        with pytest.raises(ValidationError, match="underlying"):
            Instrument(symbol="TQQQ", asset_class=AssetClass.ETF, leverage=3)

    @pytest.mark.parametrize("asset_class", [AssetClass.OPTION, AssetClass.FUTURE])
    def test_options_and_futures_not_in_v1(self, asset_class: AssetClass) -> None:
        with pytest.raises(ValidationError, match="not supported"):
            Instrument(symbol="X", asset_class=asset_class)

    def test_index_is_signal_only(self) -> None:
        with pytest.raises(ValidationError, match="signal-only"):
            Instrument(symbol="NDX", asset_class=AssetClass.INDEX)

    def test_flags(self) -> None:
        assert INSTRUMENTS["SQQQ"].is_inverse and INSTRUMENTS["SQQQ"].is_leveraged
        assert not INSTRUMENTS["QQQ"].is_leveraged


class TestOrderRequest:
    def _req(self, **kw: Any) -> OrderRequest:
        base: dict[str, Any] = {
            "client_order_id": "aq-TQQQ-b0-abc",
            "symbol": "TQQQ",
            "side": OrderSide.BUY,
            "quantity": Decimal(10),
            "risk_increasing": True,
            "decision_id": "dec-1",
        }
        base.update(kw)
        return OrderRequest(**base)

    def test_market_order(self) -> None:
        assert self._req().order_type is OrderType.MARKET

    def test_zero_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._req(quantity=Decimal(0))

    def test_limit_requires_price(self) -> None:
        with pytest.raises(ValidationError, match="limit_price"):
            self._req(order_type=OrderType.LIMIT)

    def test_market_must_not_have_limit(self) -> None:
        with pytest.raises(ValidationError, match="must not"):
            self._req(limit_price=Decimal(50))
