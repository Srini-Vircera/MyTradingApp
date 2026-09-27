"""Control-plane test fixtures (PostgreSQL fixtures re-used from the persistence tests)."""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from adaptive_quant.cli import EXIT_OK, main
from adaptive_quant.core.clock import FrozenClock
from tests.conftest import REPO_CONFIG
from tests.data_helpers import daily_bars
from tests.persistence.conftest import db, empty_db, pg_server_url  # noqa: F401

QS, QE = date(2016, 1, 4), date(2019, 12, 31)
QNOW = datetime(2020, 1, 3, 15, tzinfo=UTC)


def to_csv(df: pd.DataFrame, path: Path, *, actions: bool = True) -> None:
    out = df.drop(columns=[c for c in ("is_synthetic",) if c in df.columns]).reset_index()
    out.insert(0, "date", out.pop("timestamp").dt.tz_convert("America/New_York").dt.date)
    out.to_csv(path, index=False)
    if actions:
        path.with_name(path.stem + "_actions.csv").write_text("ex_date,type,ratio,amount\n")


def aq(config_dir: Path, *args: str) -> int:
    return main(
        ["--config-dir", str(config_dir), "--env-file", "/nonexistent", *args],
        clock=FrozenClock(QNOW),
    )


@pytest.fixture(scope="module")
def qqq_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A project whose store holds QQQ only (like the operator's current dataset)."""
    root = tmp_path_factory.mktemp("qqq")
    config_dir = root / "config"
    shutil.copytree(REPO_CONFIG, config_dir)
    imp = root / "var/data/import"
    imp.mkdir(parents=True)
    # like the operator's real file: extra div/split columns and no QQQ_actions.csv
    bars = daily_bars(QS, QE, start_price=100, vol=0.012, seed=11).assign(div=0.0, split=1.0)
    to_csv(bars, imp / "QQQ.csv", actions=False)
    assert aq(config_dir, "data", "download", "--symbols", "QQQ", "--start", str(QS)) == EXIT_OK
    return config_dir
