from decimal import Decimal

import pytest

from adaptive_quant.core.money import (
    quantize_price,
    quantize_quantity,
    quantize_weight,
    to_decimal,
)


class TestToDecimal:
    def test_float_has_no_binary_artifacts(self) -> None:
        assert to_decimal(0.1) == Decimal("0.1")
        assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")

    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity"]
    )
    def test_non_finite_rejected(self, value: float | str) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            to_decimal(value)

    def test_bool_rejected(self) -> None:
        with pytest.raises(TypeError):
            to_decimal(True)

    def test_garbage_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot parse"):
            to_decimal("12abc")

    def test_passthrough_types(self) -> None:
        assert to_decimal(Decimal("1.5")) == Decimal("1.5")
        assert to_decimal(3) == Decimal(3)
        assert to_decimal(" 2.25 ") == Decimal("2.25")


class TestQuantize:
    def test_quantity_rounds_toward_zero_never_up(self) -> None:
        assert quantize_quantity("10.999", Decimal(1)) == Decimal(10)
        assert quantize_quantity("-10.999", Decimal(1)) == Decimal(-10)
        assert quantize_quantity("1.2345679", Decimal("0.000001")) == Decimal("1.234567")

    def test_price_uses_bankers_rounding(self) -> None:
        assert quantize_price("10.125") == Decimal("10.12")
        assert quantize_price("10.135") == Decimal("10.14")

    def test_weight(self) -> None:
        assert quantize_weight(1 / 3) == Decimal("0.333333")

    def test_non_positive_quantum_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantum"):
            quantize_quantity(1, Decimal(0))
