"""Backtest orchestration shared by ``aq backtest run`` and control-plane jobs.

Uses the one backtest engine (``quant/backtest``) with its look-ahead checks,
execution timing, cost model, participation caps, cash buffer, M7 risk engine,
benchmarks and accounting identity. Research only: nothing here can place an
order. Results are hypothetical.

``summarize`` turns a finished run into a bounded JSON document (metrics per
segment, benchmark comparison, a downsampled equity/drawdown curve) that the
worker stores in PostgreSQL so the API can show it without the worker volume.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import ValidationError

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.schema import BacktestConfig, Settings
from adaptive_quant.core.clock import MARKET_TZ, Clock
from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.errors import ConfigurationError, DataQualityError
from adaptive_quant.quant.analytics.report import write_report
from adaptive_quant.quant.backtest.data import BacktestData, load_backtest_data
from adaptive_quant.quant.backtest.runner import AnalysedBacktest, risk_warmup_bars, run_backtest
from adaptive_quant.quant.data.calendar import TradingCalendar, nyse_calendar
from adaptive_quant.quant.data.factory import build_store
from adaptive_quant.quant.strategies.base import Strategy
from adaptive_quant.quant.strategies.catalog import StrategyCatalog

Progress = Callable[[str, float | None], None]
TRADEABLE = ("QQQ", "TQQQ", "SQQQ")
UNDERLYING = "QQQ"
LEVERAGED = ("TQQQ", "SQQQ")
HYPOTHETICAL = (
    "HYPOTHETICAL BACKTEST: simulated fills and costs on historical data. It does not show "
    "that a strategy is profitable and does not predict future returns."
)
MAX_CURVE_POINTS = 1500
#: the cost assumptions an operator may vary per run (validated by ``CostConfig``)
COST_FIELDS = (
    "commission_per_share",
    "commission_per_order",
    "commission_minimum",
    "half_spread_bps",
    "slippage_bps",
    "impact_coefficient_bps",
    "max_participation",
)


def _noop(_msg: str, _frac: float | None) -> None:
    return None


@dataclass
class BacktestRequest:
    strategies: list[str]
    source: str | None = None
    start: date | None = None
    end: date | None = None
    initial_capital: float | None = None
    execution: str | None = None
    execution_delay_bars: int | None = None
    use_synthetic_history: bool | None = None
    costs: dict[str, Any] = field(default_factory=dict)
    out_dir: Path | None = None


@dataclass
class BacktestOutcome:
    request: BacktestRequest
    analysed: AnalysedBacktest
    report: Path
    start: date
    end: date
    source: str
    config_version: str
    notes: list[str]
    unpriced: list[str]


def apply_overrides(loaded: LoadedConfig, req: BacktestRequest) -> tuple[LoadedConfig, list[str]]:
    """Per-run backtest assumptions, validated by the configuration schema."""
    bt = loaded.settings.backtest
    update: dict[str, Any] = {}
    if req.execution is not None:
        update["execution"] = req.execution
    if req.execution_delay_bars is not None:
        update["execution_delay_bars"] = req.execution_delay_bars
    if req.use_synthetic_history is not None:
        update["use_synthetic_history"] = req.use_synthetic_history
    if req.initial_capital is not None:
        update["initial_capital"] = req.initial_capital
    unknown = set(req.costs) - set(COST_FIELDS)
    if unknown:
        raise ConfigurationError(f"unknown cost assumption(s): {sorted(unknown)}")
    if req.costs:
        update["costs"] = {**bt.costs.model_dump(), **req.costs}
    if not update:
        return loaded, []
    try:
        new_bt = BacktestConfig.model_validate({**bt.model_dump(), **update})
    except ValidationError as exc:
        msgs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigurationError(f"invalid backtest assumptions: {msgs}") from None
    settings = loaded.settings.model_copy(update={"backtest": new_bt})
    shown = {k: v for k, v in update.items() if k != "costs"} | {
        f"costs.{k}": v for k, v in req.costs.items()
    }
    note = f"per-run overrides of config {loaded.config_version}: " + ", ".join(
        f"{k}={v}" for k, v in shown.items()
    )
    return dataclasses.replace(loaded, settings=settings), [note]


def leveraged_optional(settings: Settings, strategies: Sequence[Strategy]) -> bool:
    """True when the strategies can provably never hold TQQQ or SQQQ.

    Long-only strategies capped at 1x QQQ exposure, an allocation mode that uses
    QQQ for exposure up to 1x, and volatility targeting that can only reduce
    exposure. The engine enforces this at run time as well: any non-zero weight
    on an instrument without data aborts the backtest.
    """
    a = settings.backtest.allocation
    if a.long_mode == "tqqq_only":
        return False
    for s in strategies:
        if s.p_float("max_long_exposure") > 1.0 or s.p_float("max_short_exposure") > 0.0:
            return False
    vt = settings.risk.volatility_target
    return not (a.apply_risk_limits and a.risk_engine and vt.enabled and vt.max_scale > 1.0)


def resolve_strategies(settings: Settings, ids: Sequence[str]) -> list[Strategy]:
    if not ids:
        raise ConfigurationError("choose at least one strategy")
    catalog = StrategyCatalog.from_config(settings.strategies)
    eligible = {s.strategy_id: s for s in catalog.eligible(TradingMode.BACKTEST)}
    unknown = [sid for sid in ids if sid not in eligible]
    if unknown:
        raise ConfigurationError(
            f"not available for backtesting: {unknown}",
            hint="use strategies from the catalogue (disabled strategies cannot run)",
        )
    return [eligible[sid] for sid in dict.fromkeys(ids)]


def default_range(
    data: BacktestData,
    strategies: Sequence[Strategy],
    calendar: TradingCalendar,
    start: date | None,
    end: date | None,
    min_history_bars: int = 0,
) -> tuple[date, date]:
    tradeable = [pd.DatetimeIndex(data.frames[s].index) for s in TRADEABLE if s in data.frames]
    if not tradeable:
        raise DataQualityError("no tradeable instrument has price data")
    # the first decision needs a prior known close of every instrument (sizing prices)
    common_first = max(ix[1] for ix in tradeable)
    common_last = min(ix[-1] for ix in tradeable)
    first_ready = common_first
    for s in strategies:
        idx = pd.DatetimeIndex(data.frames[s.signal_symbol].index)
        if len(idx) <= s.warmup_bars:
            raise DataQualityError(
                f"{s.strategy_id} needs {s.warmup_bars} bars; only {len(idx)} stored"
            )
        first_ready = max(first_ready, idx[s.warmup_bars])  # warm-up complete *before* this session
    if min_history_bars:
        underlying = pd.DatetimeIndex(data.frames[UNDERLYING].index)
        if len(underlying) <= min_history_bars:
            raise DataQualityError(
                f"the risk engine needs {min_history_bars} QQQ bars; only {len(underlying)} stored"
            )
        first_ready = max(first_ready, underlying[min_history_bars])
    auto_start = first_ready.tz_convert(MARKET_TZ).date()
    auto_end = common_last.tz_convert(MARKET_TZ).date()
    s_, e_ = start or auto_start, end or auto_end
    if s_ >= e_:
        raise DataQualityError(f"empty backtest range {s_} .. {e_}")
    if not calendar.is_session(s_):
        s_ = calendar.next_session(s_).date
    return s_, e_


def load_data(
    loaded: LoadedConfig, clock: Clock, source: str, strategies: Sequence[Strategy]
) -> tuple[BacktestData, list[str]]:
    """Price histories for a run; TQQQ/SQQQ may be absent only when provably unused."""
    s = loaded.settings
    optional_lev = leveraged_optional(s, strategies)
    must = [UNDERLYING] if optional_lev else list(TRADEABLE)
    required = sorted({*must, *(st.signal_symbol for st in strategies)})
    optional = sorted(
        {*s.universe.benchmarks, *(o for st in strategies for o in st.optional_symbols)}
        | (set(LEVERAGED) if optional_lev else set())
    )
    data = load_backtest_data(
        build_store(loaded, clock),
        source,
        required,
        optional,
        use_synthetic=s.backtest.use_synthetic_history,
        synthetic_symbols=s.data.synthetic.products.keys(),
    )
    unpriced = [sym for sym in LEVERAGED if sym not in data.frames]
    return data, unpriced


def run(
    loaded: LoadedConfig,
    clock: Clock,
    req: BacktestRequest,
    *,
    calendar: TradingCalendar | None = None,
    progress: Progress = _noop,
) -> BacktestOutcome:
    loaded, notes = apply_overrides(loaded, req)
    settings = loaded.settings
    strategies = resolve_strategies(settings, req.strategies)
    source = req.source or settings.data.primary_provider
    progress(f"loading {source} price history", 0.05)
    data, unpriced = load_data(loaded, clock, source, strategies)
    if unpriced:
        notes.append(
            f"no {'/'.join(unpriced)} price data loaded: the selected strategies are long-only "
            "and capped at 1x QQQ, so they can never hold these instruments (the engine "
            "aborts the run if an allocation ever would)"
        )
    calendar = calendar or nyse_calendar()
    start, end = default_range(
        data, strategies, calendar, req.start, req.end, risk_warmup_bars(settings)
    )
    progress(f"simulating {start} .. {end}", 0.2)
    analysed = run_backtest(
        loaded, strategies, data, calendar, start, end, unpriced=frozenset(unpriced)
    )
    analysed.notes[:0] = notes
    progress("writing the report", 0.9)
    stamp = f"{clock.now().astimezone(MARKET_TZ):%Y%m%d-%H%M%S}"
    name = re.sub(r"[^a-z0-9_+-]", "", "+".join(req.strategies))[:80]
    out = req.out_dir or loaded.resolve_path(settings.backtest.report_dir) / f"{stamp}-{name}"
    report = write_report(analysed, out, f"Backtest: {' + '.join(req.strategies)}")
    progress("backtest finished", 1.0)
    return BacktestOutcome(
        request=req,
        analysed=analysed,
        report=report,
        start=start,
        end=end,
        source=source,
        config_version=loaded.config_version,
        notes=list(analysed.notes),
        unpriced=unpriced,
    )


# ================================================================== summaries
def _num(v: Any) -> float | int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _curve(series: pd.Series[float], points: int = MAX_CURVE_POINTS) -> pd.Series[float]:
    if len(series) <= points:
        return series
    step = math.ceil(len(series) / points)
    positions = list(range(0, len(series), step))
    if positions[-1] != len(series) - 1:
        positions.append(len(series) - 1)  # always keep the final value
    return series.take(positions)


def summarize(outcome: BacktestOutcome) -> dict[str, Any]:
    """A bounded, JSON-ready summary for PostgreSQL (no paths to secrets, no raw data)."""
    a = outcome.analysed
    daily = a.result.daily
    equity = daily["equity"].astype(float)
    drawdown = equity / equity.cummax() - 1.0
    idx = _curve(equity).index
    curve: list[dict[str, Any]] = []
    bench_eq = {b.name: b.equity for b in a.benchmarks}
    for ts in idx:
        row: dict[str, Any] = {
            "date": f"{pd.Timestamp(ts).tz_convert(MARKET_TZ):%Y-%m-%d}",
            "equity": _num(equity.loc[ts]),
            "drawdown": _num(drawdown.loc[ts]),
            "synthetic": bool(daily["synthetic"].loc[ts]),
        }
        for name, series in bench_eq.items():
            row[f"bench:{name}"] = _num(series.get(ts))
        curve.append(row)
    metrics = {
        seg: {series: {k: _num(v) for k, v in vals.items()} for series, vals in by_series.items()}
        for seg, by_series in a.metrics.items()
    }
    seg_info = {
        seg: {
            "sessions": len(ix),
            "first": f"{ix[0].tz_convert(MARKET_TZ):%Y-%m-%d}" if len(ix) else None,
            "last": f"{ix[-1].tz_convert(MARKET_TZ):%Y-%m-%d}" if len(ix) else None,
        }
        for seg, ix in a.segments.items()
    }
    strategy = metrics.get("all", {}).get("Strategy", {})
    portfolio = a.result.portfolio
    return {
        "disclaimer": HYPOTHETICAL,
        "strategies": outcome.request.strategies,
        "strategy_versions": dict(a.result.strategy_versions),
        "source": outcome.source,
        "period": {"start": outcome.start.isoformat(), "end": outcome.end.isoformat()},
        "config_version": outcome.config_version,
        "initial_capital": _num(portfolio.initial_cash),
        "ending_equity": _num(equity.iloc[-1]),
        "headline": {
            k: strategy.get(k)
            for k in (
                "total_return",
                "cagr",
                "volatility",
                "sharpe",
                "sortino",
                "max_drawdown",
                "max_drawdown_duration_sessions",
                "calmar",
                "annual_turnover",
                "average_exposure",
                "trades",
            )
        },
        "fills": len(portfolio.fills),
        "cancelled_or_trimmed_orders": len(a.result.cancelled),
        "round_trips": a.trades.count,
        "metrics": metrics,
        "segments": seg_info,
        "has_synthetic": "synthetic" in a.segments or bool(daily["synthetic"].any()),
        "unpriced_instruments": outcome.unpriced,
        "metadata": dict(a.metadata),
        "notes": list(a.notes),
        "curve": curve,
    }
