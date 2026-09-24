"""Indicator configurations exercised by the property tests (every registered kind)."""

from adaptive_quant.quant.indicators.specs import IndicatorSpec

CASES: list[IndicatorSpec] = [
    IndicatorSpec("sma", {"window": 1}),
    IndicatorSpec("sma", {"window": 50}),
    IndicatorSpec("ema", {"window": 5}),
    IndicatorSpec("ema", {"window": 30}),
    IndicatorSpec("distance_from_ma", {"window": 20, "kind": "sma"}),
    IndicatorSpec("distance_from_ma", {"window": 20, "kind": "ema"}),
    IndicatorSpec("rolling_zscore", {"window": 20}),
    IndicatorSpec("trend_slope", {"window": 20}),
    IndicatorSpec("trend_r2", {"window": 20}),
    IndicatorSpec("rate_of_change", {"window": 10}),
    IndicatorSpec("momentum", {"window": 60, "skip": 5}),
    IndicatorSpec("momentum_acceleration", {"window": 20, "lag": 5}),
    IndicatorSpec("rsi", {"window": 14}),
    IndicatorSpec("rsi", {"window": 2}),
    IndicatorSpec("rolling_std", {"window": 20, "ddof": 1}),
    IndicatorSpec("rolling_std", {"window": 20, "ddof": 0}),
    IndicatorSpec("historical_volatility", {"window": 20}),
    IndicatorSpec("true_range"),
    IndicatorSpec("atr", {"window": 14}),
    *(
        IndicatorSpec("bollinger", {"window": 20, "output": o})
        for o in ("middle", "upper", "lower", "width", "percent_b")
    ),
    IndicatorSpec("volatility_percentile", {"vol_window": 10, "lookback": 60}),
    IndicatorSpec("rolling_high", {"window": 20, "include_current": True}),
    IndicatorSpec("rolling_high", {"window": 20, "include_current": False}),
    IndicatorSpec("rolling_low", {"window": 20, "include_current": True}),
    IndicatorSpec("rolling_low", {"window": 20, "include_current": False}),
    IndicatorSpec("drawdown"),
    IndicatorSpec("drawdown", {"window": 60}),
    IndicatorSpec("drawdown_duration"),
    IndicatorSpec("rolling_sharpe", {"window": 63}),
    IndicatorSpec("rolling_sharpe", {"window": 21, "risk_free_annual": 0.03}),
]


def ids(spec: IndicatorSpec) -> str:
    return spec.name
