"""`aq trade` refuses to run without human-promoted strategies, credentials or a safe mode."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from adaptive_quant.cli import EXIT_OK, EXIT_REFUSED, main
from adaptive_quant.core.clock import FrozenClock
from tests.conftest import REPO_CONFIG

pytestmark = pytest.mark.integration


def aq(cfg: Path, *args: str, at: datetime) -> int:
    return main(
        ["--config-dir", str(cfg), "--env-file", "/nonexistent", *args], clock=FrozenClock(at)
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, d)
    return d


def test_status_shows_schedule_and_early_close(
    cfg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        aq(cfg, "--env", "paper", "trade", "status", at=datetime(2024, 11, 29, 15, tzinfo=UTC))
        == EXIT_OK
    )
    out = capsys.readouterr().out
    assert (
        "EARLY CLOSE" in out and "12:40  health_check" in out and "no new orders from 12:55" in out
    )
    assert (
        aq(cfg, "--env", "paper", "trade", "status", at=datetime(2024, 7, 6, 15, tzinfo=UTC))
        == EXIT_OK
    )
    assert "no trading session today" in capsys.readouterr().out


def test_run_refuses_without_promoted_strategies(
    cfg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = aq(
        cfg, "--env", "paper", "trade", "run", "--once", at=datetime(2024, 7, 2, 19, 45, tzinfo=UTC)
    )
    assert code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "no strategy is eligible for paper mode" in err and "software never does this" in err


def test_run_refuses_backtest_mode(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = cfg / "development.yaml"
    data = yaml.safe_load(path.read_text())
    data["trading"]["mode"] = "backtest"
    path.write_text(yaml.safe_dump(data))
    assert (
        aq(cfg, "trade", "run", "--once", at=datetime(2024, 7, 2, 19, 45, tzinfo=UTC))
        == EXIT_REFUSED
    )
    assert "paper or shadow mode only" in capsys.readouterr().err


def test_run_needs_credentials_once_strategies_are_promoted(
    cfg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = cfg / "strategies.yaml"
    data = yaml.safe_load(path.read_text())
    data["strategies"]["strategies"][0]["lifecycle"] = "paper"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    assert (
        aq(
            cfg,
            "--env",
            "paper",
            "trade",
            "run",
            "--once",
            at=datetime(2024, 7, 2, 19, 45, tzinfo=UTC),
        )
        == EXIT_REFUSED
    )
    assert "DATABASE_URL" in capsys.readouterr().err
