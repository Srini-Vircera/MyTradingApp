import pytest

from adaptive_quant.core.errors import StrategyError
from adaptive_quant.quant.strategies.params import ParamSpec, parse_int_list, resolve_params
from adaptive_quant.quant.strategies.registry import create, registry

SPECS = (
    ParamSpec("window", int, 20, 2, 100),
    ParamSpec("scale", float, 0.05, 0.001, 1.0),
    ParamSpec("mode", str, "sma", choices=("sma", "ema")),
    ParamSpec("flag", bool, True),
)


def test_defaults_and_coercion() -> None:
    p = resolve_params(SPECS, {"scale": 1})  # int accepted for float
    assert dict(p) == {"window": 20, "scale": 1.0, "mode": "sma", "flag": True}
    assert isinstance(p["scale"], float)
    with pytest.raises(TypeError):
        p["window"] = 5  # type: ignore[index]


@pytest.mark.parametrize(
    ("given", "match"),
    [
        ({"window": 1}, "below the minimum"),
        ({"window": 101}, "above the maximum"),
        ({"window": 20.0}, "integer"),
        ({"window": True}, "integer"),
        ({"scale": "0.1"}, "number"),
        ({"scale": float("nan")}, "finite"),
        ({"mode": "wma"}, "one of"),
        ({"flag": 1}, "true/false"),
        ({"windw": 5}, "unknown parameter"),
    ],
)
def test_invalid_values(given: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        resolve_params(SPECS, given)


def test_all_errors_reported_together() -> None:
    with pytest.raises(ValueError, match="window") as info:
        resolve_params(SPECS, {"window": 0, "mode": "x", "bogus": 1})
    text = str(info.value)
    assert "window" in text and "mode" in text and "bogus" in text


def test_parse_int_list() -> None:
    assert parse_int_list("20, 5,10,5", "h") == (5, 10, 20)
    for bad in ("", "a,b", "0,5", "5,2000"):
        with pytest.raises(ValueError, match="h "):
            parse_int_list(bad, "h")


@pytest.mark.parametrize(
    ("impl", "params", "match"),
    [
        ("it_ma_stack", {"fast": 60, "mid": 50}, "fast < mid < slow"),
        ("st_ema_cross", {"fast": 30, "slow": 30}, "fast < slow"),
        ("mom_time_series", {"window": 21, "skip": 30}, "skip"),
        ("mom_multi_horizon", {"horizons": "5,x"}, "horizons"),
        ("vol_regime", {"low_below": 0.8}, "low_below < elevated_above"),
        ("ltt_sma_distance", {"window": 5}, "below the minimum"),
        ("mr_rsi_dip", {"max_short_exposure": 0.5}, "above the maximum"),
        ("bear_confirmed_trend", {"max_long_exposure": 1.0}, "above the maximum"),
        ("bear_confirmed_trend", {"max_short_exposure": 2.0}, "above the maximum"),
    ],
)
def test_strategy_parameter_validation(impl: str, params: dict[str, object], match: str) -> None:
    with pytest.raises(StrategyError, match=match):
        create(impl, params=params)


def test_every_strategy_documents_itself() -> None:
    for name, cls in registry().items():
        assert cls.implementation == name
        assert cls.description
        assert cls.version
        names = [s.name for s in cls.all_param_specs()]
        assert len(names) == len(set(names)), name
        assert {"signal_symbol", "max_long_exposure", "max_short_exposure"} <= set(names)


def test_version_id_tracks_parameters() -> None:
    a = create("ltt_sma_distance", params={"window": 200})
    b = create("ltt_sma_distance", params={"window": 200})
    c = create("ltt_sma_distance", params={"window": 220})
    assert a.version_id == b.version_id
    assert a.version_id != c.version_id
    assert a.version_id.startswith("ltt_sma_distance@1.0.0#")


def test_unknown_implementation() -> None:
    with pytest.raises(KeyError, match="unknown strategy implementation"):
        create("does_not_exist")
