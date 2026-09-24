"""End-to-end: CSV files -> `aq data download` -> list -> validate -> synthesize."""

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
from tests.conftest import PatchYaml
from tests.data_helpers import daily_bars, market_dates

pytestmark = pytest.mark.integration

START, END = date(2009, 1, 2), date(2012, 6, 29)
CLOCK_TIME = datetime(2012, 7, 2, 14, 0, tzinfo=UTC)  # Monday morning after END


def to_csv(df: pd.DataFrame, path: Path) -> None:
    out = df.drop(columns=[c for c in ("is_synthetic",) if c in df.columns]).reset_index()
    out.insert(0, "date", out.pop("timestamp").dt.tz_convert("America/New_York").dt.date)
    out.to_csv(path, index=False)


@pytest.fixture
def project(config_dir: Path) -> Path:
    settings = load_config("development", config_dir=config_dir).settings
    imports = config_dir.parent / "var/data/import"
    imports.mkdir(parents=True)
    qqq = daily_bars(START, END, start_price=30.0, vol=0.012, seed=11)
    spy = daily_bars(START, END, start_price=90.0, vol=0.01, seed=12)
    to_csv(qqq, imports / "QQQ.csv")
    to_csv(spy, imports / "SPY.csv")
    syn = settings.data.synthetic
    rf = constant_risk_free(pd.DatetimeIndex(qqq.index), syn.risk_free.constant_annual_rate)
    fin = FinancingAssumptions(syn.swap_spread_annual, syn.underlying_expense_addback)
    for i, symbol in enumerate(("TQQQ", "SQQQ")):
        spec = product_spec(settings, symbol)
        model, _ = synthesize_leveraged_bars(qqq, spec, rf, fin)
        real = model[market_dates(model) >= spec.inception].copy()
        noise = np.cumprod(1 + np.random.default_rng(i).normal(0, 0.0003, len(real)))
        for col in ("open", "high", "low", "close"):
            real[col] = real[col] * noise
        real["high"] = real[["open", "high", "low", "close"]].max(axis=1)
        real["low"] = real[["open", "high", "low", "close"]].min(axis=1)
        real["volume"] = 1e6
        to_csv(real, imports / f"{symbol}.csv")
    for symbol in ("QQQ", "SPY", "TQQQ", "SQQQ"):
        (imports / f"{symbol}_actions.csv").write_text("ex_date,type,ratio,amount\n")
    return config_dir


def aq(config_dir: Path, *args: str) -> int:
    return main(
        ["--config-dir", str(config_dir), "--env-file", "/nonexistent", *args],
        clock=FrozenClock(CLOCK_TIME),
    )


def test_full_data_workflow(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = aq(
        project,
        "data",
        "download",
        "--symbols",
        "QQQ",
        "SPY",
        "TQQQ",
        "SQQQ",
        "--start",
        str(START),
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "4/4 symbol(s) stored and valid" in out
    assert f"..{END}" in out  # default end = last completed session

    assert aq(project, "data", "list") == EXIT_OK
    listing = capsys.readouterr().out
    assert "file:QQQ:1d:raw" in listing
    assert "file:QQQ:1d:all" in listing

    assert aq(project, "data", "validate", "--require-fresh") == EXIT_OK
    assert "STALE" not in capsys.readouterr().out

    assert aq(project, "data", "synthesize") == EXIT_OK
    syn = capsys.readouterr().out
    assert "OK   TQQQ SYNTHETIC" in syn
    assert "PASS" in syn
    assert "ASSUMPTION" in syn  # constant risk-free rate is called out

    assert aq(project, "data", "list") == EXIT_OK
    assert "SYNTHETIC" in capsys.readouterr().out

    # re-running the download changes nothing (idempotent)
    assert aq(project, "data", "download", "--symbols", "QQQ", "--start", str(START)) == EXIT_OK
    manifests = list((project.parent / "var/data/bars/file/1d/raw/QQQ").glob("*.parquet"))
    assert len(manifests) == 1


def test_stale_data_fails_require_fresh(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        aq(
            project,
            "data",
            "download",
            "--symbols",
            "QQQ",
            "--start",
            str(START),
            "--end",
            "2012-06-01",
        )
        == EXIT_OK
    )
    capsys.readouterr()
    assert aq(project, "data", "validate", "--symbols", "QQQ") == EXIT_OK  # reported only
    assert "STALE" in capsys.readouterr().out
    assert aq(project, "data", "validate", "--symbols", "QQQ", "--require-fresh") == EXIT_REFUSED


def test_invalid_file_data_is_refused(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = project.parent / "var/data/import/SPY.csv"
    df = pd.read_csv(path)
    df.loc[10, "close"] = 0
    df.to_csv(path, index=False)
    assert (
        aq(project, "data", "download", "--symbols", "SPY", "--start", str(START)) == EXIT_REFUSED
    )
    assert "non_positive_price" in capsys.readouterr().out
    assert aq(project, "data", "validate", "--symbols", "SPY") == EXIT_REFUSED


def test_missing_data_and_credentials(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert aq(project, "data", "validate", "--symbols", "QQQ") == EXIT_REFUSED
    assert "MISSING" in capsys.readouterr().out
    assert aq(project, "data", "synthesize") == EXIT_REFUSED
    assert aq(project, "data", "download", "--provider", "alpaca") == EXIT_REFUSED
    assert "ALPACA_API_KEY_ID" in capsys.readouterr().err
    assert (
        aq(project, "data", "download", "--symbols", "IWM", "--start", str(START)) == EXIT_REFUSED
    )


def test_list_when_empty(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert aq(project, "data", "list") == EXIT_OK
    assert "no data stored" in capsys.readouterr().out


def test_adjusted_file_data_validates_its_own_basis(
    project: Path, patch_yaml: PatchYaml, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_yaml("base.yaml", lambda d: d["data"]["file_import"].update({"adjustment": "all"}))
    assert aq(project, "data", "download", "--symbols", "QQQ", "--start", str(START)) == EXIT_OK
    assert "no local adjustment possible" in capsys.readouterr().out
    assert aq(project, "data", "validate", "--symbols", "QQQ") == EXIT_OK
    assert "file:QQQ:1d:all" in capsys.readouterr().out
