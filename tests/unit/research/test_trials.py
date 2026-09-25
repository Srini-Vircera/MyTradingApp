"""Trials are deterministic backtests; the registry counts every configuration honestly."""

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from adaptive_quant.core.errors import AQError
from adaptive_quant.quant.research.trials import (
    TrialRegistry,
    TrialSpec,
    data_fingerprint,
    run_trial,
    run_trials,
    trial_id,
)
from tests.unit.research.helpers import context

NOW = datetime(2024, 1, 2, tzinfo=UTC)


def spec(fast: int = 5, slow: int = 20) -> TrialSpec:
    return TrialSpec(
        "st_ema_cross", "st_ema_cross", {"fast": fast, "slow": slow}, (0, 0), f"{fast}/{slow}"
    )


def test_trial_outcome_is_a_consistent_backtest() -> None:
    ctx = context()
    o = run_trial(spec(), ctx)
    assert o.ok and o.fills > 0
    assert len(o.returns) == len(o.equity) - 1
    np.testing.assert_allclose((1 + o.returns).cumprod() * o.equity.iloc[0], o.equity.iloc[1:])
    assert o.version_id.startswith("st_ema_cross@")
    again = run_trial(spec(), ctx)
    assert o.trial_id == again.trial_id
    assert (o.returns == again.returns).all()


def test_invalid_parameter_combination_is_a_failed_outcome() -> None:
    o = run_trial(spec(fast=30, slow=20), context())
    assert not o.ok and "fast < slow" in (o.error or "")
    assert len(o.returns) == 0


def test_trial_identity_depends_on_version_data_settings_and_period() -> None:
    ctx = context()
    other_data = context(seed=4)
    assert data_fingerprint(ctx.frames) != data_fingerprint(other_data.frames)
    assert data_fingerprint(ctx.frames) == data_fingerprint(
        dict(reversed(list(ctx.frames.items())))
    )
    base = trial_id("v", ctx)
    assert trial_id("w", ctx) != base
    assert trial_id("v", other_data) != base
    slower = ctx.with_settings(
        ctx.settings.__class__(
            ctx.settings.config.model_copy(update={"execution_delay_bars": 1}),
            ctx.settings.rebalance_threshold,
            ctx.settings.allow_fractional,
            ctx.settings.risk_limits,
        )
    )
    assert trial_id("v", slower) != base


def test_parallel_workers_give_identical_results() -> None:
    ctx = context()
    specs = [spec(5, 20), spec(10, 30)]
    serial = run_trials(specs, ctx, workers=1)
    parallel = run_trials(specs, ctx, workers=2)
    for a, b in zip(serial, parallel, strict=True):
        assert a.trial_id == b.trial_id
        assert (a.returns.to_numpy() == b.returns.to_numpy()).all()


def test_registry_is_append_only_and_counts_distinct_trials(tmp_path: Path) -> None:
    ctx = context()
    outs = run_trials([spec(5, 20), spec(10, 30), spec(30, 20)], ctx)
    reg = TrialRegistry(tmp_path / "r" / "trials.jsonl")
    assert reg.distinct_trials() == 0
    assert reg.record(outs, ctx, "run1", NOW, "grid") == 3
    assert reg.distinct_trials() == 2  # the invalid combination is logged but not counted
    # re-running identical trials adds records, never distinct trials
    assert reg.record(outs[:2], ctx, "run2", NOW, "grid") == 0
    fresh = TrialRegistry(reg.path)
    assert len(fresh.records()) == 5
    assert fresh.distinct_trials(ctx.data_fingerprint, purpose="grid") == 2
    assert fresh.distinct_trials("other-data") == 0
    assert fresh.distinct_trials(purpose="monte_carlo:costs") == 0
    line = json.loads(reg.path.read_text().splitlines()[0])
    assert line["params"] == {"fast": 5, "slow": 20} and line["purpose"] == "grid"


def test_corrupt_registry_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "trials.jsonl"
    p.write_text('{"trial_id": "x"}\n')
    with pytest.raises(AQError, match="corrupt"):
        TrialRegistry(p).records()
