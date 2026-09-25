from decimal import Decimal

import pytest

from adaptive_quant.quant.backtest.allocation import ExposureAllocator
from tests.unit.backtest.helpers import SETTINGS

INST = SETTINGS.universe.by_symbol


def alloc(e: float, mode: str = "qqq_then_tqqq", limits: bool = False):  # type: ignore[no-untyped-def]
    return ExposureAllocator(INST, mode, SETTINGS.risk if limits else None).allocate(e)


@pytest.mark.parametrize(
    ("e", "qqq", "tqqq", "sqqq"),
    [
        (0.0, "0", "0", "0"),
        (0.6, "0.6", "0", "0"),
        (1.0, "1", "0", "0"),
        (2.0, "0.5", "0.5", "0"),  # 0.5*1 + 0.5*3 = 2.0 with 100% invested
        (3.0, "0", "1", "0"),
        (-0.45, "0", "0", "0.15"),
    ],
)
def test_mapping(e: float, qqq: str, tqqq: str, sqqq: str) -> None:
    a = alloc(e)
    assert (a.weights["QQQ"], a.weights["TQQQ"], a.weights["SQQQ"]) == (
        Decimal(qqq),
        Decimal(tqqq),
        Decimal(sqqq),
    )
    alloc_obj = ExposureAllocator(INST)
    assert alloc_obj.net_exposure(a.weights) == pytest.approx(e)


def test_modes() -> None:
    assert alloc(1.5, "tqqq_only").weights["TQQQ"] == Decimal("0.5")
    assert alloc(1.5, "qqq_only").weights["QQQ"] == Decimal(1)


def test_static_risk_caps_are_applied_and_explained() -> None:
    a = alloc(3.0, limits=True)  # risk.yaml: net cap 2.0, TQQQ <= 66%, leveraged <= 66%
    net = ExposureAllocator(INST).net_exposure(a.weights)
    assert net <= 2.0 + 1e-9
    assert a.weights["TQQQ"] <= Decimal("0.66")
    assert any("capped" in adj for adj in a.adjustments)
    bear = alloc(-3.0, limits=True)  # inverse cap 0.45 -> 15% SQQQ
    assert bear.weights["SQQQ"] == Decimal("0.15")
    assert any("inverse" in adj for adj in bear.adjustments)


def test_weights_never_exceed_one() -> None:
    for e in (-5, -1, 0, 0.3, 1, 1.7, 3, 9):
        a = alloc(float(e), limits=True)
        assert sum(a.weights.values()) <= 1
        assert all(w >= 0 for w in a.weights.values())


def test_requires_known_instruments() -> None:
    with pytest.raises(ValueError, match="needs instruments"):
        ExposureAllocator({"QQQ": INST["QQQ"]})
    with pytest.raises(ValueError, match="long_mode"):
        ExposureAllocator(INST, "yolo")
