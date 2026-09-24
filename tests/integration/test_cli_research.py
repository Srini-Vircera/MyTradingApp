"""End-to-end: stored (generated) data -> `aq research run | trials | status` -> report."""

from pathlib import Path

import pytest
import yaml

from adaptive_quant.cli import EXIT_OK, EXIT_REFUSED
from tests.integration.test_cli_backtest import aq, project  # noqa: F401 - fixture reuse

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def research_project(project: Path) -> Path:  # noqa: F811 - the imported fixture
    """Short walk-forward windows and few simulations so the 4-year fixture yields folds."""
    path = project / "development.yaml"
    data = yaml.safe_load(path.read_text())
    data["research"] = {
        "walk_forward": {
            "train_years": 0.5,
            "validate_years": 0.25,
            "test_years": 0.25,
            "step_years": 0.25,
        },
        "monte_carlo": {"cost_simulations": 1, "delays": [0, 1], "min_years": 0.5, "top_n": 1},
        "bootstrap": {"samples": 200},
        "pbo_partitions": 6,
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return project


def test_research_run_trials_and_status(
    research_project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    out = tmp_path / "r"
    args = [
        "research",
        "run",
        "--strategy",
        "st_ema_cross",
        "--simulations",
        "30",
        "--out",
        str(out),
    ]
    assert aq(research_project, *args) == EXIT_OK
    text = capsys.readouterr().out
    assert "HYPOTHETICAL RESEARCH" in text
    assert "9 trials this run" in text  # 3 x 3 param_grid from strategies.yaml
    assert "st_ema_cross" in text and "Reality Check p=" in text
    assert "monte_carlo.simulations=30" in text  # override recorded
    page = (out / "report.html").read_text()
    assert "OUT-OF-SAMPLE" in page and "IN-SAMPLE (hindsight)" in page
    for secretish in ("ALPACA", "POLYGON_API_KEY", "SecretStr"):
        assert secretish not in page

    assert aq(research_project, "research", "trials") == EXIT_OK
    trials = capsys.readouterr().out
    assert "9 distinct grid trials" in trials

    assert aq(research_project, "research", "status") == EXIT_OK
    status = capsys.readouterr().out
    assert "st_ema_cross" in status and "human approval" in status
    cfg = yaml.safe_load((research_project / "strategies.yaml").read_text())
    ema = next(s for s in cfg["strategies"]["strategies"] if s["id"] == "st_ema_cross")
    assert ema.get("lifecycle", "research") == "research"  # never edited by software


def test_synthetic_research_is_labelled_and_cannot_validate(
    research_project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    args = ["research", "run", "--strategy", "st_ema_cross", "--synthetic", "--simulations", "20"]
    assert aq(research_project, *args, "--out", str(tmp_path / "s")) == EXIT_OK
    text = capsys.readouterr().out
    assert "Includes SYNTHETIC history" in text
    assert "real_data" in text  # the real-data gate fails
    assert "-> validated" not in text


def test_unknown_strategy_refused(
    research_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert aq(research_project, "research", "run", "--strategy", "nope") == EXIT_REFUSED
    assert "not available for research" in capsys.readouterr().err


def test_empty_registry_and_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import shutil

    from tests.conftest import REPO_CONFIG

    cfg = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, cfg)
    assert aq(cfg, "research", "trials") == EXIT_OK
    assert "empty" in capsys.readouterr().out
    assert aq(cfg, "research", "status") == EXIT_OK
    assert "no research evidence" in capsys.readouterr().out
