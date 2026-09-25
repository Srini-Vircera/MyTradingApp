"""Declarative indicator specs and the registry.

Strategies (Milestone 4) and configuration refer to indicators as data, e.g.
``IndicatorSpec("sma", {"window": 200})``. A spec is validated when it is
created - unknown kinds, unknown parameters and invalid values fail immediately
(at startup), not in the middle of a trading cycle.

Each registered kind declares its **warm-up**: the number of leading rows that
are NaN on clean input (equivalently, the index of the first valid value).
Strategies use it to know how much history they need; tests verify every
declaration against the actual output.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import pandas as pd

from adaptive_quant.quant.indicators import momentum as mom
from adaptive_quant.quant.indicators import performance as perf
from adaptive_quant.quant.indicators import ranges as rng
from adaptive_quant.quant.indicators import trend as tr
from adaptive_quant.quant.indicators import volatility as vol

ParamValue = int | float | str | bool | None
Params = Mapping[str, ParamValue]
_PRICE_COLUMNS = ("open", "high", "low", "close")
_SOURCES = (*_PRICE_COLUMNS, "volume")


@dataclass(frozen=True)
class IndicatorDef:
    kind: str
    description: str
    defaults: Mapping[str, ParamValue]
    warmup: Callable[[Params], int]
    compute: Callable[[pd.DataFrame, str, Params], pd.Series[float]]
    default_source: str = "close"
    uses_ohlc: bool = False  # ignores ``source`` and reads high/low/close


def _i(p: Params, key: str) -> int:
    v = p[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"parameter {key!r} must be an integer, got {v!r}")
    return v


def _f(p: Params, key: str) -> float:
    v = p[key]
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError(f"parameter {key!r} must be a number, got {v!r}")
    return float(v)


def _b(p: Params, key: str) -> bool:
    v = p[key]
    if not isinstance(v, bool):
        raise ValueError(f"parameter {key!r} must be true/false, got {v!r}")
    return v


def _opt_i(p: Params, key: str) -> int | None:
    return None if p[key] is None else _i(p, key)


_BOLLINGER_OUTPUTS = ("middle", "upper", "lower", "width", "percent_b")


def _bollinger(b: pd.DataFrame, s: str, p: Params) -> pd.Series[float]:
    output = str(p["output"])
    if output not in _BOLLINGER_OUTPUTS:
        raise ValueError(f"bollinger output must be one of {_BOLLINGER_OUTPUTS}")
    return vol.bollinger_bands(b[s], _i(p, "window"), _f(p, "num_std"))[output]


def _defs() -> dict[str, IndicatorDef]:
    d = [
        IndicatorDef(
            "sma",
            "simple moving average",
            {"window": 20},
            lambda p: _i(p, "window") - 1,
            lambda b, s, p: tr.sma(b[s], _i(p, "window")),
        ),
        IndicatorDef(
            "ema",
            "exponential moving average (SMA-seeded)",
            {"window": 20},
            lambda p: _i(p, "window") - 1,
            lambda b, s, p: tr.ema(b[s], _i(p, "window")),
        ),
        IndicatorDef(
            "distance_from_ma",
            "x / MA - 1",
            {"window": 200, "kind": "sma"},
            lambda p: _i(p, "window") - 1,
            lambda b, s, p: tr.distance_from_ma(b[s], _i(p, "window"), str(p["kind"])),
        ),
        IndicatorDef(
            "rolling_zscore",
            "(x - mean) / std over a window",
            {"window": 20},
            lambda p: _i(p, "window") - 1,
            lambda b, s, p: tr.rolling_zscore(b[s], _i(p, "window")),
        ),
        IndicatorDef(
            "trend_slope",
            "annualized OLS slope of log price",
            {"window": 50, "annualize": True},
            lambda p: _i(p, "window") - 1,
            lambda b, s, p: tr.trend_slope(b[s], _i(p, "window"), _b(p, "annualize")),
        ),
        IndicatorDef(
            "trend_r2",
            "R² of the log-price trend fit",
            {"window": 50},
            lambda p: _i(p, "window") - 1,
            lambda b, s, p: tr.trend_r2(b[s], _i(p, "window")),
        ),
        IndicatorDef(
            "rate_of_change",
            "x_t / x_{t-n} - 1",
            {"window": 20},
            lambda p: _i(p, "window"),
            lambda b, s, p: mom.rate_of_change(b[s], _i(p, "window")),
        ),
        IndicatorDef(
            "momentum",
            "log(x_{t-skip} / x_{t-n})",
            {"window": 20, "skip": 0},
            lambda p: _i(p, "window"),
            lambda b, s, p: mom.momentum(b[s], _i(p, "window"), _i(p, "skip")),
        ),
        IndicatorDef(
            "momentum_acceleration",
            "ROC_n(t) - ROC_n(t - lag)",
            {"window": 20, "lag": 10},
            lambda p: _i(p, "window") + _i(p, "lag"),
            lambda b, s, p: mom.momentum_acceleration(b[s], _i(p, "window"), _i(p, "lag")),
        ),
        IndicatorDef(
            "rsi",
            "Wilder RSI (0..100)",
            {"window": 14},
            lambda p: _i(p, "window"),
            lambda b, s, p: mom.rsi(b[s], _i(p, "window")),
        ),
        IndicatorDef(
            "rolling_std",
            "rolling standard deviation",
            {"window": 20, "ddof": 1},
            lambda p: _i(p, "window") - 1,
            lambda b, s, p: vol.rolling_stdev(b[s], _i(p, "window"), _i(p, "ddof")),
        ),
        IndicatorDef(
            "historical_volatility",
            "annualized std of log returns",
            {"window": 20},
            lambda p: _i(p, "window"),
            lambda b, s, p: vol.historical_volatility(b[s], _i(p, "window")),
        ),
        IndicatorDef(
            "true_range",
            "true range",
            {},
            lambda p: 1,
            lambda b, s, p: vol.true_range(b),
            uses_ohlc=True,
        ),
        IndicatorDef(
            "atr",
            "Wilder average true range",
            {"window": 14},
            lambda p: _i(p, "window"),
            lambda b, s, p: vol.atr(b, _i(p, "window")),
            uses_ohlc=True,
        ),
        IndicatorDef(
            "bollinger",
            "Bollinger band component",
            {"window": 20, "num_std": 2.0, "output": "width"},
            lambda p: _i(p, "window") - 1,
            _bollinger,
        ),
        IndicatorDef(
            "volatility_percentile",
            "rank of current vol in its lookback (0..1]",
            {"vol_window": 20, "lookback": 252},
            lambda p: _i(p, "vol_window") + _i(p, "lookback") - 1,
            lambda b, s, p: vol.volatility_percentile(b[s], _i(p, "vol_window"), _i(p, "lookback")),
        ),
        IndicatorDef(
            "rolling_high",
            "highest value of the window",
            {"window": 20, "include_current": True},
            lambda p: _i(p, "window") - (1 if _b(p, "include_current") else 0),
            lambda b, s, p: rng.rolling_high(b[s], _i(p, "window"), _b(p, "include_current")),
            default_source="high",
        ),
        IndicatorDef(
            "rolling_low",
            "lowest value of the window",
            {"window": 20, "include_current": True},
            lambda p: _i(p, "window") - (1 if _b(p, "include_current") else 0),
            lambda b, s, p: rng.rolling_low(b[s], _i(p, "window"), _b(p, "include_current")),
            default_source="low",
        ),
        IndicatorDef(
            "drawdown",
            "x / running peak - 1",
            {"window": None},
            lambda p: 0 if p["window"] is None else _i(p, "window") - 1,
            lambda b, s, p: perf.drawdown(b[s], _opt_i(p, "window")),
        ),
        IndicatorDef(
            "drawdown_duration",
            "bars since the running peak",
            {},
            lambda p: 0,
            lambda b, s, p: perf.drawdown_duration(b[s]),
        ),
        IndicatorDef(
            "rolling_sharpe",
            "annualized Sharpe of simple returns",
            {"window": 63, "risk_free_annual": 0.0},
            lambda p: _i(p, "window"),
            lambda b, s, p: perf.rolling_sharpe(b[s], _i(p, "window"), _f(p, "risk_free_annual")),
        ),
    ]
    return {x.kind: x for x in d}


REGISTRY: Mapping[str, IndicatorDef] = MappingProxyType(_defs())


@dataclass(frozen=True)
class IndicatorSpec:
    """One configured indicator. ``name`` is the column/key it is reported under."""

    kind: str
    params: Mapping[str, ParamValue] = field(default_factory=dict)
    source: str | None = None
    name: str = ""

    def __post_init__(self) -> None:
        definition = REGISTRY.get(self.kind)
        if definition is None:
            raise ValueError(f"unknown indicator kind {self.kind!r}; known: {sorted(REGISTRY)}")
        unknown = set(self.params) - set(definition.defaults)
        if unknown:
            raise ValueError(f"{self.kind}: unknown parameter(s) {sorted(unknown)}")
        resolved = MappingProxyType({**definition.defaults, **self.params})
        object.__setattr__(self, "params", resolved)
        source = self.source or definition.default_source
        if source not in _SOURCES:
            raise ValueError(f"{self.kind}: source must be one of {_SOURCES}")
        object.__setattr__(self, "source", source)
        if not self.name:
            object.__setattr__(self, "name", _default_name(self.kind, resolved, source, definition))
        # validate parameter values now (fail at configuration time)
        definition.warmup(resolved)
        definition.compute(_EMPTY, source, resolved)

    @property
    def definition(self) -> IndicatorDef:
        return REGISTRY[self.kind]

    @property
    def warmup(self) -> int:
        return self.definition.warmup(self.params)

    def compute(self, bars: pd.DataFrame) -> pd.Series[float]:
        out = self.definition.compute(bars, str(self.source), self.params)
        return out.rename(self.name).astype("float64")


def _default_name(kind: str, params: Params, source: str, d: IndicatorDef) -> str:
    parts = [kind]
    parts += [_fmt(v) for v in params.values() if v is not None]
    if not d.uses_ohlc and source != d.default_source:
        parts.append(source)
    return "_".join(parts)


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, float) and v.is_integer():
        return str(int(v)) if abs(v) >= 1 else str(v)
    return str(v)


_EMPTY = pd.DataFrame(
    {c: pd.Series(dtype="float64") for c in (*_PRICE_COLUMNS, "volume")},
    index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
)
