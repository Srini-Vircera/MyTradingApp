"""QQQ-only buy-and-hold through the one backtest engine (no synthetic data invented).

The engine prices TQQQ/SQQQ only when a strategy can hold them; a missing
instrument is tolerated only when provably unused and the run aborts if an
allocation would ever hold it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adaptive_quant.config.loader import load_config
from adaptive_quant.config.runtime import apply_runtime
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import MissingDataError
from adaptive_quant.services import backtests
from tests.control.conftest import QNOW

pytestmark = pytest.mark.integration


def test_qqq_buy_and_hold_runs_with_qqq_data_only(qqq_project: Path) -> None:
    loaded = load_config("development", config_dir=qqq_project)
    outcome = backtests.run(
        loaded,
        FrozenClock(QNOW),
        backtests.BacktestRequest(strategies=["baseline_buy_hold"], source="file"),
    )
    assert outcome.unpriced == ["TQQQ", "SQQQ"]
    daily = outcome.analysed.result.daily
    assert (daily["w_TQQQ"] == 0).all() and (daily["w_SQQQ"] == 0).all()
    assert (daily["w_QQQ"].iloc[5:] > 0).all()  # held throughout (size set by the risk engine)
    assert not daily["synthetic"].any()
    assert any("can never hold" in n for n in outcome.notes)
    summary = backtests.summarize(outcome)
    assert summary["strategies"] == ["baseline_buy_hold"]
    assert summary["headline"]["cagr"] is not None
    assert summary["curve"] and summary["curve"][-1]["equity"] == summary["ending_equity"]
    assert "HYPOTHETICAL" in summary["disclaimer"]
    assert summary["unpriced_instruments"] == ["TQQQ", "SQQQ"]
    assert outcome.report.is_file()


def test_leveraged_exposure_still_needs_leveraged_data(qqq_project: Path) -> None:
    base = load_config("development", config_dir=qqq_project)
    levered = apply_runtime(base, {}, {"baseline_buy_hold": {"params": {"max_long_exposure": 2.0}}})
    with pytest.raises(MissingDataError):
        backtests.run(
            levered,
            FrozenClock(QNOW),
            backtests.BacktestRequest(strategies=["baseline_buy_hold"], source="file"),
        )
