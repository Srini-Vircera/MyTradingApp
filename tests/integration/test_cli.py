"""End-to-end tests of the operator CLI against a private copy of the config."""

import json
from pathlib import Path

import pytest

from adaptive_quant.cli import EXIT_OK, EXIT_REFUSED, main
from adaptive_quant.trading.safety.kill_switch import RELEASE_CONFIRMATION
from tests.conftest import PatchYaml

pytestmark = pytest.mark.integration


def run(config_dir: Path, *args: str) -> int:
    return main(["--config-dir", str(config_dir), "--env-file", "/nonexistent", *args])


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_validate_ok(config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(config_dir, "--env", "paper", "config", "validate") == EXIT_OK
    out = capsys.readouterr().out
    assert "configuration is valid" in out
    assert "trading mode: paper" in out
    assert "REAL MONEY" not in out


def test_validate_requires_secrets(config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run(config_dir, "config", "validate", "--require-secrets", "broker")
    assert code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "ALPACA_API_KEY_ID" in err and "What to do" in err


def test_validate_reports_bad_config(
    config_dir: Path, patch_yaml: PatchYaml, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_yaml("paper.yaml", lambda d: d["trading"].update({"mode": "live"}))
    assert run(config_dir, "--env", "paper", "config", "validate") == EXIT_REFUSED
    assert "live trading requested but not fully authorised" in capsys.readouterr().err


def test_show_redacts(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "topsecret")
    assert run(config_dir, "config", "show") == EXIT_OK
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["config_version"].startswith("development-")
    assert "topsecret" not in out


def test_secrets_command(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@h/db")
    assert run(config_dir, "secrets") == EXIT_OK
    out = capsys.readouterr().out
    assert "DATABASE_URL" in out and "pw@" not in out


def test_kill_switch_workflow(config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(config_dir, "kill-switch", "status") == EXIT_OK
    assert "ENGAGED" in capsys.readouterr().out  # fresh install fails closed

    bad = run(
        config_dir,
        "kill-switch",
        "release",
        "--actor",
        "srini",
        "--reason",
        "ready",
        "--confirm",
        "yes",
    )
    assert bad == EXIT_REFUSED
    capsys.readouterr()

    ok = run(
        config_dir,
        "kill-switch",
        "release",
        "--actor",
        "srini",
        "--reason",
        "ready",
        "--confirm",
        RELEASE_CONFIRMATION,
    )
    assert ok == EXIT_OK
    assert "released" in capsys.readouterr().out

    assert run(config_dir, "kill-switch", "engage", "--actor", "srini", "--reason", "halt") == 0
    assert "ENGAGED" in capsys.readouterr().out
    state_file = config_dir.parent / "var/state/kill_switch.json"
    assert json.loads(state_file.read_text())["engaged"] is True
