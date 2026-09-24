"""High-level backtest runner: configuration + store -> engine -> analysed result.

Research use only. Nothing here can place an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.quant.analytics.metrics import TradeStats, performance_summary, trade_stats
from adaptive_quant.quant.backtest.benchmarks import Benchmark, benchmark_curves
from adaptive_quant.quant.backtest.data import BacktestData
from adaptive_quant.quant.backtest.engine import BacktestEngine, BacktestResult, EngineSettings
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.quant.strategies.base import Strategy

Metrics = dict[str, float | int | None]


@dataclass
class AnalysedBacktest:
    result: BacktestResult
    benchmarks: list[Benchmark]
    segments: dict[str, pd.DatetimeIndex]  # "all", and "real"/"synthetic" when mixed
    metrics: dict[str, dict[str, Metrics]]  # segment -> series name -> metrics
    trades: TradeStats
    metadata: dict[str, str]
    notes: list[str] = field(default_factory=list)

    @property
    def strategy_label(self) -> str:
        return "Strategy"


def run_backtest(
    loaded: LoadedConfig,
    strategies: list[Strategy],
    data: BacktestData,
    calendar: TradingCalendar,
    start: date,
    end: date,
) -> AnalysedBacktest:
    s = loaded.settings
    engine = BacktestEngine(
        frames=data.frames,
        strategies=strategies,
        instruments=s.universe.by_symbol,
        calendar=calendar,
        settings=EngineSettings(
            config=s.backtest,
            rebalance_threshold=s.trading.rebalance_threshold_weight,
            allow_fractional=s.trading.allow_fractional_shares,
            risk_limits=s.risk,
        ),
        synthetic=data.synthetic,
    )
    result = engine.run(start, end)
    notes = []
    if data.missing_optional:
        notes.append(f"optional data unavailable: {', '.join(data.missing_optional)}")
    return analyse(result, data, loaded, notes=notes)


def analyse(
    result: BacktestResult, data: BacktestData, loaded: LoadedConfig, notes: list[str] | None = None
) -> AnalysedBacktest:
    s = loaded.settings
    daily = result.daily
    index = pd.DatetimeIndex(daily.index)
    bench, bench_notes = benchmark_curves(
        data.frames,
        [*s.universe.benchmarks],
        index,
        s.backtest.initial_capital,
        cash_rate=s.backtest.cash_interest_annual,
        synthetic=data.synthetic,
    )
    rts = result.portfolio.round_trips
    trades = trade_stats(
        [float(t.pnl) for t in rts], [t.return_pct for t in rts], [t.holding_days for t in rts]
    )
    synthetic_any = pd.Series(daily["synthetic"].astype(bool), index=index)
    for b in bench:
        synthetic_any = synthetic_any | b.synthetic
    segments = {"all": index}
    if synthetic_any.any() and (~synthetic_any).any():
        segments["synthetic"] = index[synthetic_any.to_numpy()]
        segments["real"] = index[(~synthetic_any).to_numpy()]
    rf = s.backtest.cash_interest_annual
    qqq = next((b.equity for b in bench if b.name.startswith("QQQ ")), None)
    metrics: dict[str, dict[str, Metrics]] = {}
    for seg, idx in segments.items():
        if len(idx) < 2:
            continue
        eq = daily["equity"].reindex(idx)
        metrics[seg] = {
            "Strategy": performance_summary(
                eq,
                rf_annual=rf,
                exposure=daily["gross_exposure"].reindex(idx),
                turnover=daily["turnover"].reindex(idx),
                trades=trades if seg == "all" else None,
                benchmark=None if qqq is None else qqq.reindex(idx),
            )
        }
        for b in bench:
            metrics[seg][b.name] = performance_summary(b.equity.reindex(idx), rf_annual=rf)
    p = result.portfolio
    metadata = {
        "config_version": loaded.config_version,
        "execution": result.execution.value,
        "execution_delay_bars": str(s.backtest.execution_delay_bars),
        "strategies": ", ".join(f"{k} ({v})" for k, v in result.strategy_versions.items()),
        "allocation": f"{s.backtest.allocation.long_mode}; static risk caps "
        f"{'on' if s.backtest.allocation.apply_risk_limits else 'off'} (full risk engine: M7)",
        "costs": _cost_text(loaded),
        "period": f"{index[0]:%Y-%m-%d} .. {index[-1]:%Y-%m-%d} ({len(index)} sessions)",
        "initial_capital": f"{p.initial_cash:,.2f}",
        "final_equity": f"{daily['equity'].iloc[-1]:,.2f}",
        "realized_pnl": f"{p.realized_pnl:,.2f}",
        "commissions": f"{p.commissions:,.2f}",
        "implicit_costs": f"{p.implicit_costs:,.2f} (spread + slippage + impact)",
        "interest": f"{p.interest:,.2f}",
        "fills": str(len(p.fills)),
        "cancelled_or_trimmed_orders": str(len(result.cancelled)),
        **{f"data {k}": v for k, v in data.provenance.items()},
    }
    return AnalysedBacktest(
        result=result,
        benchmarks=bench,
        segments=segments,
        metrics=metrics,
        trades=trades,
        metadata=metadata,
        notes=[*(notes or []), *bench_notes, *result.warnings],
    )


def _cost_text(loaded: LoadedConfig) -> str:
    c = loaded.settings.backtest.costs
    spreads = ", ".join(f"{k} {v:g}" for k, v in c.half_spread_bps.items())
    return (
        f"commission {c.commission_per_share:g}/sh + {c.commission_per_order:g}/order "
        f"(min {c.commission_minimum:g}); half-spread bps [{spreads}]; slippage "
        f"{c.slippage_bps:g} bps; impact {c.impact_coefficient_bps:g} bps "
        f"x sqrt(q/ADV{c.adv_window}); "
        f"max participation {c.max_participation:.0%}"
    )
