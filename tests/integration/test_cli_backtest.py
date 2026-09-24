"""End-to-end: files -> data download -> synthesize -> `aq backtest run` -> report."""

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.cli import EXIT_OK, EXIT_REFUSED, main
from adaptive_quant.config.loader import load_config
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.quant.data.history import product_spec
from adaptive_quant.quant.data.synthetic import (
    FinancingAssumptions,
    constant_risk_free,
    synthesize_leveraged_bars,
)
from tests.data_helpers import daily_bars, market_dates

pytestmark = pytest.mark.integration

S, E = date(2008, 1, 2), date(2011, 12, 30)
NOW = datetime(2012, 1, 3, 15, tzinfo=UTC)


def to_csv(df: pd.DataFrame, path: Path) -> None:
    out = df.drop(columns=[c for c in ("is_synthetic",) if c in df.columns]).reset_index()
    out.insert(0, "date", out.pop("timestamp").dt.tz_convert("America/New_York").dt.date)
    out.to_csv(path, index=False)
    path.with_name(path.stem + "_actions.csv").write_text("ex_date,type,ratio,amount\n")


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import shutil

    from tests.conftest import REPO_CONFIG

    root = tmp_path_factory.mktemp("bt")
    config_dir = root / "config"
    shutil.copytree(REPO_CONFIG, config_dir)
    settings = load_config("development", config_dir=config_dir).settings
    imp = root / "var/data/import"
    imp.mkdir(parents=True)
    qqq = daily_bars(S, E, start_price=40, vol=0.013, seed=5)
    to_csv(qqq, imp / "QQQ.csv")
    to_csv(daily_bars(S, E, start_price=120, vol=0.011, seed=6), imp / "SPY.csv")
    syn = settings.data.synthetic
    rf = constant_risk_free(pd.DatetimeIndex(qqq.index), syn.risk_free.constant_annual_rate)
    for i, sym in enumerate(("TQQQ", "SQQQ")):
        spec = product_spec(settings, sym)
        model, _ = synthesize_leveraged_bars(
            qqq,
            spec,
            rf,
            FinancingAssumptions(syn.swap_spread_annual, syn.underlying_expense_addback),
        )
        real = model[market_dates(model) >= spec.inception].copy()
        noise = np.cumprod(1 + np.random.default_rng(i).normal(0, 0.0003, len(real)))
        for col in ("open", "high", "low", "close"):
            real[col] = real[col] * noise
        real["high"] = real[["open", "high", "low", "close"]].max(axis=1)
        real["low"] = real[["open", "high", "low", "close"]].min(axis=1)
        real["volume"] = 5e6
        to_csv(real, imp / f"{sym}.csv")
    assert aq(config_dir, "data", "download", "--start", str(S)) == EXIT_OK
    assert aq(config_dir, "data", "synthesize") == EXIT_OK
    return config_dir


def aq(config_dir: Path, *args: str) -> int:
    return main(
        ["--config-dir", str(config_dir), "--env-file", "/nonexistent", *args],
        clock=FrozenClock(NOW),
    )


def test_real_data_backtest(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    out = tmp_path / "real"
    assert (
        aq(project, "backtest", "run", "--strategy", "st_ema_cross", "--out", str(out)) == EXIT_OK
    )
    text = capsys.readouterr().out
    assert "HYPOTHETICAL BACKTEST" in text
    assert "SYNTHETIC" not in text
    assert "execution near_close" in text
    # real TQQQ starts 2010-02-11, so the default start cannot be earlier
    assert "period 2010-02-1" in text
    page = (out / "report.html").read_text()
    assert "Hypothetical backtest" in page
    assert "Contains SYNTHETIC" not in page


def test_synthetic_extended_backtest_is_labelled(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    out = tmp_path / "syn"
    code = aq(
        project, "backtest", "run", "--strategy", "st_ema_cross", "--synthetic", "--out", str(out)
    )
    assert code == EXIT_OK
    text = capsys.readouterr().out
    assert "Includes SYNTHETIC price history" in text
    assert "use_synthetic_history=True" in text  # override recorded
    assert "period 2009-" in text  # 2008 data minus the risk engine's 272-bar warm-up
    page = (out / "report.html").read_text()
    assert "SYNTHETIC data period only" in page
    assert "Real data only" in page


def test_closing_auction_and_delay(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert (
        aq(
            project,
            "backtest",
            "run",
            "--strategy",
            "st_ema_cross",
            "--execution",
            "closing_auction",
            "--out",
            str(tmp_path / "a"),
        )
        == EXIT_OK
    )
    assert "closing_auction model" in capsys.readouterr().out
    assert (
        aq(
            project,
            "backtest",
            "run",
            "--strategy",
            "st_ema_cross",
            "--execution",
            "next_open",
            "--delay",
            "2",
            "--out",
            str(tmp_path / "b"),
        )
        == EXIT_OK
    )
    assert "execution next_open (+2 bars)" in capsys.readouterr().out


def test_multiple_strategies_naive_average(project: Path, tmp_path: Path) -> None:
    assert (
        aq(
            project,
            "backtest",
            "run",
            "--strategy",
            "st_ema_cross",
            "--strategy",
            "baseline_cash",
            "--out",
            str(tmp_path / "m"),
        )
        == EXIT_OK
    )
    decisions = pd.read_csv(tmp_path / "m" / "decisions.csv")
    assert decisions["signals"].str.contains("baseline_cash").all()


def test_unknown_strategy_refused(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert aq(project, "backtest", "run", "--strategy", "no_such_strategy") == EXIT_REFUSED
    assert "not available for backtesting" in capsys.readouterr().err


def test_no_credentials_needed_and_none_leaked(
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "SECRET-DO-NOT-LEAK-789")
    capsys.readouterr()
    out = tmp_path / "s"
    assert (
        aq(project, "backtest", "run", "--strategy", "st_ema_cross", "--out", str(out)) == EXIT_OK
    )
    captured = capsys.readouterr()
    assert "SECRET-DO-NOT-LEAK-789" not in captured.out + captured.err
    for f in out.iterdir():
        assert "SECRET-DO-NOT-LEAK-789" not in f.read_text()
