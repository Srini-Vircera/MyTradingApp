"""`aq db upgrade | status | explain` end-to-end against real PostgreSQL."""

import json
import shutil
from pathlib import Path

import pytest

from adaptive_quant.cli import EXIT_OK, EXIT_REFUSED, main
from adaptive_quant.persistence.db import Database
from tests.conftest import REPO_CONFIG
from tests.persistence.helpers import CYCLE, seed


def aq(cfg: Path, *args: str) -> int:
    return main(["--config-dir", str(cfg), "--env-file", "/nonexistent", *args])


def test_upgrade_status_and_explain(
    empty_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, cfg)
    url = empty_db.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    assert aq(cfg, "db", "status") == EXIT_REFUSED
    assert "not migrated" in capsys.readouterr().out
    assert aq(cfg, "db", "upgrade") == EXIT_OK
    out = capsys.readouterr().out
    assert "22 strategy versions" in out
    assert aq(cfg, "db", "status") == EXIT_OK
    assert "drift     none" in capsys.readouterr().out
    seed(empty_db)
    assert aq(cfg, "db", "explain", CYCLE) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["cycle"]["cycle_id"] == CYCLE


def test_missing_database_url_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, cfg)
    assert aq(cfg, "db", "status") == EXIT_REFUSED
    assert "DATABASE_URL" in capsys.readouterr().err
