"""End-to-end research pipeline on generated prices: determinism, gates, governance, report."""

import csv
import hashlib
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from adaptive_quant.core.enums import StrategyLifecycle as L
from adaptive_quant.governance.research import GovernanceLedger
from adaptive_quant.quant.research.pipeline import ResearchResult, research_candidates, run_research
from adaptive_quant.quant.research.report import write_report
from adaptive_quant.quant.research.trials import TrialRegistry
from adaptive_quant.quant.strategies.catalog import StrategyCatalog, StrategyVersion
from tests.conftest import REPO_CONFIG
from tests.unit.backtest.helpers import SETTINGS
from tests.unit.research.helpers import context, small_config, version

NOW = datetime(2024, 1, 2, 12, tzinfo=UTC)
LENIENT = {
    "dsr_min": 0.0,
    "pbo_max": 1.0,
    "robustness_min": 0.0,
    "oos_sharpe_min": -100.0,
    "fdr_q": 1.0,
    "min_oos_sessions": 21,
    "min_sharpe_vs_benchmark": -100.0,
}


def versions() -> list[StrategyVersion]:
    return [
        version("st_ema_cross", {"fast": [5, 10, 25], "slow": [20, 30]}),  # 25/20 is invalid
        version("it_ma_stack", {"fast": [10, 20], "mid": [40], "slow": [100]}),
    ]


def run(tmp: Path, cfg=None, ctx=None, ledger: bool = True) -> ResearchResult:  # type: ignore[no-untyped-def]
    return run_research(
        versions(),
        ctx or context(),
        cfg or small_config(),
        TrialRegistry(tmp / "trials.jsonl"),
        GovernanceLedger(tmp / "gov.jsonl") if ledger else None,
        now=NOW,
        config_version="test-cfg",
    )


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> ResearchResult:
    return run(tmp_path_factory.mktemp("research"))


def test_every_trial_is_run_and_registered(result: ResearchResult, tmp_path: Path) -> None:
    assert result.trials_this_run == 5 + 2  # one invalid st_ema_cross combination
    ema = next(s for s in result.strategies if s.strategy_id == "st_ema_cross")
    assert len(ema.outcomes) == 5 and len(ema.invalid) == 1
    assert "fast < slow" in ema.invalid[0][1]
    assert result.trials_registered == 7
    assert result.var_trial_sharpe > 0
    assert result.reality_check is not None and result.reality_check.n_models == 7
    assert result.global_pbo is not None and 0.0 <= result.global_pbo.pbo <= 1.0


def test_each_candidate_has_the_full_evidence(result: ResearchResult) -> None:
    for s in result.strategies:
        assert s.error is None
        assert s.robustness is not None and s.selected == s.robustness.plateau_centre
        wf = s.walk_forward
        assert wf is not None and len(wf.folds) == 5  # 4 full test windows + a partial one
        assert len(wf.oos_returns) == sum(f.window.test[1] - f.window.test[0] for f in wf.folds)
        assert s.dsr is not None and s.dsr.n_trials == 7
        assert s.pbo is not None
        assert s.sharpe_ci is not None and s.sharpe_ci.lower <= s.sharpe_ci.upper
        assert s.regimes is not None
        assert s.scored is not None and s.record is not None
        assert {"trade_sequence", "return_blocks", "start_date", "parameters"} <= set(s.monte_carlo)
        assert s.oos_equity is not None and len(s.oos_equity) == len(wf.oos_returns) + 1
    top = result.ranked[0].evidence.strategy_id
    top_study = next(s for s in result.strategies if s.strategy_id == top)
    assert {"costs", "signal_delay"} <= set(top_study.monte_carlo)  # engine MC for the top_n only
    assert top_study.monte_carlo["signal_delay"]["cagr"].n == 2


def test_research_is_deterministic(result: ResearchResult, tmp_path: Path) -> None:
    again = run(tmp_path)
    assert [sc.evidence.strategy_id for sc in again.ranked] == [
        sc.evidence.strategy_id for sc in result.ranked
    ]
    for a, b in zip(result.strategies, again.strategies, strict=True):
        assert a.selected == b.selected
        assert a.dsr == b.dsr and a.pbo == b.pbo and a.sharpe_ci == b.sharpe_ci
        assert a.monte_carlo == b.monte_carlo
        assert [f.chosen for f in a.walk_forward.folds] == [f.chosen for f in b.walk_forward.folds]  # type: ignore[union-attr]


