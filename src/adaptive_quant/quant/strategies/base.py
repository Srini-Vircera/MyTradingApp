"""The strategy contract.

Subclasses declare *what* they need (indicator specs, parameters) and *how*
to score (``evaluate``). The base class owns everything safety-relevant, so
no strategy can bypass it:

* data comes only from a point-in-time :class:`MarketDataView`;
* insufficient history raises :class:`InsufficientHistoryError` - no signal is
  ever produced from a partially warmed-up indicator;
* indicator values that are undefined (NaN) raise :class:`StrategyError`;
* the evaluation is checked (finite, score in [-1, 1], confidence in [0, 1],
  exposure inside the strategy's own limits, long-only / short-only rules);
* ``data_timestamp`` is the last bar actually used, so the signal model's own
  look-ahead guard (``data_timestamp <= timestamp``) applies.
"""

from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

import pandas as pd

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.core.errors import InsufficientHistoryError, MissingDataError, StrategyError
from adaptive_quant.core.models import StrategySignal, direction_for_score
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.indicators.engine import IndicatorEngine
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.params import ParamSpec, ParamValue, resolve_params
from adaptive_quant.quant.strategies.scoring import exposure_for

_TOL = 1e-9

COMMON_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("signal_symbol", str, "QQQ", description="symbol whose bars drive the signal"),
    ParamSpec(
        "max_long_exposure",
        float,
        1.0,
        0.0,
        3.0,
        description="net underlying exposure suggested at score +1 (risk engine caps it)",
    ),
    ParamSpec(
        "max_short_exposure",
        float,
        0.0,
        0.0,
        3.0,
        description="net *inverse* exposure suggested at score -1 (0 = reduce to cash only)",
    ),
)


