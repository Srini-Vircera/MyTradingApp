"""Shared fixtures."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml

from adaptive_quant.core.clock import FrozenClock

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG = REPO_ROOT / "config"

_ENV_VARS = (
    "AQ_ENV",
    "AQ_CONFIG_DIR",
    "AQ_LIVE_TRADING_CONFIRM",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "POLYGON_API_KEY",
    "DATABASE_URL",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests never see the developer's real credentials or environment selection."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
    structlog.reset_defaults()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2024, 1, 2, 20, 45, tzinfo=UTC))


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A private, editable copy of the repository's config directory."""
    dest = tmp_path / "project" / "config"
    shutil.copytree(REPO_CONFIG, dest)
    return dest


PatchYaml = Callable[[str, Callable[[dict[str, Any]], None]], None]


@pytest.fixture
def patch_yaml(config_dir: Path) -> PatchYaml:
    """Edit one YAML file in the copied config dir: ``patch_yaml("risk.yaml", fn)``."""

    def _patch(name: str, fn: Callable[[dict[str, Any]], None]) -> None:
        path = config_dir / name
        data = yaml.safe_load(path.read_text()) or {}
        fn(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    return _patch
