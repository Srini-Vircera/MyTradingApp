"""Research report: self-contained HTML plus CSV/JSON exports.

Output directory contents::

    report.html       scorecard, multiple-testing statistics, per-strategy robustness
                      heatmaps, walk-forward (in-sample and out-of-sample shown
                      separately), regimes and Monte Carlo tables
    research.json     every statistic per strategy, plus the cross-candidate tests
    scorecard.csv     ranked candidates with gate results
    trials.csv        every trial of this run (strategy, parameters, full-period Sharpe)
    walkforward.csv   every fold: windows, chosen parameters, IS / validation / OOS Sharpe
    montecarlo.csv    percentile tables (adverse direction, rounded)

All results are hypothetical; nothing in the report is a parameter recommendation.
"""

from __future__ import annotations

import csv
import html
import json
import math
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.quant.analytics.charts import (
    LineSeries,
    column_chart,
    diverging_cell_style,
    line_chart,
)
from adaptive_quant.quant.analytics.report import PAGE_CSS, PAGE_JS, PAGE_TEMPLATE
from adaptive_quant.quant.research import montecarlo as mc
from adaptive_quant.quant.research.pipeline import ResearchResult, StrategyResearch
from adaptive_quant.quant.research.robustness import Coord

MC_TITLES = {
    "trade_sequence": "Trade-sequence bootstrap",
    "return_blocks": "Block-bootstrapped daily returns",
    "start_date": "Random later start dates",
    "parameters": "Parameters in the plateau neighbourhood",
    "costs": "Costs scaled by a random factor (re-simulated)",
    "signal_delay": "Extra signal delay (re-simulated)",
}
MC_METRICS = {
    "cagr": ("CAGR", "pct"),
    "max_drawdown": ("Max drawdown", "pct"),
    "worst_year": ("Worst year", "pct"),
    "sharpe": ("Sharpe", "ratio"),
    "ending_equity": ("Ending equity", "money"),
}
EXTRA_CSS = """
.pass{color:var(--series-3);font-weight:600}.fail{color:var(--neg);font-weight:600}
details{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin:0 0 12px}
summary{cursor:pointer;font-weight:600}.mark{font-weight:700}
.heat th[scope=row]{width:1%;white-space:nowrap;padding-right:16px}
"""


