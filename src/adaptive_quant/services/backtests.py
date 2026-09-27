"""Backtest orchestration shared by ``aq backtest run`` and control-plane jobs.

Uses the one backtest engine (``quant/backtest``) with its look-ahead checks,
execution timing, cost model, participation caps, cash buffer, M7 risk engine,
benchmarks and accounting identity. Research only: nothing here can place an
order. Results are hypothetical.

``summarize`` turns a finished run into a bounded JSON document (metrics per
segment, benchmark comparison, a downsampled equity/drawdown curve) that the
worker stores in PostgreSQL so the API can show it without the worker volume.

Run modes when several strategies are selected (never blurred):

* ``ensemble`` (the default and the long-standing behaviour): ONE backtest whose
  signals are combined by the configured ensemble -> policy -> risk engine (or,
  with the risk engine off, averaged) into one portfolio.
* ``independent``: one backtest PER strategy, all on identical data, date range
  (the latest warm-up of all selected strategies decides the common start),
  starting capital, execution timing, costs and risk settings - a side-by-side
  comparison, not a portfolio.

Per-run strategy parameters (e.g. a 20/100 EMA Golden/Death Cross variant) are
validated by the strategy registry and apply to that run only; they never change
the configured or approved strategy.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
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
from adaptive_quant.quant.strategies.params import ParamValue

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
    #: per-run parameter overrides for selected strategies (validated, never persisted)
    strategy_params: dict[str, dict[str, ParamValue]] = field(default_factory=dict)
    run_mode: str = "ensemble"  # ensemble | independent


RUN_MODES = ("ensemble", "independent")


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
    #: resolved parameters of every strategy in this run
    strategy_params: dict[str, dict[str, ParamValue]] = field(default_factory=dict)
    implementations: dict[str, str] = field(default_factory=dict)


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


def apply_strategy_params(
    loaded: LoadedConfig, req: BacktestRequest
) -> tuple[LoadedConfig, list[str]]:
    """Per-run parameter overrides for selected strategies, validated by the registry."""
    if not req.strategy_params:
        return loaded, []
    stray = sorted(set(req.strategy_params) - set(req.strategies))
    if stray:
        raise ConfigurationError(f"parameters given for strategies not selected: {stray}")
    cfg = loaded.settings.strategies
    known = {e.id for e in cfg.strategies}
    unknown = sorted(set(req.strategy_params) - known)
    if unknown:
        raise ConfigurationError(f"unknown strategies: {unknown}")
    entries = [
        e.model_copy(update={"params": {**e.params, **req.strategy_params[e.id]}})
        if e.id in req.strategy_params
        else e
        for e in cfg.strategies
    ]
    new_cfg = cfg.model_copy(update={"strategies": entries})
    StrategyCatalog.from_config(new_cfg)  # fails closed with a readable ConfigurationError
    settings = loaded.settings.model_copy(update={"strategies": new_cfg})
    shown = "; ".join(
        f"{sid}: " + ", ".join(f"{k}={v}" for k, v in sorted(p.items()))
        for sid, p in sorted(req.strategy_params.items())
    )
    return dataclasses.replace(loaded, settings=settings), [
        f"per-run strategy parameters (this run only; the configured strategy is unchanged): "
        f"{shown}"
    ]


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
    notes: list[str] | None = None,
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
    if start is not None and start < auto_start:
        # before this date no selected strategy (or the risk engine) can produce a valid
        # signal: that history is used for warm-up only, never traded
        s_ = auto_start
        if notes is not None:
            notes.append(
                f"requested start {start} is before the warm-up is complete; the backtest "
                f"starts on {auto_start}, the first session on which every selected strategy "
                "(and the risk engine) can produce a valid signal - earlier bars are used "
                "only to compute the indicators"
            )
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
    if req.run_mode == "independent":
        raise ConfigurationError("run() is the ensemble run; use run_independent()")
    loaded, notes = _prepare(loaded, req)
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
        data, strategies, calendar, req.start, req.end, risk_warmup_bars(settings), notes
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
        strategy_params={st.strategy_id: dict(st.params) for st in strategies},
        implementations={st.strategy_id: st.implementation for st in strategies},
    )


def _prepare(loaded: LoadedConfig, req: BacktestRequest) -> tuple[LoadedConfig, list[str]]:
    if req.run_mode not in RUN_MODES:
        raise ConfigurationError(f"run_mode must be one of {list(RUN_MODES)}")
    loaded, notes = apply_overrides(loaded, req)
    loaded, more = apply_strategy_params(loaded, req)
    return loaded, notes + more


def run_independent(
    loaded: LoadedConfig,
    clock: Clock,
    req: BacktestRequest,
    *,
    calendar: TradingCalendar | None = None,
    progress: Progress = _noop,
) -> list[BacktestOutcome]:
    """One backtest per selected strategy on IDENTICAL data, dates and assumptions.

    The common period starts once every selected strategy (and the risk engine) is
    warmed up, so no strategy gets a head start; the same loaded price histories,
    starting capital, execution timing, costs and risk settings are used for all.
    """
    loaded, notes = _prepare(loaded, req)
    settings = loaded.settings
    strategies = resolve_strategies(settings, req.strategies)
    source = req.source or settings.data.primary_provider
    progress(f"loading {source} price history", 0.05)
    data, unpriced = load_data(loaded, clock, source, strategies)
    if unpriced:
        notes.append(
            f"no {'/'.join(unpriced)} price data loaded: every selected strategy is long-only "
            "and capped at 1x QQQ (the engine aborts the run if an allocation ever would hold "
            "them)"
        )
    calendar = calendar or nyse_calendar()
    start, end = default_range(
        data, strategies, calendar, req.start, req.end, risk_warmup_bars(settings), notes
    )
    notes.append(
        f"independent comparison: {len(strategies)} separate backtests over the identical "
        f"period {start} .. {end} (the latest warm-up of all selected strategies), with the "
        "same data, starting capital, execution, costs and risk settings; this is not an "
        "ensemble"
    )
    stamp = f"{clock.now().astimezone(MARKET_TZ):%Y%m%d-%H%M%S}"
    base = loaded.resolve_path(settings.backtest.report_dir)
    out: list[BacktestOutcome] = []
    for n, st in enumerate(strategies):
        progress(
            f"simulating {st.strategy_id} ({n + 1}/{len(strategies)})",
            0.1 + 0.8 * n / len(strategies),
        )
        analysed = run_backtest(
            loaded, [st], data, calendar, start, end, unpriced=frozenset(unpriced)
        )
        analysed.notes[:0] = notes
        name = re.sub(r"[^a-z0-9_+-]", "", st.strategy_id)[:80]
        target = (req.out_dir / name) if req.out_dir else base / f"{stamp}-compare-{name}"
        report = write_report(
            analysed, target, f"Backtest (independent comparison): {st.strategy_id}"
        )
        single = dataclasses.replace(req, strategies=[st.strategy_id])
        out.append(
            BacktestOutcome(
                request=single,
                analysed=analysed,
                report=report,
                start=start,
                end=end,
                source=source,
                config_version=loaded.config_version,
                notes=list(analysed.notes),
                unpriced=unpriced,
                strategy_params={st.strategy_id: dict(st.params)},
                implementations={st.strategy_id: st.implementation},
            )
        )
    progress("comparison finished", 1.0)
    return out


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


def _day(ts: datetime | pd.Timestamp) -> str:
    return f"{pd.Timestamp(ts).tz_convert(MARKET_TZ):%Y-%m-%d}"


CROSS_TYPES = {1: "golden", -1: "death"}
REGIMES = {1: "bullish", -1: "bearish", 0: "neutral"}


def crossover_details(outcome: BacktestOutcome) -> dict[str, Any]:
    """Golden/Death Cross events, regime periods and the orders they caused.

    Derived from the decisions the engine already records (each signal carries its
    regime / cross-event values), so nothing is stored twice. Dates are the date
    of the bar whose close revealed the cross (``data_timestamp``); orders follow
    the configured execution timing.
    """
    a = outcome.analysed
    fills: dict[int, list[Any]] = {}
    for f in a.result.fills:
        fills.setdefault(f.order_id, []).append(f)
    out: dict[str, Any] = {}
    for sid, impl in outcome.implementations.items():
        if impl != "golden_death_cross":
            continue
        p = outcome.strategy_params.get(sid, {})
        events: list[dict[str, Any]] = []
        periods: list[dict[str, Any]] = []
        first: str | None = None
        for d in a.result.decisions:
            sig = next((x for x in d.signals if x.strategy_name == sid), None)
            if sig is None:
                continue
            iv = sig.indicator_values
            first = first or _day(sig.data_timestamp)
            regime = int(iv.get("regime") or 0)
            if not periods or periods[-1]["regime"] != REGIMES[regime]:
                if periods:
                    periods[-1]["to"] = _day(sig.data_timestamp)
                periods.append({"regime": REGIMES[regime], "from": _day(sig.data_timestamp)})
            event = int(iv.get("cross_event") or 0)
            if event:
                orders = [
                    {
                        "date": _day(f.time),
                        "symbol": f.symbol,
                        "side": f.side.value,
                        "quantity": _num(f.quantity),
                        "price": _num(f.fill_price),
                    }
                    for oid in d.order_ids
                    for f in fills.get(oid, [])
                ]
                events.append(
                    {
                        "date": _day(sig.data_timestamp),
                        "type": CROSS_TYPES[event],
                        "fast_ma": _num(iv.get("fast_ma")),
                        "slow_ma": _num(iv.get("slow_ma")),
                        "decision": _day(d.decision_time),
                        "orders": orders,
                        "refused": d.refused,
                    }
                )
        if periods:
            periods[-1]["to"] = None  # still current at the end of the backtest
        fast, slow, ma = p.get("fast_period"), p.get("slow_period"), p.get("ma_type")
        out[sid] = {
            "fast_period": fast,
            "slow_period": slow,
            "ma_type": ma,
            "bearish_action": p.get("bearish_action"),
            "classic": (fast, slow, ma) == (50, 200, "SMA"),
            "first_signal": first,
            "current_regime": periods[-1]["regime"] if periods else None,
            "golden_cross_dates": [e["date"] for e in events if e["type"] == "golden"],
            "death_cross_dates": [e["date"] for e in events if e["type"] == "death"],
            "events": events,
            "regime_periods": periods,
        }
    return out


def summarize(outcome: BacktestOutcome, curve_points: int = MAX_CURVE_POINTS) -> dict[str, Any]:
    """A bounded, JSON-ready summary for PostgreSQL (no paths to secrets, no raw data)."""
    a = outcome.analysed
    daily = a.result.daily
    equity = daily["equity"].astype(float)
    drawdown = equity / equity.cummax() - 1.0
    idx = _curve(equity, curve_points).index
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
        "mode": outcome.request.run_mode,
        "strategy_params": outcome.strategy_params,
        "first_decision": _day(a.result.decisions[0].decision_time) if a.result.decisions else None,
        "crossover": crossover_details(outcome),
    }


#: points per equity curve in an independent comparison (several curves per result)
COMPARISON_CURVE_POINTS = 400


def summarize_comparison(outcomes: Sequence[BacktestOutcome]) -> dict[str, Any]:
    """Side-by-side summary of an independent comparison (identical period for all)."""
    if not outcomes:
        raise ValueError("no backtests to compare")
    runs = [summarize(o, COMPARISON_CURVE_POINTS) for o in outcomes]
    first, s0 = outcomes[0], runs[0]
    all0 = (s0.get("metrics") or {}).get("all", {})
    benchmarks = [{"series": name, **vals} for name, vals in all0.items() if name != "Strategy"]
    return {
        "mode": "independent",
        "disclaimer": HYPOTHETICAL,
        "strategies": [o.request.strategies[0] for o in outcomes],
        "source": first.source,
        "period": {"start": first.start.isoformat(), "end": first.end.isoformat()},
        "config_version": first.config_version,
        "initial_capital": s0["initial_capital"],
        "ending_equity": None,
        "headline": {},
        "has_synthetic": any(r["has_synthetic"] for r in runs),
        "unpriced_instruments": first.unpriced,
        "notes": list(first.notes),
        "comparison": [
            {
                "strategy": r["strategies"][0],
                "ending_equity": r["ending_equity"],
                **r["headline"],
                "fills": r["fills"],
            }
            for r in runs
        ],
        "benchmarks": benchmarks,
        "runs": runs,
    }