@dataclass(frozen=True)
class OptionalData:
    """Latest indicator values for an optional symbol (``None`` if unavailable)."""

    symbol: str
    values: Mapping[str, float] | None

    @property
    def available(self) -> bool:
        return self.values is not None


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy may look at - all of it point-in-time."""

    bars: pd.DataFrame  # visible bars of the signal symbol (timestamp <= as_of)
    frame: pd.DataFrame  # indicator history on those bars (same index)
    values: Mapping[str, float]  # latest indicator values (all defined)
    optional: Mapping[str, OptionalData] = field(default_factory=dict)

    @property
    def close(self) -> float:
        return float(self.bars["close"].iloc[-1])


@dataclass(frozen=True)
class Evaluation:
    score: float  # normalized, [-1, 1]
    reason: str
    confidence: float | None = None  # defaults to |score|
    raw_score: float | None = None  # the un-normalized quantity; defaults to score
    exposure: float | None = None  # defaults to exposure_for(score, limits)
    extra: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PrecomputedIndicators:
    """Full-history bars and indicator frames for one strategy (backtest path)."""

    strategy_id: str
    frames: Mapping[str, tuple[pd.DataFrame, pd.DataFrame]]


def _visible(
    pair: tuple[pd.DataFrame, pd.DataFrame], cut: pd.Timestamp
) -> tuple[pd.DataFrame, pd.DataFrame]:
    bars, frame = pair
    n = int(bars.index.searchsorted(cut, side="right"))
    return bars.iloc[:n], frame.iloc[:n]


class Strategy(ABC):
    implementation: ClassVar[str]
    family: ClassVar[StrategyFamily]
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str]
    param_specs: ClassVar[tuple[ParamSpec, ...]] = ()
    can_long: ClassVar[bool] = True  # False: score must be <= 0 (bearish-only)
    can_short: ClassVar[bool] = True  # False: suggested exposure never negative

    def __init__(
        self, strategy_id: str | None = None, params: Mapping[str, object] | None = None
    ) -> None:
        self.strategy_id = strategy_id or self.implementation
        try:
            self.params = resolve_params(self.all_param_specs(), params)
            self.validate_params()
        except ValueError as exc:
            raise StrategyError(f"{self.strategy_id}: invalid parameters: {exc}") from exc
        if not self.can_short and self.p_float("max_short_exposure") > 0:
            raise StrategyError(f"{self.strategy_id}: this strategy never shorts")
        if not self.can_long and self.p_float("max_long_exposure") > 0:
            raise StrategyError(f"{self.strategy_id}: this strategy is bearish-only")
        self._engine = IndicatorEngine(self.indicators())

    # ------------------------------------------------------------ declaration
    @classmethod
    def all_param_specs(cls) -> tuple[ParamSpec, ...]:
        overrides = {s.name for s in cls.param_specs}
        common = tuple(s for s in COMMON_PARAMS if s.name not in overrides)
        return common + cls.param_specs

    @abstractmethod
    def indicators(self) -> list[IndicatorSpec]:
        """Indicator specs computed on the signal symbol (and optional symbols)."""

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        """Score the latest point-in-time data."""

    def validate_params(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Cross-parameter checks (raise ``ValueError``)."""

    @property
    def optional_symbols(self) -> tuple[str, ...]:
        return ()

    # ------------------------------------------------------------ identity
    @property
    def signal_symbol(self) -> str:
        return self.p_str("signal_symbol")

    @property
    def version_id(self) -> str:
        """``implementation@version#paramhash`` - changes whenever code version or params do."""
        payload = json.dumps(dict(self.params), sort_keys=True)
        digest = hashlib.sha256(f"{self.implementation}|{self.version}|{payload}".encode())
        return f"{self.implementation}@{self.version}#{digest.hexdigest()[:10]}"

    @property
    def warmup_bars(self) -> int:
        """Bars of history required before a signal can be produced."""
        return self._engine.warmup + 1 + self.extra_history_bars()

    def extra_history_bars(self) -> int:
        """Rows of *indicator* history ``evaluate`` reads beyond the latest row."""
        return 0

    # ------------------------------------------------------------ parameters
    def p_int(self, name: str) -> int:
        v = self.params[name]
        if not isinstance(v, int) or isinstance(v, bool):
            raise TypeError(name)
        return v

    def p_float(self, name: str) -> float:
        v = self.params[name]
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise TypeError(name)
        return float(v)

    def p_str(self, name: str) -> str:
        return str(self.params[name])

    def p_bool(self, name: str) -> bool:
        return bool(self.params[name])

    # ------------------------------------------------------------ evaluation
    def generate_signal(self, view: MarketDataView) -> StrategySignal:
        """Produce a signal from data known at ``view.as_of`` (live / research path).

        Indicators are recomputed on the visible bars only.
        """
        bars = view.bars(self.signal_symbol)
        self._require_history(len(bars), view.as_of)
        frame = self._engine.compute(bars)
        optional: dict[str, tuple[pd.DataFrame, pd.DataFrame] | None] = {}
        for sym in self.optional_symbols:
            if sym in view.symbols and view.has(sym):
                obars = view.bars(sym)
                optional[sym] = (obars, self._engine.compute(obars))
            else:
                optional[sym] = None
        return self._signal(bars, frame, optional, view.as_of)

    def precompute(self, frames: Mapping[str, pd.DataFrame]) -> PrecomputedIndicators:
        """Compute indicators once over whole histories (backtest path).

        Valid because every indicator is causal (value at t uses rows <= t; proved
        by the Milestone 3 look-ahead tests). :meth:`signal_at` then slices to
        ``timestamp <= as_of``, so no row after ``as_of`` is ever *read*.
        """
        out: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
        for sym in (self.signal_symbol, *self.optional_symbols):
            if sym in frames:
                bars = frames[sym]
                out[sym] = (bars, self._engine.compute(bars))
        return PrecomputedIndicators(self.strategy_id, out)

    def signal_at(self, pre: PrecomputedIndicators, as_of: datetime) -> StrategySignal:
        """Identical result to ``generate_signal(MarketDataView(frames, as_of))``."""
        if pre.strategy_id != self.strategy_id:
            raise StrategyError("precomputed indicators belong to another strategy")
        cut = pd.Timestamp(ensure_utc(as_of))
        if self.signal_symbol not in pre.frames:
            raise MissingDataError(f"no market data loaded for {self.signal_symbol}")
        bars, frame = _visible(pre.frames[self.signal_symbol], cut)
        self._require_history(len(bars), cut.to_pydatetime())
        optional: dict[str, tuple[pd.DataFrame, pd.DataFrame] | None] = {}
        for sym in self.optional_symbols:
            optional[sym] = _visible(pre.frames[sym], cut) if sym in pre.frames else None
        return self._signal(bars, frame, optional, cut.to_pydatetime())

    # ------------------------------------------------------------ shared core
    def _require_history(self, rows: int, as_of: datetime) -> None:
        if rows < self.warmup_bars:
            raise InsufficientHistoryError(
                f"{self.strategy_id}: {rows} bars of {self.signal_symbol} known as of "
                f"{as_of:%Y-%m-%d %H:%M}Z; needs {self.warmup_bars}",
                hint="load more history before this date",
            )

    def _signal(
        self,
        bars: pd.DataFrame,
        frame: pd.DataFrame,
        optional_frames: Mapping[str, tuple[pd.DataFrame, pd.DataFrame] | None],
        as_of: datetime,
    ) -> StrategySignal:
        values = self._latest(frame, self.signal_symbol)
        optional = {sym: self._optional(sym, pair) for sym, pair in optional_frames.items()}
        ctx = StrategyContext(bars=bars, frame=frame, values=values, optional=optional)
        try:
            ev = self.evaluate(ctx)
        except (ValueError, ZeroDivisionError, KeyError) as exc:
            raise StrategyError(f"{self.strategy_id}: evaluation failed: {exc}") from exc
        return self._to_signal(ev, as_of, bars, values)

    def _latest(self, frame: pd.DataFrame, symbol: str) -> dict[str, float]:
        if len(frame) == 0:  # (a frame with rows but no indicator columns is fine)
            raise InsufficientHistoryError(f"{self.strategy_id}: no {symbol} bars known")
        last = frame.iloc[-1]
        undefined = [str(k) for k, v in last.items() if not math.isfinite(float(v))]
        if undefined:
            raise StrategyError(
                f"{self.strategy_id}: indicators undefined for {symbol} "
                f"at the latest bar: {undefined}"
            )
        return {str(k): float(v) for k, v in last.items()}

    def _optional(
        self, symbol: str, pair: tuple[pd.DataFrame, pd.DataFrame] | None
    ) -> OptionalData:
        """Optional data is used only if present *and* fully warmed up; otherwise None."""
        if pair is None or len(pair[0]) < self.warmup_bars:
            return OptionalData(symbol, None)
        try:
            return OptionalData(symbol, self._latest(pair[1], symbol))
        except (StrategyError, MissingDataError):
            return OptionalData(symbol, None)

    def _to_signal(
        self,
        ev: Evaluation,
        as_of: datetime,
        bars: pd.DataFrame,
        values: Mapping[str, float],
    ) -> StrategySignal:
        score = ev.score
        confidence = abs(score) if ev.confidence is None else ev.confidence
        raw = score if ev.raw_score is None else ev.raw_score
        long_max, short_max = self.p_float("max_long_exposure"), self.p_float("max_short_exposure")
        exposure = exposure_for(score, long_max, short_max) if ev.exposure is None else ev.exposure
        numbers = {"score": score, "confidence": confidence, "raw_score": raw, "exposure": exposure}
        bad = [k for k, v in numbers.items() if not math.isfinite(v)]
        if bad:
            raise StrategyError(f"{self.strategy_id}: non-finite {bad}")
        if not -1.0 - _TOL <= score <= 1.0 + _TOL:
            raise StrategyError(f"{self.strategy_id}: score {score} outside [-1, 1]")
        if not 0.0 - _TOL <= confidence <= 1.0 + _TOL:
            raise StrategyError(f"{self.strategy_id}: confidence {confidence} outside [0, 1]")
        if not -short_max - _TOL <= exposure <= long_max + _TOL:
            raise StrategyError(
                f"{self.strategy_id}: exposure {exposure} outside [-{short_max}, {long_max}]"
            )
        if not self.can_long and score > _TOL:
            raise StrategyError(f"{self.strategy_id}: bearish-only strategy produced {score}")
        if not ev.reason.strip():
            raise StrategyError(f"{self.strategy_id}: empty reason")
        score = max(-1.0, min(1.0, score))
        indicator_values: dict[str, float | None] = {k: round(v, 10) for k, v in values.items()}
        indicator_values.update({k: round(float(v), 10) for k, v in ev.extra.items()})
        indicator_values["close"] = round(float(bars["close"].iloc[-1]), 10)
        return StrategySignal(
            strategy_name=self.strategy_id,
            strategy_version=self.version_id,
            timestamp=as_of,
            data_timestamp=bars.index[-1].to_pydatetime(),
            direction=direction_for_score(score),
            raw_score=raw,
            normalized_score=score,
            confidence=max(0.0, min(1.0, confidence)),
            suggested_exposure=exposure,
            reason=ev.reason.strip(),
            indicator_values=indicator_values,
        )

    def describe(self) -> dict[str, ParamValue]:
        return dict(self.params)
