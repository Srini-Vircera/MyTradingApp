"""Backtest report: a self-contained HTML file plus CSV/JSON exports.

Output directory contents::

    report.html      charts + tables (no external assets, no network requests)
    metrics.json     every metric, per segment (all / real / synthetic) and series
    daily.csv        equity, cash, exposures, weights, turnover, costs per session
    fills.csv        every fill with decision time, data time and cost breakdown
    trades.csv       FIFO round trips (net of all costs)
    decisions.csv    every decision: signals, net exposure, target weights, caps
    cancelled.csv    unfilled / trimmed order quantities with reasons

Every page states that the results are hypothetical, and synthetic-data
periods are labelled and reported separately.
"""

from __future__ import annotations

import csv
import html
import json
import math
from decimal import Decimal
from pathlib import Path

import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.quant.analytics.charts import (
    LineSeries,
    column_chart,
    diverging_cell_style,
    fmt,
    line_chart,
    stacked_area_chart,
)
from adaptive_quant.quant.analytics.metrics import (
    daily_returns,
    drawdown_series,
    period_returns,
)
from adaptive_quant.quant.backtest.runner import AnalysedBacktest

ROLLING_WINDOW = 252
SEGMENT_TITLES = {
    "all": "Full period",
    "real": "Real data only",
    "synthetic": "SYNTHETIC data period only",
}
METRIC_ROWS: list[tuple[str, str, str]] = [
    ("total_return", "Total return", "pct"),
    ("cagr", "CAGR", "pct"),
    ("annualized_return", "Annualized return (252d)", "pct"),
    ("volatility", "Volatility (ann.)", "pct"),
    ("downside_volatility", "Downside volatility (ann.)", "pct"),
    ("sharpe", "Sharpe", "ratio"),
    ("sortino", "Sortino", "ratio"),
    ("max_drawdown", "Max drawdown", "pct"),
    ("max_drawdown_duration_sessions", "Longest drawdown (sessions)", "int"),
    ("calmar", "Calmar", "ratio"),
    ("var_95", "VaR 95% (1 day)", "pct"),
    ("es_95", "Expected shortfall 95%", "pct"),
    ("var_99", "VaR 99% (1 day)", "pct"),
    ("es_99", "Expected shortfall 99%", "pct"),
    ("best_month", "Best month", "pct"),
    ("worst_month", "Worst month", "pct"),
    ("winning_months_pct", "Winning months", "pct0"),
    ("best_year", "Best year", "pct"),
    ("worst_year", "Worst year", "pct"),
    ("average_exposure", "Average gross exposure", "pct0"),
    ("time_invested_pct", "Time invested", "pct0"),
    ("annual_turnover", "Turnover (x equity / yr)", "ratio"),
    ("beta", "Beta vs QQQ", "ratio"),
    ("correlation", "Correlation vs QQQ", "ratio"),
    ("excess_cagr", "CAGR minus QQQ CAGR", "pct"),
    ("trades", "Round trips", "int"),
    ("win_rate", "Win rate", "pct0"),
    ("profit_factor", "Profit factor", "ratio"),
    ("average_trade", "Average trade", "pct"),
    ("median_trade", "Median trade", "pct"),
    ("best_trade", "Best trade", "pct"),
    ("worst_trade", "Worst trade", "pct"),
    ("average_holding_days", "Average holding (days)", "ratio"),
]
SERIES_COLORS = ["--series-1", "--series-2", "--series-3", "--series-4", "--series-5"]


def _esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def _cell(value: float | int | None, kind: str) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "-"
    if abs(value) < 5e-7:
        value = 0.0  # never print negative zero
    if kind == "int":
        return f"{int(value):,}"
    if kind == "pct0":
        return f"{value:.0%}"
    if kind == "pct":
        return f"{value:+.2%}"
    return f"{value:.2f}"


