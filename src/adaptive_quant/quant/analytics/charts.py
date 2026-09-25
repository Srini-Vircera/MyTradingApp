"""Dependency-free SVG charts for the HTML backtest report.

Design rules (dataviz method, reference palette):
* one y-axis per chart; hairline recessive grid; 2px lines; 10 % area washes;
* categorical colors are CSS custom properties (``--series-N``) in fixed
  order, with light/dark values defined once in the report stylesheet;
* a legend for >= 2 series; direct end-label only for the lead series;
* a crosshair tooltip on every time chart (``report.js``), fed from embedded
  JSON - every value is also available in a table view;
* synthetic-data periods are drawn as a labelled band, never hidden.
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

W, H = 920, 300
ML, MR, MT, MB = 64, 96, 18, 30
MAX_POINTS = 1200


@dataclass(frozen=True)
class LineSeries:
    name: str
    values: Sequence[float | None]
    color: str  # CSS variable name, e.g. "--series-1"
    area: bool = False
    label_end: bool = False


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def downsample(n: int, reduce_min: Sequence[float | None] | None = None) -> list[int]:
    """Indices to plot: every k-th point (+ the last); keeps bucket minima if given."""
    if n <= MAX_POINTS:
        return list(range(n))
    k = math.ceil(n / MAX_POINTS)
    idx: list[int] = []
    for start in range(0, n, k):
        bucket = range(start, min(start + k, n))
        if reduce_min is not None:
            vals = [(reduce_min[i], i) for i in bucket if reduce_min[i] is not None]
            idx.append(min(vals)[1] if vals else start)
        else:
            idx.append(start)
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    if not math.isfinite(lo) or not math.isfinite(hi):
        return [0.0]
    if hi <= lo:
        hi = lo + (abs(lo) or 1.0)
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step) * step
    ticks, t = [], first
    while t <= hi + step * 0.5:
        ticks.append(round(t, 12))
        t += step
    return ticks


def log_ticks(lo: float, hi: float) -> list[float]:
    ticks: list[float] = []
    e = math.floor(math.log10(lo))
    while 10**e <= hi * 10:
        for m in (1, 2, 5):
            v = m * 10**e
            if lo / 1.5 <= v <= hi * 1.5:
                ticks.append(v)
        e += 1
    return ticks or [lo, hi]


def _nz(value: float) -> float:
    """Collapse negative zero / sub-display noise so '-0.0%' is never printed."""
    return 0.0 if abs(value) < 5e-7 else value


def fmt(value: float | None, kind: str) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    value = _nz(value)
    if kind == "pct":
        return f"{value:+.1%}" if abs(value) < 10 else f"{value:+.0%}"
    if kind == "money":
        return f"{value:,.0f}"
    return f"{value:.2f}"


def _axis_label(value: float, kind: str) -> str:
    value = _nz(value)
    if kind == "pct":
        return f"{value:.0%}"
    if kind == "money":
        if abs(value) >= 1e6:
            return f"{value / 1e6:.1f}M"
        if abs(value) >= 1e3:
            return f"{value / 1e3:.0f}k"
        return f"{value:.0f}"
    return f"{value:g}"


def line_chart(
    chart_id: str,
    title: str,
    subtitle: str,
    dates: Sequence[str],
    series: Sequence[LineSeries],
    *,
    kind: str = "money",
    log: bool = False,
    bands: Sequence[tuple[int, int, str]] = (),
    zero_line: bool = False,
    keep_minima: bool = False,
) -> str:
    n = len(dates)
    lead = series[0].values if series else []
    idx = downsample(n, lead if keep_minima else None)
    finite = [v for s in series for i in idx if (v := s.values[i]) is not None and math.isfinite(v)]
    if not finite:
        return f'<figure class="chart"><figcaption><h3>{_esc(title)}</h3></figcaption><p class="muted">No data.</p></figure>'
    lo, hi = min(finite), max(finite)
    if log:
        lo, hi = max(lo, 1e-9), max(hi, 1e-9)
        ticks = log_ticks(lo, hi)
        tlo, thi = math.log10(min(ticks[0], lo)), math.log10(max(ticks[-1], hi))

        def ty(v: float) -> float:
            return MT + (H - MT - MB) * (1 - (math.log10(max(v, 1e-12)) - tlo) / ((thi - tlo) or 1))
    else:
        if zero_line:
            lo, hi = min(lo, 0.0), max(hi, 0.0)
        ticks = nice_ticks(lo, hi)
        tlo, thi = ticks[0], ticks[-1]

        def ty(v: float) -> float:
            return MT + (H - MT - MB) * (1 - (v - tlo) / ((thi - tlo) or 1))

    def tx(i: int) -> float:
        return ML + (W - ML - MR) * (i / max(n - 1, 1))

    parts = [
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="{chart_id}-t" data-chart="{chart_id}">',
        f'<title id="{chart_id}-t">{_esc(title)}</title>',
    ]
    for a, b, label in bands:
        x0, x1 = tx(a), tx(b)
        parts.append(
            f'<rect class="band" x="{x0:.1f}" y="{MT}" width="{max(x1 - x0, 1):.1f}" height="{H - MT - MB}"/>'
        )
        parts.append(
            f'<text class="band-label" x="{x0 + 6:.1f}" y="{MT + 14}">{_esc(label)}</text>'
        )
    for t in ticks:
        y = ty(t)
        parts.append(f'<line class="grid" x1="{ML}" x2="{W - MR}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="tick" x="{ML - 8}" y="{y + 4:.1f}" text-anchor="end">{_esc(_axis_label(t, kind))}</text>'
        )
    if zero_line and not log:
        parts.append(
            f'<line class="baseline" x1="{ML}" x2="{W - MR}" y1="{ty(0):.1f}" y2="{ty(0):.1f}"/>'
        )
    years = _year_ticks(dates)
    for i, label in years:
        parts.append(
            f'<text class="tick" x="{tx(i):.1f}" y="{H - 8}" text-anchor="middle">{label}</text>'
        )
    for s in series:
        pts = [(tx(i), ty(v)) for i in idx if (v := s.values[i]) is not None and math.isfinite(v)]
        if not pts:
            continue
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if s.area:
            base = ty(0.0) if (zero_line and not log) else H - MB
            parts.append(
                f'<polygon class="area" style="fill:var({s.color})" points="{pts[0][0]:.1f},{base:.1f} {path} {pts[-1][0]:.1f},{base:.1f}"/>'
            )
        parts.append(f'<polyline class="line" style="stroke:var({s.color})" points="{path}"/>')
        if s.label_end:
            x, y = pts[-1]
            parts.append(
                f'<circle class="end-dot" style="fill:var({s.color})" cx="{x:.1f}" cy="{y:.1f}" r="4"/>'
            )
            last = next((v for v in reversed(s.values) if v is not None), None)
            parts.append(
                f'<text class="end-label" x="{x + 8:.1f}" y="{y + 4:.1f}">{_esc(fmt(last, kind))}</text>'
            )
    parts.append(
        f'<line class="crosshair" x1="0" x2="0" y1="{MT}" y2="{H - MB}" visibility="hidden"/>'
    )
    parts.append(
        f'<rect class="hit" x="{ML}" y="{MT}" width="{W - ML - MR}" height="{H - MT - MB}"/>'
    )
    parts.append("</svg>")
    payload = {
        "x0": ML,
        "x1": W - MR,
        "dates": list(dates),
        "series": [
            {"name": s.name, "color": s.color, "values": [fmt(v, kind) for v in s.values]}
            for s in series
        ],
    }
    legend = ""
    if len(series) >= 2:
        legend = (
            '<div class="legend">'
            + "".join(
                f'<span><i class="key" style="background:var({s.color})"></i>{_esc(s.name)}</span>'
                for s in series
            )
            + "</div>"
        )
    return (
        f'<figure class="chart" id="{chart_id}"><figcaption><h3>{_esc(title)}</h3>'
        f'<p class="muted">{_esc(subtitle)}</p></figcaption>{legend}'
        f'<div class="plot">{"".join(parts)}<div class="tooltip" hidden></div></div>'
        f'<script type="application/json" class="chart-data">{_json(payload)}</script></figure>'
    )


def stacked_area_chart(
    chart_id: str, title: str, subtitle: str, dates: Sequence[str], series: Sequence[LineSeries]
) -> str:
    """Stacked weights (0..1). Series values must be >= 0."""
    n = len(dates)
    idx = downsample(n)

    def tx(i: int) -> float:
        return ML + (W - ML - MR) * (i / max(n - 1, 1))

    def ty(v: float) -> float:
        return MT + (H - MT - MB) * (1 - v)

    parts = [
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="{chart_id}-t" data-chart="{chart_id}">',
        f'<title id="{chart_id}-t">{_esc(title)}</title>',
    ]
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        parts.append(
            f'<line class="grid" x1="{ML}" x2="{W - MR}" y1="{ty(t):.1f}" y2="{ty(t):.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{ML - 8}" y="{ty(t) + 4:.1f}" text-anchor="end">{t:.0%}</text>'
        )
    for i, label in _year_ticks(dates):
        parts.append(
            f'<text class="tick" x="{tx(i):.1f}" y="{H - 8}" text-anchor="middle">{label}</text>'
        )
    cum = [0.0] * n
    for s in series:
        lower = [cum[i] for i in idx]
        upper = []
        for j, i in enumerate(idx):
            v = s.values[i] or 0.0
            upper.append(lower[j] + max(v, 0.0))
        for i, j in zip(idx, range(len(idx)), strict=True):
            cum[i] = upper[j]
        top = " ".join(f"{tx(i):.1f},{ty(u):.1f}" for i, u in zip(idx, upper, strict=True))
        bottom = " ".join(
            f"{tx(i):.1f},{ty(lw):.1f}" for i, lw in reversed(list(zip(idx, lower, strict=True)))
        )
        parts.append(
            f'<polygon class="stack" style="fill:var({s.color})" points="{top} {bottom}"/>'
        )
    parts.append(
        f'<line class="crosshair" x1="0" x2="0" y1="{MT}" y2="{H - MB}" visibility="hidden"/>'
    )
    parts.append(
        f'<rect class="hit" x="{ML}" y="{MT}" width="{W - ML - MR}" height="{H - MT - MB}"/>'
    )
    parts.append("</svg>")
    payload = {
        "x0": ML,
        "x1": W - MR,
        "dates": list(dates),
        "series": [
            {"name": s.name, "color": s.color, "values": [f"{(v or 0):.1%}" for v in s.values]}
            for s in series
        ],
    }
    legend = (
        '<div class="legend">'
        + "".join(
            f'<span><i class="swatch" style="background:var({s.color})"></i>{_esc(s.name)}</span>'
            for s in series
        )
        + "</div>"
    )
    return (
        f'<figure class="chart" id="{chart_id}"><figcaption><h3>{_esc(title)}</h3>'
        f'<p class="muted">{_esc(subtitle)}</p></figcaption>{legend}'
        f'<div class="plot">{"".join(parts)}<div class="tooltip" hidden></div></div>'
        f'<script type="application/json" class="chart-data">{_json(payload)}</script></figure>'
    )


def column_chart(
    chart_id: str, title: str, subtitle: str, labels: Sequence[str], values: Sequence[float]
) -> str:
    """Single-series columns from a zero baseline (annual returns)."""
    if not values:
        return ""
    lo, hi = min(0.0, *values), max(0.0, *values)
    ticks = nice_ticks(lo, hi)
    tlo, thi = ticks[0], ticks[-1]
    n = len(values)
    slot = (W - ML - MR) / n
    bw = min(24.0, slot * 0.6)

    def ty(v: float) -> float:
        return MT + (H - MT - MB) * (1 - (v - tlo) / ((thi - tlo) or 1))

    parts = [
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="{chart_id}-t">',
        f'<title id="{chart_id}-t">{_esc(title)}</title>',
    ]
    for t in ticks:
        parts.append(
            f'<line class="grid" x1="{ML}" x2="{W - MR}" y1="{ty(t):.1f}" y2="{ty(t):.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{ML - 8}" y="{ty(t) + 4:.1f}" text-anchor="end">{t:.0%}</text>'
        )
    y0 = ty(0.0)
    for k, (label, v) in enumerate(zip(labels, values, strict=True)):
        cx = ML + slot * (k + 0.5)
        y1 = ty(v)
        top, h = min(y0, y1), abs(y1 - y0)
        cls = "col-pos" if v >= 0 else "col-neg"
        parts.append(
            f'<rect class="{cls}" tabindex="0" x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(h, 1):.1f}" rx="2">'
            f"<title>{_esc(label)}: {v:+.1%}</title></rect>"
        )
        if n <= 30:
            parts.append(
                f'<text class="tick" x="{cx:.1f}" y="{H - 8}" text-anchor="middle">{_esc(label)}</text>'
            )
    parts.append(
        f'<line class="baseline" x1="{ML}" x2="{W - MR}" y1="{y0:.1f}" y2="{y0:.1f}"/></svg>'
    )
    return (
        f'<figure class="chart" id="{chart_id}"><figcaption><h3>{_esc(title)}</h3>'
        f'<p class="muted">{_esc(subtitle)}</p></figcaption><div class="plot">{"".join(parts)}</div></figure>'
    )


def diverging_cell_style(value: float | None, scale: float = 0.10) -> str:
    """Blue (gain) <-> gray midpoint <-> red (loss); text ink chosen by fill lightness."""
    if value is None or not math.isfinite(value):
        return ""
    t = max(-1.0, min(1.0, value / scale))
    var = "--pos" if t >= 0 else "--neg"
    pct = round(abs(t) * 100)
    ink = "var(--ink-on-strong)" if pct >= 60 else "var(--text-primary)"
    return f"background:color-mix(in oklab, var({var}) {pct}%, var(--mid));color:{ink}"


def _year_ticks(dates: Sequence[str]) -> list[tuple[int, str]]:
    seen: list[tuple[int, str]] = []
    last = ""
    for i, d in enumerate(dates):
        y = d[:4]
        if y != last:
            seen.append((i, y))
            last = y
    # drop a partial first year whose label would collide with the next one
    min_gap = max(1, len(dates) * 45 // (W - ML - MR))
    if len(seen) >= 2 and seen[1][0] - seen[0][0] < min_gap:
        seen = seen[1:]
    if len(seen) > 12:
        step = math.ceil(len(seen) / 12)
        seen = seen[::step]
    return seen


def _json(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")