def write_report(result: ResearchResult, out_dir: Path, title: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    _exports(result, out_dir)
    path = out_dir / "report.html"
    path.write_text(render_html(result, title), encoding="utf-8")
    return path


# ------------------------------------------------------------------ formatting
def _esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def _v(value: float | int | None, kind: str) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "-"
    if abs(value) < 5e-7:
        value = 0.0
    if kind == "pct":
        return f"{value:+.1%}"
    if kind == "pct0":
        return f"{value:.0%}"
    if kind == "p":
        return f"{value:.3f}"
    if kind == "int":
        return f"{int(value):,}"
    if kind == "money":
        return f"{value:,.0f}"
    return f"{value:.2f}"


def _ok(passed: bool) -> str:
    return '<span class="pass">pass</span>' if passed else '<span class="fail">FAIL</span>'


def _table(caption: str, head: list[str], rows: list[list[str]], cls: str = "") -> str:
    th = "".join(f'<th scope="col">{_esc(h)}</th>' for h in head)
    body = "".join(
        "<tr>" + f'<th scope="row">{r[0]}</th>' + "".join(f"<td>{c}</td>" for c in r[1:]) + "</tr>"
        for r in rows
    )
    return (
        f'<div class="table-wrap"><table class="{cls}"><caption>{_esc(caption)}</caption>'
        f"<thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _dates(idx: pd.Index) -> list[str]:
    return [f"{pd.Timestamp(t).tz_convert(MARKET_TZ):%Y-%m-%d}" for t in idx]


# ------------------------------------------------------------------ page
def render_html(r: ResearchResult, title: str) -> str:
    passed = sum(1 for sc in r.ranked if sc.passed)
    rc = r.reality_check
    tiles = [
        ("Candidates", str(len(r.strategies))),
        ("Trials this run", f"{r.trials_this_run:,}"),
        ("Trials on this data (all runs)", f"{r.trials_registered:,}"),
        ("Passing every gate", f"{passed} of {len(r.ranked)}"),
        ("Reality Check p (best vs QQQ)", _v(rc.reality_check_p, "p") if rc else "-"),
        ("PBO, all trials", _v(r.global_pbo.pbo, "pct0") if r.global_pbo else "-"),
    ]
    body = [
        f"<header><h1>{_esc(title)}</h1>{_banner(r)}</header>",
        '<div class="tiles">'
        + "".join(
            f'<div class="tile"><span class="muted">{_esc(k)}</span><strong>{_esc(v)}</strong></div>'
            for k, v in tiles
        )
        + "</div>",
        "<section><h2>Ranking scorecard</h2>",
        "<p class='muted'>Percentile-rank score over out-of-sample risk-adjusted metrics, robustness, "
        "consistency, diversification and trading costs. CAGR is not a criterion. Gates are pass/fail; "
        "a candidate failing any gate can never be marked validated.</p>",
        _scorecard(r),
        "</section>",
        "<section><h2>Multiple-testing controls</h2>",
        _multiple_testing(r),
        "</section>",
        "<section><h2>Strategies</h2>",
        *[_strategy(s, r) for s in r.strategies],
        "</section>",
        "<section><h2>Method, assumptions and provenance</h2>",
        _provenance(r),
        "</section>",
        "<footer><p class='muted'>Hypothetical research results from simulations. Not investment advice and not "
        "a prediction of future returns. No parameter shown here has been adopted; the lifecycle in "
        "strategies.yaml is changed only by a human.</p></footer>",
    ]
    css = PAGE_CSS + EXTRA_CSS
    return PAGE_TEMPLATE.format(title=_esc(title), css=css, js=PAGE_JS, body="\n".join(body))


def _banner(r: ResearchResult) -> str:
    parts = [
        "<p><strong>HYPOTHETICAL RESEARCH.</strong> Simulated fills and modelled costs. Every configuration tried "
        "is counted in the trial registry and deflates the statistics below.</p>",
        f"<p>Period {r.start} .. {r.end} ({r.sessions:,} sessions); run {_esc(r.run_id)}; config "
        f"{_esc(r.config_version)}; data {_esc(r.data_fingerprint)}.</p>",
    ]
    if r.synthetic_sessions:
        parts.append(
            f"<p><strong>Includes SYNTHETIC data</strong> ({r.synthetic_sessions:,} sessions). The real-data gate "
            "fails for any out-of-sample period that contains them.</p>"
        )
    return f'<div class="banner" role="note">{"".join(parts)}</div>'


def _scorecard(r: ResearchResult) -> str:
    rows = []
    for k, sc in enumerate(r.ranked, 1):
        e = sc.evidence
        study = next(s for s in r.strategies if s.strategy_id == e.strategy_id)
        failed = [g.name for g in sc.gates if not g.passed]
        change = "-"
        if study.transition is not None:
            change = f"{study.transition.from_state.value} &rarr; {study.transition.to_state.value}"
        rows.append(
            [
                f"{k}. {_esc(e.strategy_id)}",
                _esc(e.params_label),
                f"{sc.score:.2f}",
                _v(e.oos_sharpe, "ratio"),
                _v(e.oos_benchmark_sharpe, "ratio"),
                _v(e.oos_max_drawdown, "pct"),
                _v(e.oos_calmar, "ratio"),
                _v(e.robustness_score, "ratio"),
                _v(e.dsr, "p"),
                _v(e.pbo, "pct0"),
                _v(e.bh_adjusted_p, "p"),
                _ok(sc.passed)
                + (
                    "" if sc.passed else f"<br><span class='muted'>{_esc(', '.join(failed))}</span>"
                ),
                change,
            ]
        )
    return _table(
        "Candidates (gate-passing first, then by score)",
        [
            "Strategy",
            "Plateau-centre params",
            "Score",
            "OOS Sharpe",
            "QQQ OOS Sharpe",
            "OOS max DD",
            "OOS Calmar",
            "Robustness",
            "DSR",
            "PBO",
            "BH p",
            "Gates",
            "Automated lifecycle change",
        ],
        rows,
    )


def _multiple_testing(r: ResearchResult) -> str:
    rows = []
    for s in r.strategies:
        if s.dsr is None:
            continue
        ci = s.sharpe_ci
        e = s.scored.evidence if s.scored else None
        rows.append(
            [
                _esc(s.strategy_id),
                _v(s.dsr.sharpe * math.sqrt(252), "ratio"),
                _v(s.dsr.sr0 * math.sqrt(252), "ratio"),
                _v(s.dsr.dsr, "p"),
                _v(s.pbo.pbo if s.pbo else None, "pct0"),
                _v(s.p_value, "p"),
                _v(e.bh_adjusted_p if e else None, "p"),
                "-" if ci is None else f"{_v(ci.lower, 'ratio')} .. {_v(ci.upper, 'ratio')}",
                "-"
                if s.cagr_ci is None
                else f"{_v(s.cagr_ci.lower, 'pct')} .. {_v(s.cagr_ci.upper, 'pct')}",
                "-"
                if s.max_dd_ci is None
                else f"{_v(s.max_dd_ci.lower, 'pct')} .. {_v(s.max_dd_ci.upper, 'pct')}",
            ]
        )
    out = [
        _table(
            "Per strategy (DSR and PBO use full-period returns; p-values and intervals use OOS returns)",
            [
                "Strategy",
                "Sharpe (full, plateau centre)",
                "Expected max Sharpe of null (SR0)",
                "Deflated Sharpe",
                "PBO over its grid",
                "p (Sharpe <= 0)",
                "BH-adjusted p",
                "OOS Sharpe 95% CI",
                "OOS CAGR 95% CI",
                "OOS max DD 95% CI",
            ],
            rows,
        )
    ]
    g = []
    if r.reality_check:
        rc = r.reality_check
        g += [
            ["White's Reality Check p", _v(rc.reality_check_p, "p")],
            ["Hansen SPA p (consistent)", _v(rc.spa_p, "p")],
            ["Best trial vs QQQ", _esc(rc.best)],
            ["Best mean daily excess vs QQQ", f"{rc.best_mean_excess * 1e4:+.2f} bps"],
            ["Models tested / bootstrap samples", f"{rc.n_models:,} / {rc.samples:,}"],
        ]
    if r.global_pbo:
        p = r.global_pbo
        g += [
            ["PBO across all trials (CSCV)", _v(p.pbo, "pct0")],
            ["CSCV combinations (partitions)", f"{p.n_combinations:,} ({p.partitions})"],
            ["OOS vs IS Sharpe slope of the IS-best trial", _v(p.degradation_slope, "ratio")],
            ["P(IS-best trial loses OOS)", _v(p.prob_oos_loss, "pct0")],
        ]
    g += [
        ["Trials in this run", f"{r.trials_this_run:,}"],
        ["Distinct trials on this data (all runs; DSR N)", f"{r.trials_registered:,}"],
        ["Variance of trial Sharpe (per period)", f"{r.var_trial_sharpe:.3g}"],
    ]
    out.append(_table("Across all trials", ["Test", "Value"], g, "meta"))
    if r.global_pbo:
        counts, edges = _histogram(list(r.global_pbo.logits))
        out.append(
            column_chart(
                "pbo-logits",
                "Distribution of CSCV logits (all trials)",
                "Share of combinations per bin; logit <= 0 means the in-sample winner was at or below the "
                "out-of-sample median",
                [f"{a:+.1f}" for a in edges],
                counts,
            )
        )
    return "".join(out)


def _histogram(values: list[float], bins: int = 12) -> tuple[list[float], list[float]]:
    if not values:
        return [], []
    lo, hi = min(values), max(values)
    width = (hi - lo) / bins if hi > lo else 1.0
    counts = [0] * bins
    for v in values:
        counts[min(int((v - lo) / width), bins - 1)] += 1
    return [c / len(values) for c in counts], [lo + width * (i + 0.5) for i in range(bins)]


def _strategy(s: StrategyResearch, r: ResearchResult) -> str:
    head = f"<summary>{_esc(s.strategy_id)} <span class='muted'>({_esc(s.version.family.value)})</span></summary>"
    if s.error:
        return f"<details>{head}<p class='fail'>{_esc(s.error)}</p>{_invalid(s)}</details>"
    parts = [head, _robustness(s), _walk_forward(s, r), _regimes(s), _monte_carlo(s), _invalid(s)]
    return f"<details>{''.join(parts)}</details>"


def _robustness(s: StrategyResearch) -> str:
    rb = s.robustness
    if rb is None:
        return ""
    g = s.grid
    summary = _table(
        "Parameter robustness (metric: full-period annualised Sharpe)",
        ["Measure", "Value"],
        [
            ["Grid points (valid)", f"{rb.n_points} ({rb.n_valid})"],
            ["Best point", _esc(g.label(rb.best))],
            ["Best Sharpe", _v(rb.best_metric, "ratio")],
            ["Neighbour median / best", _v(rb.plateau_ratio, "ratio")],
            ["Dispersion", _v(rb.dispersion, "ratio")],
            ["Neighbours <= 0", _v(rb.negative_share, "pct0")],
            ["Robustness score", _v(rb.score, "ratio")],
            ["Potentially overfit", "YES" if rb.potentially_overfit else "no"],
            ["Plateau centre (research choice)", _esc(g.label(rb.plateau_centre))],
        ],
        "meta",
    )
    return "<h3>Parameter sensitivity</h3>" + summary + "".join(_heatmaps(s))


def _heatmaps(s: StrategyResearch) -> list[str]:
    g, rb = s.grid, s.robustness
    if rb is None or not g.dims:
        return []
    m = s.sharpe_grid
    marks = {rb.best: "&#9679;", rb.plateau_centre: "&#9670;"}
    if rb.best == rb.plateau_centre:
        marks = {rb.best: "&#9679;&#9670;"}

    def cell(c: Coord) -> str:
        v = m.get(c)
        style = diverging_cell_style(v, scale=1.5)
        mark = marks.get(c, "")
        return f'<td style="{style}">{_v(v, "ratio")} <span class="mark">{mark}</span></td>'

    legend = "<p class='muted'>&#9679; best point &nbsp; &#9670; plateau centre (neighbourhood median).</p>"
    if len(g.dims) == 1:
        head = [g.dims[0], *[str(v) for v in g.values[0]]]
        cells = [cell((i,)) for i in range(g.shape[0])]
        return [_raw_heat(f"Sharpe by {g.dims[0]}", head, [("Sharpe", cells)]), legend]
    out = []
    fixed = [(k, i) for k, i in enumerate(rb.plateau_centre) if k >= 2]
    slices: list[tuple[int, ...]] = [tuple(i for _, i in fixed)]
    if len(g.dims) == 3:
        slices = [(i,) for i in range(g.shape[2])]
    for sl in slices:
        extra = ", ".join(f"{g.dims[2 + k]}={g.values[2 + k][i]}" for k, i in enumerate(sl))
        caption = f"Sharpe: {g.dims[0]} (rows) x {g.dims[1]} (columns)" + (
            f" at {extra}" if extra else ""
        )
        head = [f"{g.dims[0]} \\ {g.dims[1]}", *[str(v) for v in g.values[1]]]
        rows = [
            (str(g.values[0][i]), [cell((i, j, *sl)) for j in range(g.shape[1])])
            for i in range(g.shape[0])
        ]
        out.append(_raw_heat(caption, head, rows))
    out.append(legend)
    return out


def _raw_heat(caption: str, head: list[str], rows: list[tuple[str, list[str]]]) -> str:
    th = "".join(f'<th scope="col">{_esc(h)}</th>' for h in head)
    body = "".join(
        f'<tr><th scope="row">{_esc(label)}</th>{"".join(cells)}</tr>' for label, cells in rows
    )
    return (
        f'<div class="table-wrap"><table class="heat"><caption>{_esc(caption)}</caption>'
        f"<thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _walk_forward(s: StrategyResearch, r: ResearchResult) -> str:
    wf = s.walk_forward
    if wf is None:
        return ""
    st = wf.settings
    rows = [
        [
            str(f.window.fold + 1),
            f"{f.dates[0]} .. {f.dates[1]}",
            f"{f.dates[2]} .. {f.dates[3]}",
            f"{f.dates[4]} .. {f.dates[5]}",
            _esc(f.label),
            _v(f.train_sharpe, "ratio"),
            _v(f.validate_sharpe, "ratio"),
            _v(f.test_sharpe, "ratio"),
            _v(f.test_return, "pct"),
            _v(f.test_max_drawdown, "pct"),
        ]
        for f in wf.folds
    ]
    out = [
        f"<h3>Walk-forward ({_esc(st.scheme)}: train {st.train_years:g}y, validate {st.validate_years:g}y, "
        f"test {st.test_years:g}y, step {st.step_years:g}y)</h3>",
        _table(
            "Folds: parameters chosen on train/validate only; the test window is out-of-sample",
            [
                "Fold",
                "Train (IS)",
                "Validate",
                "Test (OOS)",
                "Chosen",
                "IS Sharpe",
                "Validation Sharpe",
                "OOS Sharpe",
                "OOS return",
                "OOS max DD",
            ],
            rows,
        ),
        _table(
            "In-sample vs out-of-sample",
            ["Measure", "Value"],
            [
                ["Mean train (IS) Sharpe", _v(wf.mean_train_sharpe, "ratio")],
                ["Mean validation Sharpe", _v(wf.mean_validate_sharpe, "ratio")],
                ["Stitched OOS Sharpe", _v(wf.oos_sharpe, "ratio")],
                ["OOS / IS Sharpe (decay ratio)", _v(wf.decay_ratio, "ratio")],
                ["Parameter changes between folds", str(wf.parameter_changes)],
                ["OOS sessions", f"{len(wf.oos_returns):,}"],
            ],
            "meta",
        ),
    ]
    if s.oos_equity is not None and len(s.oos_equity) > 2:
        idx = s.oos_equity.index
        dates = _dates(idx)
        bands = []
        offset = 1  # position 0 is the session before the first OOS return
        for f in wf.folds:
            n = f.window.test[1] - f.window.test[0]
            if f.window.fold % 2 == 0:
                bands.append((offset, offset + n - 1, f"fold {f.window.fold + 1}"))
            offset += n
        series = [
            LineSeries("Walk-forward OOS", s.oos_equity.tolist(), "--series-1", label_end=True)
        ]
        if r.benchmark_equity is not None:
            b_eq = r.benchmark_equity.reindex(idx)
            if b_eq.notna().all():
                series.append(
                    LineSeries(
                        "QQQ buy-and-hold",
                        (b_eq / b_eq.iloc[0] * s.oos_equity.iloc[0]).tolist(),
                        "--series-2",
                    )
                )
        out.append(
            line_chart(
                f"wf-{s.strategy_id}",
                "OUT-OF-SAMPLE: stitched walk-forward equity",
                "Only test-window returns of parameters chosen before each window; shaded bands mark alternate folds",
                dates,
                series,
                kind="money",
                bands=bands,
            )
        )
    chosen = s.selected_outcome
    if chosen is not None:
        out.append(
            line_chart(
                f"is-{s.strategy_id}",
                "IN-SAMPLE (hindsight): full-period equity of the plateau-centre parameters",
                "Parameters chosen with knowledge of the whole period - not an out-of-sample result",
                _dates(chosen.equity.index),
                [
                    LineSeries(
                        "In-sample (hindsight)",
                        chosen.equity.tolist(),
                        "--series-4",
                        label_end=True,
                    )
                ],
                kind="money",
            )
        )
    return "".join(out)


def _regimes(s: StrategyResearch) -> str:
    if s.regimes is None:
        return ""
    rows = [
        [
            _esc(f"{st.dimension}: {st.regime}"),
            f"{st.sessions:,}",
            _v(st.annual_return, "pct"),
            _v(st.sharpe, "ratio"),
        ]
        for st in s.regimes.stats
    ]
    rows.append(
        [
            "Consistency (share of buckets with a positive return)",
            "",
            _v(s.regimes.consistency, "pct0"),
            "",
        ]
    )
    return "<h3>Regime and decade consistency (OOS returns)</h3>" + _table(
        "Regime labels use only data before each session",
        ["Regime", "Sessions", "Annualised return", "Sharpe"],
        rows,
    )


def _monte_carlo(s: StrategyResearch) -> str:
    if not s.monte_carlo:
        return ""
    rows = []
    for dim, tab in s.monte_carlo.items():
        for metric, (name, kind) in MC_METRICS.items():
            d = tab.get(metric)
            if d is None or d.n == 0:
                continue
            vals = [mc.rounded(metric, x) for x in (d.median, d.p75, d.p90, d.p95, d.worst)]
            rows.append(
                [_esc(f"{MC_TITLES.get(dim, dim)}: {name}"), str(d.n), *[_v(v, kind) for v in vals]]
            )
    return "<h3>Monte Carlo (plateau-centre parameters, full period)</h3>" + _table(
        "Adverse percentiles: p90 = the value 90% of simulations beat. Rounded to avoid false precision.",
        ["Dimension: metric", "n", "Median", "p75", "p90", "p95", "Worst"],
        rows,
    )


def _invalid(s: StrategyResearch) -> str:
    if not s.invalid:
        return ""
    items = "".join(f"<li>{_esc(label)}: {_esc(err)}</li>" for label, err in s.invalid)
    return f"<p class='muted'>Parameter combinations not run ({len(s.invalid)}):</p><ul class='notes'>{items}</ul>"


def _provenance(r: ResearchResult) -> str:
    rows = [
        ["Run id", _esc(r.run_id)],
        ["Created (UTC)", _esc(r.created_at.isoformat())],
        ["Config version", _esc(r.config_version)],
        ["Data fingerprint", _esc(r.data_fingerprint)],
        ["Initial capital", f"{r.initial_capital:,.0f}"],
        [
            "Walk-forward approximation",
            "each trial is one continuous backtest; parameter switches at fold boundaries are not simulated as trades",
        ],
        ["Deflated Sharpe N", "distinct grid trials recorded on this data across all runs"],
        [
            "Selection rule",
            "plateau centre (highest neighbourhood-median Sharpe), never the raw best point",
        ],
    ]
    notes = "".join(f"<li>{_esc(n)}</li>" for n in r.notes)
    return _table("Provenance", ["Item", "Value"], rows, "meta") + (
        f"<ul class='notes'>{notes}</ul>" if notes else ""
    )


# ------------------------------------------------------------------ exports
def _exports(r: ResearchResult, out: Path) -> None:
    summary: dict[str, object] = {
        "run_id": r.run_id,
        "hypothetical": True,
        "config_version": r.config_version,
        "data_fingerprint": r.data_fingerprint,
        "period": [r.start, r.end],
        "trials_this_run": r.trials_this_run,
        "trials_registered": r.trials_registered,
        "synthetic_sessions": r.synthetic_sessions,
        "reality_check": None if r.reality_check is None else r.reality_check.__dict__,
        "global_pbo": None
        if r.global_pbo is None
        else {k: v for k, v in r.global_pbo.__dict__.items() if k != "logits"},
        "notes": r.notes,
        "strategies": [_strategy_json(s) for s in r.strategies],
    }
    (out / "research.json").write_text(
        json.dumps(_clean(summary), indent=2, sort_keys=True), encoding="utf-8"
    )
    _csv(
        out / "scorecard.csv",
        [
            "rank",
            "strategy_id",
            "version_id",
            "params",
            "score",
            "passed",
            *[g.name for g in r.ranked[0].gates],
        ]
        if r.ranked
        else ["rank"],
        [
            [
                k,
                sc.evidence.strategy_id,
                sc.evidence.version_id,
                sc.evidence.params_label,
                round(sc.score, 4),
                sc.passed,
            ]
            + [g.passed for g in sc.gates]
            for k, sc in enumerate(r.ranked, 1)
        ],
    )
    _csv(
        out / "trials.csv",
        ["strategy_id", "params", "version_id", "trial_id", "sharpe_full_period", "status"],
        [
            [s.strategy_id, o.spec.label, o.version_id, o.trial_id, _r(s.sharpe_grid.get(c)), "ok"]
            for s in r.strategies
            for c, o in sorted(s.outcomes.items())
        ]
        + [
            [s.strategy_id, label, "", "", "", f"not run: {err}"]
            for s in r.strategies
            for label, err in s.invalid
        ],
    )
    _csv(
        out / "walkforward.csv",
        [
            "strategy_id",
            "fold",
            "train_start",
            "train_end",
            "validate_start",
            "validate_end",
            "test_start",
            "test_end",
            "chosen",
            "is_sharpe",
            "validate_sharpe",
            "oos_sharpe",
            "oos_return",
            "oos_max_drawdown",
        ],
        [
            [
                s.strategy_id,
                f.window.fold + 1,
                *f.dates,
                f.label,
                _r(f.train_sharpe),
                _r(f.validate_sharpe),
                _r(f.test_sharpe),
                _r(f.test_return),
                _r(f.test_max_drawdown),
            ]
            for s in r.strategies
            if s.walk_forward
            for f in s.walk_forward.folds
        ],
    )
    _csv(
        out / "montecarlo.csv",
        ["strategy_id", "dimension", "metric", "n", "median", "p75", "p90", "p95", "worst"],
        [
            [
                s.strategy_id,
                dim,
                metric,
                d.n,
                *[mc.rounded(metric, x) for x in (d.median, d.p75, d.p90, d.p95, d.worst)],
            ]
            for s in r.strategies
            for dim, tab in s.monte_carlo.items()
            for metric, d in tab.items()
        ],
    )


def _strategy_json(s: StrategyResearch) -> dict[str, object]:
    out: dict[str, object] = {"strategy_id": s.strategy_id, "error": s.error, "invalid": s.invalid}
    if s.robustness:
        out["robustness"] = {
            **s.robustness.__dict__,
            "best": s.grid.label(s.robustness.best),
            "plateau_centre": s.grid.label(s.robustness.plateau_centre),
        }
    if s.walk_forward:
        wf = s.walk_forward
        out["walk_forward"] = {
            "mean_train_sharpe": wf.mean_train_sharpe,
            "mean_validate_sharpe": wf.mean_validate_sharpe,
            "oos_sharpe": wf.oos_sharpe,
            "decay_ratio": wf.decay_ratio,
            "oos_sessions": len(wf.oos_returns),
        }
    out["oos_metrics"] = s.oos_metrics
    out["full_period_metrics_in_sample"] = s.full_metrics
    if s.dsr:
        out["deflated_sharpe"] = s.dsr.__dict__
    if s.pbo:
        out["pbo"] = {k: v for k, v in s.pbo.__dict__.items() if k != "logits"}
    for name in ("sharpe_ci", "cagr_ci", "max_dd_ci"):
        ci = getattr(s, name)
        if ci is not None:
            out[name] = ci.__dict__
    out["p_value_sharpe_le_0"] = s.p_value
    if s.regimes:
        out["regime_consistency"] = s.regimes.consistency
    if s.scored:
        out["score"] = s.scored.score
        out["gates"] = [g.__dict__ for g in s.scored.gates]
    if s.transition:
        out["automated_transition"] = (
            f"{s.transition.from_state.value} -> {s.transition.to_state.value}"
        )
    return out


def _clean(o: object) -> object:
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_clean(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, int | str | bool) or o is None:
        return o
    return str(o)


def _r(v: float | None) -> float | str:
    return "" if v is None or not math.isfinite(v) else round(v, 6)


def _csv(path: Path, header: list[str], rows: Sequence[Sequence[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
