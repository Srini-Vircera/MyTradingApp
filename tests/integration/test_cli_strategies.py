"""End-to-end: stored data -> `aq strategies signals` (research only, never orders)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from adaptive_quant.cli import EXIT_OK, EXIT_REFUSED, main
from adaptive_quant.core.clock import FrozenClock
from tests.conftest import PatchYaml
from tests.data_helpers import daily_bars

pytestmark = pytest.mark.integration

START, END = date(2010, 1, 4), date(2012, 6, 29)
NOW = datetime(2012, 7, 2, 14, 0, tzinfo=UTC)


def to_csv(df: pd.DataFrame, path: Path) -> None:
    out = df.reset_index()
    out.insert(0, "date", out.pop("timestamp").dt.tz_convert("America/New_York").dt.date)
    out.to_csv(path, index=False)


def aq(config_dir: Path, *args: str) -> int:
    return main(
        ["--config-dir", str(config_dir), "--env-file", "/nonexistent", *args],
        clock=FrozenClock(NOW),
    )


@pytest.fixture
def project(config_dir: Path) -> Path:
    imports = config_dir.parent / "var/data/import"
    imports.mkdir(parents=True)
    to_csv(daily_bars(START, END, start_price=45.0, vol=0.012, seed=3), imports / "QQQ.csv")
    (imports / "QQQ_actions.csv").write_text(
        "ex_date,type,ratio,amount\n2011-12-19,cash_dividend,,0.10\n"
    )
    return config_dir


def download(project: Path, *symbols: str) -> None:
    assert aq(project, "data", "download", "--symbols", *symbols, "--start", str(START)) == EXIT_OK


def test_list_and_validate(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert aq(project, "strategies", "validate") == EXIT_OK
    out = capsys.readouterr().out
    assert "22 strategies valid" in out
    assert "eligible in live    : 0" in out
    assert aq(project, "strategies", "list") == EXIT_OK
    assert "ltt_sma_distance@1.0.0#" in capsys.readouterr().out


def test_signals_from_stored_data(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    download(project, "QQQ")
    capsys.readouterr()
    assert aq(project, "strategies", "signals", "--as-of", "2012-06-29") == EXIT_OK
    out = capsys.readouterr().out
    assert "RESEARCH OUTPUT - no orders are placed" in out
    assert "optional data not available: NDX" in out
    assert "NDX unavailable, not used" in out
    assert "NO SIGNAL" not in out
    assert out.count("\n") >= 24  # header lines + 22 strategies


def test_signals_before_warmup_refuse(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    download(project, "QQQ")
    capsys.readouterr()
    assert aq(project, "strategies", "signals", "--as-of", "2010-03-01") == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "NO SIGNAL" in out
    assert "needs" in out


def test_governed_modes_have_no_eligible_strategies(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert aq(project, "strategies", "signals", "--mode", "paper") == EXIT_REFUSED
    assert "no strategies are eligible" in capsys.readouterr().out
    with pytest.raises(SystemExit):  # live is not even a selectable research mode
        aq(project, "strategies", "signals", "--mode", "live")


def test_no_secret_leaks(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PKLEAKCHECK123")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "SECRETLEAKCHECK456")
    download(project, "QQQ")
    aq(project, "strategies", "signals", "--as-of", "2012-06-29")
    aq(project, "strategies", "list")
    captured = capsys.readouterr()
    for secret in ("PKLEAKCHECK123", "SECRETLEAKCHECK456"):
        assert secret not in captured.out
        assert secret not in captured.err


def test_invalid_strategy_config_is_refused_everywhere(
    project: Path, patch_yaml: PatchYaml, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_yaml(
        "strategies.yaml",
        lambda d: d["strategies"]["strategies"][0]["params"].update({"window": 5}),
    )
    assert aq(project, "strategies", "validate") == EXIT_REFUSED
    assert "ltt_sma_distance" in capsys.readouterr().err
    assert aq(project, "config", "validate") == EXIT_REFUSED


def test_live_approved_without_approval_is_refused(
    project: Path, patch_yaml: PatchYaml, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_yaml(
        "strategies.yaml",
        lambda d: d["strategies"]["strategies"][0].update({"lifecycle": "live_approved"}),
    )
    assert aq(project, "config", "validate") == EXIT_REFUSED
    assert "approval" in capsys.readouterr().err


def test_datetime_as_of(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    download(project, "QQQ")
    capsys.readouterr()
    code = aq(project, "strategies", "signals", "--as-of", "2012-06-29T15:45:00-04:00")
    assert code == EXIT_OK
    assert "2012-06-29 15:45" in capsys.readouterr().out