def test_repeated_runs_do_not_inflate_the_trial_count(tmp_path: Path) -> None:
    first = run(tmp_path, ledger=False)
    second = run(tmp_path, ledger=False)
    assert first.trials_registered == second.trials_registered == 7
    lines = (tmp_path / "trials.jsonl").read_text().splitlines()
    grid = [json.loads(x) for x in lines if json.loads(x)["purpose"] == "grid"]
    assert len(grid) == 2 * 8  # every attempt is logged, including the invalid combination


def test_synthetic_oos_can_never_validate(tmp_path: Path) -> None:
    cfg = small_config(gates=SETTINGS.research.gates.model_copy(update=LENIENT))
    res = run(tmp_path, cfg=cfg, ctx=context(synthetic_before=date(2018, 6, 1)))
    assert res.synthetic_sessions > 0
    assert any("SYNTHETIC" in n for n in res.notes)
    for sc in res.ranked:
        gate = {g.name: g.passed for g in sc.gates}
        assert gate["real_data"] is False and not sc.passed
    assert all(s.transition is None for s in res.strategies)
    assert GovernanceLedger(tmp_path / "gov.jsonl").validated_versions() == {}


def test_automated_promotion_stops_at_validated_and_can_be_reversed(tmp_path: Path) -> None:
    yaml_before = hashlib.sha256((REPO_CONFIG / "strategies.yaml").read_bytes()).hexdigest()
    lenient = small_config(gates=SETTINGS.research.gates.model_copy(update=LENIENT))
    res = run(tmp_path, cfg=lenient)
    changes = [s.transition for s in res.strategies if s.transition is not None]
    assert changes and all(t.to_state is L.VALIDATED for t in changes)
    ledger = GovernanceLedger(tmp_path / "gov.jsonl")
    assert set(ledger.validated_versions()) == {t.strategy_id for t in changes}
    # the same versions re-researched under strict gates are demoted - never promoted further
    strict = small_config(gates=SETTINGS.research.gates.model_copy(update={"dsr_min": 1.0}))
    res2 = run(tmp_path, cfg=strict)
    for s in res2.strategies:
        if s.transition is not None:
            assert (s.transition.from_state, s.transition.to_state) == (L.VALIDATED, L.RESEARCH)
    assert ledger.validated_versions() == {}
    # software never edits strategies.yaml; the configured lifecycle stays "research"
    assert hashlib.sha256((REPO_CONFIG / "strategies.yaml").read_bytes()).hexdigest() == yaml_before
    assert all(v.lifecycle is L.RESEARCH for v in versions())


def test_research_candidates_exclude_benchmarks_and_disabled() -> None:
    all_versions = [e.version for e in StrategyCatalog.from_config(SETTINGS.strategies).entries]
    cands = research_candidates(all_versions)
    ids = {v.strategy_id for v in cands}
    assert "always_long_qqq" not in ids and "always_cash" not in ids
    assert len(cands) == 20


def test_report_and_exports(result: ResearchResult, tmp_path: Path) -> None:
    path = write_report(result, tmp_path / "out", "Research test")
    html = path.read_text()
    for text in (
        "HYPOTHETICAL RESEARCH",
        "Ranking scorecard",
        "CAGR is not a criterion",
        "OUT-OF-SAMPLE: stitched walk-forward equity",
        "IN-SAMPLE (hindsight)",
        "Deflated Sharpe",
        "Reality Check p",
        "Monte Carlo",
        "Parameter combinations not run",
        "plateau centre",
    ):
        assert text in html, text
    assert "http://" not in html and "https://" not in html
    for name in (
        "research.json",
        "scorecard.csv",
        "trials.csv",
        "walkforward.csv",
        "montecarlo.csv",
    ):
        assert (tmp_path / "out" / name).exists()
    data = json.loads((tmp_path / "out" / "research.json").read_text())
    assert data["hypothetical"] is True and data["trials_this_run"] == 7
    with (tmp_path / "out" / "walkforward.csv").open() as fh:
        wf_rows = list(csv.reader(fh))
    assert len(wf_rows) > 1 and all(len(row) == len(wf_rows[0]) for row in wf_rows)
    trials = (tmp_path / "out" / "trials.csv").read_text().splitlines()
    assert len(trials) == 1 + 7 + 1 and "not run" in trials[-1]  # header, 7 ok, 1 invalid
    for s in data["strategies"]:
        for v in s.get("deflated_sharpe", {}).values():
            assert not (isinstance(v, float) and math.isnan(v))
