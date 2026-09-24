import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from adaptive_quant.config.loader import load_config
from adaptive_quant.quant.analytics.report import render_html, write_report
from adaptive_quant.quant.backtest.benchmarks import benchmark_curves
from adaptive_quant.quant.backtest.data import BacktestData
from adaptive_quant.quant.backtest.runner import analyse
from adaptive_quant.quant.strategies.registry import create
from tests.conftest import REPO_CONFIG
from tests.unit.backtest.helpers import ScriptedExposure, random_frames, run

LOADED = load_config("development", config_dir=REPO_CONFIG)
FRAMES = random_frames(seed=5)
FRAMES["SPY"] = random_frames(seed=6)["QQQ"]


def _index() -> pd.DatetimeIndex:
    return pd.DatetimeIndex(FRAMES["QQQ"].index[5:])


def test_benchmarks_normalized_to_initial_capital() -> None:
    idx = _index()
    bench, notes = benchmark_curves(FRAMES, ["SPY", "QQQ", "TQQQ"], idx, 50_000, cash_rate=0.0365)
    names = [b.name for b in bench]
    assert names == ["SPY buy & hold", "QQQ buy & hold", "TQQQ buy & hold", "Cash"]
    q = bench[1].equity
    assert q.iloc[0] == pytest.approx(50_000)
    assert q.iloc[-1] / q.iloc[0] == pytest.approx(
        FRAMES["QQQ"]["close"].iloc[-1] / FRAMES["QQQ"]["close"].iloc[5]
    )
    cash = bench[-1].equity
    days = (idx[-1] - idx[0]).days
    assert cash.iloc[-1] == pytest.approx(50_000 * 1.0001**days, rel=1e-3)
    assert notes == []


def test_partial_benchmarks_are_skipped_not_misaligned() -> None:
    idx = _index()
    short = {"TQQQ": FRAMES["TQQQ"].iloc[100:]}
    bench, notes = benchmark_curves(short, ["TQQQ", "IWM"], idx, 1.0)
    assert [b.name for b in bench] == ["Cash"]
    assert any("TQQQ benchmark unavailable for the full period" in n for n in notes)
    assert any("IWM" in n for n in notes)


def _analysed(synthetic_until: int | None = None, execution: str = "near_close"):  # type: ignore[no-untyped-def]
    synthetic = {}
    if synthetic_until is not None:
        flags = pd.Series(False, index=FRAMES["TQQQ"].index)
        flags.iloc[:synthetic_until] = True
        synthetic["TQQQ"] = flags
    data = BacktestData(frames=FRAMES, synthetic=synthetic, provenance={"QQQ": "generated"})
    res = run(
        FRAMES,
        create("st_ema_cross", params={"max_long_exposure": 2.0}),
        start=date(2020, 3, 2),
        execution=execution,
    )
    for flags in synthetic.values():  # mark the engine's daily rows the way the engine would
        res.daily["synthetic"] = [bool(flags.get(ts, False)) for ts in res.daily.index]
    return analyse(res, data, LOADED)


def test_report_marks_results_hypothetical_and_is_self_contained(tmp_path: Path) -> None:
    a = _analysed()
    path = write_report(a, tmp_path, "Backtest: st_ema_cross")
    page = path.read_text()
    assert "Hypothetical backtest" in page
    assert (
        "SYNTHETIC" not in page.split("<footer")[0].split("Assumptions")[0]
        or "synthetic" in a.segments
    )
    for needle in (
        "Equity curve",
        "Equity curve (log scale)",
        "Drawdown",
        "Rolling 1-year return",
        "Rolling Sharpe",
        "Monthly returns",
        "Annual returns",
        "Exposure over time",
        "Portfolio allocation over time",
        "Costs and trading",
        "SPY buy &amp; hold",
    ):
        assert needle in page, needle
    assert "http://" not in page and "https://" not in page  # no external assets or trackers
    assert "<script src" not in page
    for name in (
        "daily.csv",
        "fills.csv",
        "trades.csv",
        "decisions.csv",
        "cancelled.csv",
        "metrics.json",
    ):
        assert (tmp_path / name).exists(), name
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert "Strategy" in metrics["metrics"]["all"]
    fills = pd.read_csv(tmp_path / "fills.csv")
    assert list(fills.columns[:4]) == ["order_id", "time", "decision_time", "data_timestamp"]
    assert len(fills) == len(a.result.fills)


def test_synthetic_periods_are_separated(tmp_path: Path) -> None:
    a = _analysed(synthetic_until=120)
    assert set(a.segments) == {"all", "synthetic", "real"}
    assert "Strategy" in a.metrics["synthetic"] and "Strategy" in a.metrics["real"]
    assert a.segments["synthetic"][-1] < a.segments["real"][0]
    page = render_html(a, "t")
    assert "Contains SYNTHETIC price history" in page
    assert "SYNTHETIC data period only" in page
    assert "Real data only" in page
    assert 'class="band"' in page  # shaded, labelled synthetic band on the time charts


def test_closing_auction_is_flagged_in_report() -> None:
    page = render_html(_analysed(execution="closing_auction"), "t")
    assert "Closing-auction execution" in page


def test_untrusted_names_are_escaped() -> None:
    a = _analysed()
    page = render_html(a, "<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_report_with_no_trades(tmp_path: Path) -> None:
    res = run(FRAMES, ScriptedExposure(base=0.0))
    a = analyse(res, BacktestData(frames=FRAMES), LOADED)
    assert a.trades.count == 0
    write_report(a, tmp_path, "cash only")
    assert (tmp_path / "report.html").exists()