def write_report(analysed: AnalysedBacktest, out_dir: Path, title: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_exports(analysed, out_dir)
    path = out_dir / "report.html"
    path.write_text(render_html(analysed, title), encoding="utf-8")
    return path


# ------------------------------------------------------------------ HTML
def render_html(a: AnalysedBacktest, title: str) -> str:
    daily = a.result.daily
    idx = pd.DatetimeIndex(daily.index)
    dates = [f"{t.tz_convert(MARKET_TZ):%Y-%m-%d}" for t in idx]
    equity = daily["equity"]
    bands = _synthetic_bands(a, idx)
    curves = [LineSeries("Strategy", equity.tolist(), SERIES_COLORS[0], label_end=True)]
    for k, b in enumerate(a.benchmarks, start=1):
        curves.append(LineSeries(b.name, b.equity.reindex(idx).tolist(), SERIES_COLORS[k % 5]))
    dd = drawdown_series(equity)
    r = daily_returns(equity).reindex(idx)
    roll_ret = equity / equity.shift(ROLLING_WINDOW) - 1
    roll_sharpe = (r.rolling(126).mean() / r.rolling(126).std()) * math.sqrt(252)
    charts = [
        line_chart(
            "equity",
            "Equity curve",
            "Strategy vs buy-and-hold benchmarks (total return; benchmarks have no costs)",
            dates,
            curves,
            kind="money",
            bands=bands,
        ),
        line_chart(
            "log-equity",
            "Equity curve (log scale)",
            "Equal vertical distance = equal percentage change",
            dates,
            curves,
            kind="money",
            log=True,
            bands=bands,
        ),
        line_chart(
            "drawdown",
            "Drawdown",
            "Decline from the running peak of strategy equity",
            dates,
            [LineSeries("Drawdown", dd.tolist(), SERIES_COLORS[0], area=True)],
            kind="pct",
            zero_line=True,
            bands=bands,
            keep_minima=True,
        ),
        line_chart(
            "rolling-return",
            "Rolling 1-year return",
            f"{ROLLING_WINDOW}-session trailing return",
            dates,
            [LineSeries("Rolling return", _clean(roll_ret), SERIES_COLORS[0])],
            kind="pct",
            zero_line=True,
            bands=bands,
        ),
        line_chart(
            "rolling-sharpe",
            "Rolling Sharpe (126 sessions)",
            "Annualized, zero risk-free rate",
            dates,
            [LineSeries("Rolling Sharpe", _clean(roll_sharpe), SERIES_COLORS[0])],
            kind="ratio",
            zero_line=True,
            bands=bands,
        ),
        line_chart(
            "exposure",
            "Exposure over time",
            "Gross = sum of weights; net = QQQ-equivalent (TQQQ counts 3x, SQQQ -3x)",
            dates,
            [
                LineSeries("Gross exposure", daily["gross_exposure"].tolist(), SERIES_COLORS[0]),
                LineSeries("Net exposure", daily["net_exposure"].tolist(), SERIES_COLORS[1]),
            ],
            kind="ratio",
            zero_line=True,
            bands=bands,
        ),
        stacked_area_chart(
            "allocation",
            "Portfolio allocation over time",
            "Share of equity in each instrument; the rest is cash",
            dates,
            [
                LineSeries(sym, daily[f"w_{sym}"].tolist(), SERIES_COLORS[k])
                for k, sym in enumerate(("QQQ", "TQQQ", "SQQQ"))
            ],
        ),
    ]
    years = period_returns(equity, "Y")
    charts.append(
        column_chart(
            "annual",
            "Annual returns",
            "Strategy, calendar years (first/last years may be partial)",
            [str(y) for y in years.index],
            years.tolist(),
        )
    )
    body = [
        f"<header><h1>{_esc(title)}</h1>{_banner(a)}</header>",
        _summary_tiles(a),
        "<section><h2>Performance</h2>",
        *[_metrics_table(a, seg) for seg in a.metrics],
        "</section>",
        "<section><h2>Charts</h2>",
        *charts,
        "</section>",
        "<section><h2>Monthly returns</h2>",
        _monthly_table(equity),
        "</section>",
        "<section><h2>Annual returns</h2>",
        _annual_table(a),
        "</section>",
        "<section><h2>Costs and trading</h2>",
        _costs_table(a),
        "</section>",
        "<section><h2>Assumptions and provenance</h2>",
        _metadata_table(a),
        _notes(a),
        "</section>",
        "<footer><p class='muted'>Hypothetical results from a simulation. Not investment advice and "
        "not a prediction of future returns. Costs, fills and data quality are modelled assumptions.</p></footer>",
    ]
    return _PAGE.format(title=_esc(title), css=_CSS, js=_JS, body="\n".join(body))


def _clean(s: pd.Series) -> list[float | None]:
    return [None if not math.isfinite(v) else float(v) for v in s.tolist()]


def _synthetic_bands(a: AnalysedBacktest, idx: pd.DatetimeIndex) -> list[tuple[int, int, str]]:
    if "synthetic" not in a.segments:
        return []
    mask = idx.isin(a.segments["synthetic"])
    bands, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        if not m and start is not None:
            bands.append((start, i - 1, "SYNTHETIC data"))
            start = None
    if start is not None:
        bands.append((start, len(mask) - 1, "SYNTHETIC data"))
    return bands


def _banner(a: AnalysedBacktest) -> str:
    items = [
        "<strong>Hypothetical backtest.</strong> Simulated with modelled costs and fills; "
        "not validated against live trading; no performance claim is made."
    ]
    if "synthetic" in a.segments:
        s = a.segments["synthetic"]
        items.append(
            f"<strong>Contains SYNTHETIC price history</strong> ({s[0]:%Y-%m-%d} .. {s[-1]:%Y-%m-%d}); "
            "metrics are also shown separately for real and synthetic periods."
        )
    if a.result.same_bar_close:
        items.append(
            "<strong>Closing-auction execution:</strong> trades at the same close that produced the "
            "signal. This is optimistic and only valid with a genuine closing-auction process."
        )
    return '<div class="banner" role="note">' + "".join(f"<p>{i}</p>" for i in items) + "</div>"


def _summary_tiles(a: AnalysedBacktest) -> str:
    m = a.metrics["all"]["Strategy"]
    tiles = [
        ("CAGR", fmt(m.get("cagr"), "pct")),
        ("Max drawdown", fmt(m.get("max_drawdown"), "pct")),
        ("Sharpe", fmt(m.get("sharpe"), "ratio")),
        ("Final equity", fmt(m.get("end_equity"), "money")),
    ]
    return (
        '<section class="tiles">'
        + "".join(
            f'<div class="tile"><span class="muted">{_esc(k)}</span><strong>{_esc(v)}</strong></div>'
            for k, v in tiles
        )
        + "</section>"
    )


def _metrics_table(a: AnalysedBacktest, segment: str) -> str:
    cols = list(a.metrics[segment])
    head = "".join(f"<th scope='col'>{_esc(c)}</th>" for c in cols)
    rows = []
    for key, label, kind in METRIC_ROWS:
        vals = [a.metrics[segment][c].get(key) for c in cols]
        if all(v is None for v in vals):
            continue
        rows.append(
            f"<tr><th scope='row'>{_esc(label)}</th>"
            + "".join(f"<td>{_cell(v, kind)}</td>" for v in vals)
            + "</tr>"
        )
    idx = a.segments[segment]
    cap = f"{SEGMENT_TITLES[segment]}: {idx[0]:%Y-%m-%d} .. {idx[-1]:%Y-%m-%d}"
    return f'<div class="table-wrap"><table><caption>{_esc(cap)}</caption><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _monthly_table(equity: pd.Series) -> str:
    months = period_returns(equity, "M")
    years = period_returns(equity, "Y")
    grid: dict[int, dict[int, float]] = {}
    for p, v in months.items():
        grid.setdefault(p.year, {})[p.month] = float(v)  # type: ignore[attr-defined]
    head = "".join(
        f"<th scope='col'>{m}</th>"
        for m in (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
            "Year",
        )
    )
    rows = []
    for y in sorted(grid):
        cells = []
        for mth in range(1, 13):
            mv = grid[y].get(mth)
            cells.append(
                f'<td style="{diverging_cell_style(mv)}">{_cell(mv, "pct") if mv is not None else ""}</td>'
            )
        yv = float(years.get(y, float("nan")))
        cells.append(
            f'<td class="year" style="{diverging_cell_style(yv, 0.4)}">{_cell(yv, "pct")}</td>'
        )
        rows.append(f"<tr><th scope='row'>{y}</th>{''.join(cells)}</tr>")
    return (
        f'<div class="table-wrap"><table class="heat"><caption>Strategy monthly returns (blue = gain, red = loss; '
        f"intensity saturates at ±10%/month, ±40%/year)</caption><thead><tr><th></th>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _annual_table(a: AnalysedBacktest) -> str:
    daily = a.result.daily
    series = {"Strategy": daily["equity"]}
    for b in a.benchmarks:
        series[b.name] = b.equity
    table = pd.DataFrame({k: period_returns(v, "Y") for k, v in series.items()})
    head = "".join(f"<th scope='col'>{_esc(c)}</th>" for c in table.columns)
    rows = "".join(
        f"<tr><th scope='row'>{y}</th>"
        + "".join(f"<td>{_cell(float(v), 'pct')}</td>" for v in row)
        + "</tr>"
        for y, row in table.iterrows()
    )
    return f'<div class="table-wrap"><table><thead><tr><th>Year</th>{head}</tr></thead><tbody>{rows}</tbody></table></div>'


def _costs_table(a: AnalysedBacktest) -> str:
    p = a.result.portfolio
    fills = p.fills

    def total(attr: str) -> Decimal:
        return sum((getattr(f, attr) for f in fills), Decimal(0))

    rows = [
        ("Fills", f"{len(fills):,}"),
        ("Traded notional", f"{total('notional'):,.2f}"),
        ("Commissions", f"{total('commission'):,.2f}"),
        ("Spread cost", f"{total('spread_cost'):,.2f}"),
        ("Slippage cost", f"{total('slippage_cost'):,.2f}"),
        ("Market impact cost", f"{total('impact_cost'):,.2f}"),
        (
            "Orders trimmed by participation cap",
            str(sum(1 for f in fills if f.limited_by == "participation")),
        ),
        ("Orders trimmed by available cash", str(sum(1 for f in fills if f.limited_by == "cash"))),
        ("Cancelled / unfilled order quantities", str(len(a.result.cancelled))),
    ]
    body = "".join(f"<tr><th scope='row'>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows)
    return f'<div class="table-wrap"><table><tbody>{body}</tbody></table></div>'


def _metadata_table(a: AnalysedBacktest) -> str:
    body = "".join(
        f"<tr><th scope='row'>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in a.metadata.items()
    )
    return f'<div class="table-wrap"><table class="meta"><tbody>{body}</tbody></table></div>'


def _notes(a: AnalysedBacktest) -> str:
    if not a.notes:
        return ""
    return "<ul class='notes'>" + "".join(f"<li>{_esc(n)}</li>" for n in a.notes) + "</ul>"


# ------------------------------------------------------------------ exports
def _write_exports(a: AnalysedBacktest, out: Path) -> None:
    r = a.result
    daily = r.daily.copy()
    daily.index = pd.DatetimeIndex(daily.index).tz_convert("UTC")
    daily.to_csv(out / "daily.csv", index_label="timestamp_utc")
    (out / "metrics.json").write_text(
        json.dumps(
            {
                "segments": {k: [str(v[0]), str(v[-1])] for k, v in a.segments.items()},
                "metrics": a.metrics,
                "metadata": a.metadata,
                "notes": a.notes,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    _csv(
        out / "fills.csv",
        [
            "order_id",
            "time",
            "decision_time",
            "data_timestamp",
            "symbol",
            "side",
            "requested_quantity",
            "quantity",
            "ref_price",
            "fill_price",
            "notional",
            "commission",
            "spread_cost",
            "slippage_cost",
            "impact_cost",
            "limited_by",
        ],
        [
            [
                f.order_id,
                f.time,
                f.decision_time,
                f.data_timestamp,
                f.symbol,
                f.side.value,
                f.requested_quantity,
                f.quantity,
                f.ref_price,
                f.fill_price,
                f.notional,
                f.commission,
                f.spread_cost,
                f.slippage_cost,
                f.impact_cost,
                f.limited_by,
            ]
            for f in r.fills
        ],
    )
    _csv(
        out / "trades.csv",
        [
            "symbol",
            "quantity",
            "entry_time",
            "exit_time",
            "cost",
            "proceeds",
            "commissions",
            "pnl",
            "return_pct",
            "holding_days",
        ],
        [
            [
                t.symbol,
                t.quantity,
                t.entry_time,
                t.exit_time,
                t.cost,
                t.proceeds,
                t.commissions,
                t.pnl,
                f"{t.return_pct:.6f}",
                f"{t.holding_days:.2f}",
            ]
            for t in r.portfolio.round_trips
        ],
    )
    _csv(
        out / "decisions.csv",
        ["decision_time", "data_timestamp", "net_exposure", "weights", "caps", "signals", "orders"],
        [
            [
                d.decision_time,
                d.data_timestamp,
                f"{d.net_exposure:.6f}",
                json.dumps({k: str(v) for k, v in d.allocation.weights.items()}),
                " | ".join(d.allocation.adjustments),
                json.dumps(
                    {
                        s.strategy_name: [
                            round(s.normalized_score, 6),
                            round(s.suggested_exposure, 6),
                            s.reason,
                        ]
                        for s in d.signals
                    }
                ),
                " ".join(map(str, d.order_ids)),
            ]
            for d in r.decisions
        ],
    )
    _csv(
        out / "cancelled.csv",
        ["order_id", "symbol", "side", "quantity", "reason"],
        [[c.order_id, c.symbol, c.side.value, c.quantity, c.reason] for c in r.cancelled],
    )


def _csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


# ------------------------------------------------------------------ page template
_CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;--text-secondary:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--series-5:#e87ba4;
--pos:#1c5cab;--neg:#d03b3b;--mid:#f0efec;--ink-on-strong:#ffffff;--band:rgba(137,135,129,.14);--warn-bg:#fff4dc;--warn-ink:#5a3d00}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--surface-1:#1a1a19;--page:#0d0d0d;
--text-primary:#ffffff;--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;
--pos:#3987e5;--neg:#e66767;--mid:#383835;--band:rgba(195,194,183,.10);--warn-bg:#3a2b05;--warn-ink:#ffe2a6}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#ffffff;--text-secondary:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;
--series-4:#c98500;--series-5:#d55181;--pos:#3987e5;--neg:#e66767;--mid:#383835;--band:rgba(195,194,183,.10);--warn-bg:#3a2b05;--warn-ink:#ffe2a6}
html,body{margin:0;background:#f9f9f7}
@media (prefers-color-scheme:dark){html:where(:not([data-theme="light"])),html:where(:not([data-theme="light"])) body{background:#0d0d0d}}
html[data-theme="dark"],html[data-theme="dark"] body{background:#0d0d0d}
.viz-root{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text-primary);background:var(--page);
max-width:1000px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 12px}h2{font-size:17px;margin:32px 0 12px}h3{font-size:15px;margin:0}
.muted{color:var(--text-secondary);margin:2px 0 0;font-size:13px}
.banner{background:var(--warn-bg);color:var(--warn-ink);border-radius:8px;padding:10px 14px}.banner p{margin:4px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:16px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column}
.tile strong{font-size:22px;font-weight:600}
.chart{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:14px 14px 8px;margin:0 0 16px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:8px 0 2px;font-size:13px;color:var(--text-secondary)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.key{display:inline-block;width:16px;height:2px;border-radius:1px}.swatch{display:inline-block;width:10px;height:10px;border-radius:2px}
.plot{position:relative}svg{width:100%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}.baseline{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.area{opacity:.10}.stack{opacity:.85;stroke:var(--surface-1);stroke-width:1}
.end-dot{stroke:var(--surface-1);stroke-width:2}.end-label{fill:var(--text-primary);font-size:12px}
.band{fill:var(--band)}.band-label{fill:var(--text-secondary);font-size:11px;font-weight:600}
.crosshair{stroke:var(--axis);stroke-width:1}.hit{fill:transparent;cursor:crosshair}
.col-pos{fill:var(--series-1)}.col-neg{fill:var(--neg)}.col-pos:hover,.col-neg:hover,.col-pos:focus,.col-neg:focus{opacity:.8;outline:none}
.tooltip{position:absolute;top:8px;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);
border-radius:6px;padding:8px 10px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);min-width:150px;z-index:2}
.tooltip .d{color:var(--text-secondary);margin-bottom:4px}.tooltip .r{display:flex;align-items:center;gap:6px;white-space:nowrap}
.tooltip .r b{font-variant-numeric:tabular-nums}.tooltip .r span{color:var(--text-secondary)}
.table-wrap{overflow-x:auto;margin-bottom:16px}
table{border-collapse:collapse;width:100%;background:var(--surface-1);font-size:13px;font-variant-numeric:tabular-nums}
caption{text-align:left;font-weight:600;padding:6px 0;color:var(--text-primary)}
th,td{padding:5px 8px;border-bottom:1px solid var(--grid);text-align:right}th[scope=row]{text-align:left;font-weight:500;color:var(--text-secondary)}
thead th{color:var(--text-secondary);font-weight:600}.heat td{text-align:center;font-size:12px}.meta td{text-align:left;word-break:break-word}
.notes{color:var(--text-secondary)}footer{margin-top:32px}
@media (max-width:600px){.tile strong{font-size:18px}}
"""

_JS = """
document.querySelectorAll('figure.chart').forEach(function(fig){
  var dataEl=fig.querySelector('script.chart-data'); if(!dataEl) return;
  var data=JSON.parse(dataEl.textContent), svg=fig.querySelector('svg'), tip=fig.querySelector('.tooltip');
  var hair=svg.querySelector('.crosshair'), hit=svg.querySelector('.hit'); if(!hit) return;
  var n=data.dates.length;
  function show(evt){
    var pt=svg.createSVGPoint(); pt.x=evt.clientX; pt.y=evt.clientY;
    var p=pt.matrixTransform(svg.getScreenCTM().inverse());
    var f=(p.x-data.x0)/(data.x1-data.x0); var i=Math.max(0,Math.min(n-1,Math.round(f*(n-1))));
    var x=data.x0+(data.x1-data.x0)*(i/Math.max(n-1,1));
    hair.setAttribute('x1',x); hair.setAttribute('x2',x); hair.setAttribute('visibility','visible');
    tip.textContent=''; var d=document.createElement('div'); d.className='d'; d.textContent=data.dates[i]; tip.appendChild(d);
    data.series.forEach(function(s){var r=document.createElement('div'); r.className='r';
      var k=document.createElement('i'); k.className='key'; k.style.background='var('+s.color+')';
      var b=document.createElement('b'); b.textContent=s.values[i]; var l=document.createElement('span'); l.textContent=s.name;
      r.appendChild(k); r.appendChild(b); r.appendChild(l); tip.appendChild(r);});
    tip.hidden=false; var rect=svg.getBoundingClientRect(); var px=(x/svg.viewBox.baseVal.width)*rect.width;
    tip.style.left=(px>rect.width*0.6? px-tip.offsetWidth-12 : px+12)+'px';
  }
  hit.addEventListener('pointermove',show);
  hit.addEventListener('pointerleave',function(){tip.hidden=true; hair.setAttribute('visibility','hidden');});
});
"""

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>{css}</style></head><body><main class="viz-root">
{body}
</main><script>{js}</script></body></html>"""

#: shared page shell for other HTML reports (research, M6)
PAGE_TEMPLATE, PAGE_CSS, PAGE_JS = _PAGE, _CSS, _JS
