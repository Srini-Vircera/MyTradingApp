"use client";

/**
 * Small dependency-free SVG charts. Conventions: one y-axis per chart, thin
 * marks, recessive grid, a hover tooltip, colour by entity (never by rank),
 * status colours only with an icon + label, and a data-table view for every
 * chart so nothing depends on colour alone.
 */
import { useEffect, useId, useState, type MouseEvent, type ReactNode } from "react";
import { bandStatus } from "@/lib/mode";

const H = 220;
const M = { top: 12, right: 16, bottom: 26, left: 64 };
const IH = H - M.top - M.bottom;

/** Draw at the real pixel width so text stays 11px at any card size. */
function useWidth(): [(el: HTMLDivElement | null) => void, number] {
  const [el, setEl] = useState<HTMLDivElement | null>(null);
  const [w, setW] = useState(760);
  useEffect(() => {
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => {
      const cw = Math.round(e?.contentRect.width ?? 0);
      if (cw > 0) setW(Math.max(280, cw));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  return [setEl, w];
}

export interface Point {
  x: string; // ISO date or timestamp
  y: number;
}

function shortDate(x: string): string {
  const d = new Date(x);
  return Number.isNaN(d.getTime())
    ? x
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

function extent(values: number[], includeZero: boolean): [number, number] {
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (includeZero) {
    lo = Math.min(lo, 0);
    hi = Math.max(hi, 0);
  }
  if (lo === hi) {
    const pad = Math.abs(lo) * 0.05 || 1;
    return [lo - pad, hi + pad];
  }
  const pad = (hi - lo) * 0.05;
  return [includeZero && lo === 0 ? 0 : lo - pad, includeZero && hi === 0 ? 0 : hi + pad];
}

function ticks(lo: number, hi: number, n = 4): number[] {
  return Array.from({ length: n + 1 }, (_, i) => lo + ((hi - lo) * i) / n);
}

/** Index of the nearest x position under the pointer. */
function useHover(count: number) {
  const [idx, setIdx] = useState<number | null>(null);
  function onMove(e: MouseEvent<SVGRectElement>) {
    const box = e.currentTarget.getBoundingClientRect();
    if (box.width <= 0 || count === 0) return;
    const rel = (e.clientX - box.left) / box.width;
    setIdx(Math.max(0, Math.min(count - 1, Math.round(rel * (count - 1)))));
  }
  return { idx, onMove, onLeave: () => setIdx(null) };
}

function Figure({
  title,
  summary,
  children,
  table,
}: {
  title: string;
  summary?: ReactNode;
  children: ReactNode;
  table: ReactNode;
}) {
  const [showTable, setShowTable] = useState(false);
  return (
    <figure className="chart">
      <figcaption>
        <span className="chart-title">{title}</span>
        {summary && <span className="chart-summary">{summary}</span>}
        <button type="button" className="btn ghost small" onClick={() => setShowTable((s) => !s)}>
          {showTable ? "Show chart" : "Show table"}
        </button>
      </figcaption>
      {showTable ? <div className="table-wrap chart-table">{table}</div> : children}
    </figure>
  );
}

function Empty({ title }: { title: string }) {
  return (
    <figure className="chart">
      <figcaption>
        <span className="chart-title">{title}</span>
      </figcaption>
      <p className="muted chart-empty">No data yet.</p>
    </figure>
  );
}

function YAxis({ lo, hi, y, format, W }: { lo: number; hi: number; y: (v: number) => number; format: (v: number) => string; W: number }) {
  return (
    <g className="axis">
      {ticks(lo, hi).map((t) => (
        <g key={t}>
          <line className="grid" x1={M.left} x2={W - M.right} y1={y(t)} y2={y(t)} />
          <text x={M.left - 6} y={y(t)} dy="0.32em" textAnchor="end">
            {format(t)}
          </text>
        </g>
      ))}
    </g>
  );
}

function XLabels({ xs, x }: { xs: string[]; x: (i: number) => number }) {
  if (xs.length === 0) return null;
  const idx = xs.length === 1 ? [0] : [0, Math.floor((xs.length - 1) / 2), xs.length - 1];
  return (
    <g className="axis">
      {[...new Set(idx)].map((i) => (
        <text
          key={i}
          x={x(i)}
          y={H - 6}
          textAnchor={i === 0 ? "start" : i === xs.length - 1 ? "end" : "middle"}
        >
          {shortDate(xs[i] ?? "")}
        </text>
      ))}
    </g>
  );
}

function Tooltip({ left, W, children }: { left: number; W: number; children: ReactNode }) {
  return (
    <div className="tooltip" role="presentation" style={{ left: `${(left / W) * 100}%` }}>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------- line / area
export function LineChart({
  title,
  points,
  format,
  area = false,
  includeZero = false,
  tone = "series-1",
  summary,
  axisFormat,
}: {
  title: string;
  points: Point[];
  format: (v: number) => string;
  axisFormat?: (v: number) => string;
  area?: boolean;
  includeZero?: boolean;
  tone?: string;
  summary?: ReactNode;
}) {
  const clean = points.filter((p) => Number.isFinite(p.y));
  const hover = useHover(clean.length);
  const [ref, W] = useWidth();
  const IW = W - M.left - M.right;
  if (clean.length === 0) return <Empty title={title} />;
  const [lo, hi] = extent(
    clean.map((p) => p.y),
    includeZero || area,
  );
  const x = (i: number) => M.left + (clean.length === 1 ? IW / 2 : (IW * i) / (clean.length - 1));
  const y = (v: number) => M.top + IH - ((v - lo) / (hi - lo)) * IH;
  const line = clean.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.y)}`).join("");
  const base = y(Math.max(lo, Math.min(hi, 0)));
  const areaPath = `${line}L${x(clean.length - 1)},${base}L${x(0)},${base}Z`;
  const h = hover.idx === null ? null : clean[hover.idx];

  return (
    <Figure
      title={title}
      summary={summary}
      table={
        <table>
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col" className="num">
                {title}
              </th>
            </tr>
          </thead>
          <tbody>
            {clean.map((p) => (
              <tr key={p.x}>
                <td>{p.x}</td>
                <td className="num">{format(p.y)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      <div className="plot" ref={ref}>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
          <YAxis lo={lo} hi={hi} y={y} format={axisFormat ?? format} W={W} />
          <line className="baseline" x1={M.left} x2={W - M.right} y1={base} y2={base} />
          {area && <path d={areaPath} className={`area ${tone}`} />}
          <path d={line} className={`line ${tone}`} />
          {clean.length === 1 && <circle cx={x(0)} cy={y(clean[0]!.y)} r={4} className={`dot ${tone}`} />}
          <XLabels xs={clean.map((p) => p.x)} x={x} />
          {h && hover.idx !== null && (
            <g>
              <line className="crosshair" x1={x(hover.idx)} x2={x(hover.idx)} y1={M.top} y2={M.top + IH} />
              <circle cx={x(hover.idx)} cy={y(h.y)} r={4} className={`dot ring ${tone}`} />
            </g>
          )}
          <rect
            x={M.left}
            y={M.top}
            width={IW}
            height={IH}
            fill="transparent"
            onMouseMove={hover.onMove}
            onMouseLeave={hover.onLeave}
          />
        </svg>
        {h && hover.idx !== null && (
          <Tooltip W={W} left={x(hover.idx)}>
            <div className="tt-x">{h.x}</div>
            <div className="tt-y">{format(h.y)}</div>
          </Tooltip>
        )}
      </div>
    </Figure>
  );
}

// ---------------------------------------------------------------- bars (+/-)
export function ReturnBars({
  title,
  points,
  format,
  axisFormat,
}: {
  title: string;
  points: Point[];
  format: (v: number) => string;
  axisFormat?: (v: number) => string;
}) {
  const clean = points.filter((p) => Number.isFinite(p.y));
  const hover = useHover(clean.length);
  const [ref, W] = useWidth();
  const IW = W - M.left - M.right;
  if (clean.length === 0) return <Empty title={title} />;
  const [lo, hi] = extent(
    clean.map((p) => p.y),
    true,
  );
  const step = IW / clean.length;
  const bw = Math.max(1, Math.min(18, step - 2));
  const x = (i: number) => M.left + step * i + step / 2;
  const y = (v: number) => M.top + IH - ((v - lo) / (hi - lo)) * IH;
  const zero = y(0);
  const h = hover.idx === null ? null : clean[hover.idx];
  return (
    <Figure
      title={title}
      summary="blue = gain, red = loss"
      table={
        <table>
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col" className="num">
                Return
              </th>
            </tr>
          </thead>
          <tbody>
            {clean.map((p) => (
              <tr key={p.x}>
                <td>{p.x}</td>
                <td className="num">{format(p.y)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      <div className="plot" ref={ref}>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
          <YAxis lo={lo} hi={hi} y={y} format={axisFormat ?? format} W={W} />
          {clean.map((p, i) => (
            <rect
              key={p.x}
              x={x(i) - bw / 2}
              width={bw}
              y={Math.min(y(p.y), zero)}
              height={Math.max(1, Math.abs(y(p.y) - zero))}
              rx={Math.min(2, bw / 4)}
              className={p.y >= 0 ? "bar pos" : "bar neg"}
              opacity={hover.idx === null || hover.idx === i ? 1 : 0.5}
            />
          ))}
          <line className="baseline" x1={M.left} x2={W - M.right} y1={zero} y2={zero} />
          <XLabels xs={clean.map((p) => p.x)} x={x} />
          <rect
            x={M.left}
            y={M.top}
            width={IW}
            height={IH}
            fill="transparent"
            onMouseMove={hover.onMove}
            onMouseLeave={hover.onLeave}
          />
        </svg>
        {h && hover.idx !== null && (
          <Tooltip W={W} left={x(hover.idx)}>
            <div className="tt-x">{h.x}</div>
            <div className="tt-y">{format(h.y)}</div>
          </Tooltip>
        )}
      </div>
    </Figure>
  );
}

// ---------------------------------------------------------------- allocation
/** Fixed colour per instrument (colour follows the entity). */
export const INSTRUMENT_TONES: Record<string, string> = {
  TQQQ: "series-1",
  SQQQ: "series-2",
  QQQ: "series-3",
  CASH: "series-cash",
};

export interface AllocationPoint {
  x: string;
  weights: Record<string, number>; // symbol -> weight (cash implied as the remainder)
}

export function AllocationChart({ title, points }: { title: string; points: AllocationPoint[] }) {
  const hover = useHover(points.length);
  const [ref, W] = useWidth();
  const IW = W - M.left - M.right;
  if (points.length === 0) return <Empty title={title} />;
  const symbols = ["TQQQ", "QQQ", "SQQQ"];
  const others = [...new Set(points.flatMap((p) => Object.keys(p.weights)))].filter(
    (s) => !symbols.includes(s) && s.toUpperCase() !== "CASH",
  );
  const keys = [...symbols, ...(others.length ? ["Other"] : []), "CASH"];
  const rows = points.map((p) => {
    const v: Record<string, number> = {};
    for (const s of symbols) v[s] = Math.max(0, p.weights[s] ?? 0);
    if (others.length) v.Other = others.reduce((a, s) => a + Math.max(0, p.weights[s] ?? 0), 0);
    const invested = Object.values(v).reduce((a, b) => a + b, 0);
    v.CASH = Math.max(0, 1 - invested);
    return v;
  });
  const top = Math.max(1, ...rows.map((r) => Object.values(r).reduce((a, b) => a + b, 0)));
  const x = (i: number) => M.left + (points.length === 1 ? IW / 2 : (IW * i) / (points.length - 1));
  const y = (v: number) => M.top + IH - (v / top) * IH;
  const cum = rows.map(() => 0);
  const layers = keys.map((k) => {
    const lower = [...cum];
    rows.forEach((r, i) => {
      cum[i] = (cum[i] ?? 0) + (r[k] ?? 0);
    });
    const upper = [...cum];
    const widen = points.length === 1;
    const xs = widen ? [M.left, W - M.right] : points.map((_, i) => x(i));
    const up = widen ? [upper[0]!, upper[0]!] : upper;
    const lowv = widen ? [lower[0]!, lower[0]!] : lower;
    const d =
      xs.map((xx, i) => `${i ? "L" : "M"}${xx},${y(up[i]!)}`).join("") +
      xs
        .map((_, i) => xs.length - 1 - i)
        .map((j) => `L${xs[j]},${y(lowv[j]!)}`)
        .join("") +
      "Z";
    return { k, d };
  });
  const h = hover.idx === null ? null : rows[hover.idx];
  const pctf = (v: number) => `${(v * 100).toFixed(1)}%`;
  return (
    <Figure
      title={title}
      table={
        <table>
          <thead>
            <tr>
              <th scope="col">Time</th>
              {keys.map((k) => (
                <th key={k} scope="col" className="num">
                  {k}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>{points[i]?.x}</td>
                {keys.map((k) => (
                  <td key={k} className="num">
                    {pctf(r[k] ?? 0)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      <Legend items={keys.map((k) => ({ label: k, tone: INSTRUMENT_TONES[k] ?? "series-other" }))} />
      <div className="plot" ref={ref}>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
          <YAxis lo={0} hi={top} y={y} format={(v) => `${Math.round(v * 100)}%`} W={W} />
          {layers.map((l) => (
            <path key={l.k} d={l.d} className={`stack ${INSTRUMENT_TONES[l.k] ?? "series-other"}`} />
          ))}
          <XLabels xs={points.map((p) => p.x)} x={x} />
          {hover.idx !== null && (
            <line className="crosshair" x1={x(hover.idx)} x2={x(hover.idx)} y1={M.top} y2={M.top + IH} />
          )}
          <rect
            x={M.left}
            y={M.top}
            width={IW}
            height={IH}
            fill="transparent"
            onMouseMove={hover.onMove}
            onMouseLeave={hover.onLeave}
          />
        </svg>
        {h && hover.idx !== null && (
          <Tooltip W={W} left={x(hover.idx)}>
            <div className="tt-x">{points[hover.idx]?.x}</div>
            {keys.map((k) => (
              <div key={k} className="tt-row">
                <span className={`swatch ${INSTRUMENT_TONES[k] ?? "series-other"}`} /> {k}{" "}
                <b>{pctf(h[k] ?? 0)}</b>
              </div>
            ))}
          </Tooltip>
        )}
      </div>
    </Figure>
  );
}

export function Legend({ items }: { items: { label: string; tone: string }[] }) {
  return (
    <ul className="legend">
      {items.map((i) => (
        <li key={i.label}>
          <span className={`swatch ${i.tone}`} aria-hidden="true" /> {i.label}
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------- risk bands
const BAND_ICON: Record<string, string> = {
  good: "●",
  warning: "▲",
  serious: "◆",
  critical: "■",
  neutral: "○",
};

export function BandHistory({
  title,
  items,
}: {
  title: string;
  items: { x: string; band: string; blocked: boolean }[];
}) {
  const [hover, setHover] = useState<number | null>(null);
  const [ref, W] = useWidth();
  if (items.length === 0) return <Empty title={title} />;
  const bands = [...new Set(items.map((i) => i.band))];
  const cellW = W / items.length;
  const h = hover === null ? null : items[hover];
  return (
    <Figure
      title={title}
      table={
        <table>
          <thead>
            <tr>
              <th scope="col">Time</th>
              <th scope="col">Band</th>
              <th scope="col">Risk-increasing blocked</th>
            </tr>
          </thead>
          <tbody>
            {items.map((i, n) => (
              <tr key={n}>
                <td>{i.x}</td>
                <td>{i.band}</td>
                <td>{i.blocked ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      <ul className="legend">
        {bands.map((b) => (
          <li key={b}>
            <span className={`swatch status-${bandStatus(b)}`} aria-hidden="true" />{" "}
            {BAND_ICON[bandStatus(b)]} {b}
          </li>
        ))}
      </ul>
      <div className="plot" ref={ref}>
        <svg viewBox={`0 0 ${W} 48`} role="img" aria-label={title}>
          {items.map((i, n) => (
            <rect
              key={n}
              x={n * cellW + 1}
              y={4}
              width={Math.max(1, cellW - 2)}
              height={i.blocked ? 40 : 28}
              rx={2}
              className={`status-${bandStatus(i.band)}`}
              onMouseEnter={() => setHover(n)}
              onMouseLeave={() => setHover(null)}
            />
          ))}
        </svg>
        {h && hover !== null && (
          <Tooltip W={W} left={(hover + 0.5) * cellW}>
            <div className="tt-x">{h.x}</div>
            <div>
              {BAND_ICON[bandStatus(h.band)]} {h.band}
              {h.blocked ? " — risk-increasing orders blocked" : ""}
            </div>
          </Tooltip>
        )}
      </div>
      <p className="muted small">Taller cells: risk-increasing orders were blocked.</p>
    </Figure>
  );
}

// ---------------------------------------------------------------- heatmap
export interface HeatCell {
  row: string;
  col: string;
  value: number; // -1 .. +1
  detail?: string;
}

/** Diverging heatmap: blue = long/positive, red = short/negative, grey = neutral. */
export function Heatmap({ title, cells }: { title: string; cells: HeatCell[] }) {
  const [hover, setHover] = useState<HeatCell | null>(null);
  const id = useId();
  const [ref, W] = useWidth();
  if (cells.length === 0) return <Empty title={title} />;
  const rows = [...new Set(cells.map((c) => c.row))].sort();
  const cols = [...new Set(cells.map((c) => c.col))].sort();
  const byKey = new Map(cells.map((c) => [`${c.row}\u0000${c.col}`, c]));
  const labelW = 190;
  const cw = Math.max(6, Math.min(64, (W - labelW) / cols.length));
  const ch = 24;
  const width = labelW + cw * cols.length;
  const height = ch * rows.length + 24;
  return (
    <Figure
      title={title}
      summary="blue = bullish score, red = bearish, grey = neutral"
      table={
        <table>
          <thead>
            <tr>
              <th scope="col">Strategy</th>
              <th scope="col">Time</th>
              <th scope="col" className="num">
                Score
              </th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {cells.map((c, i) => (
              <tr key={i}>
                <td>{c.row}</td>
                <td>{c.col}</td>
                <td className="num">{c.value.toFixed(2)}</td>
                <td>{c.detail ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    >
      <div className="plot heat" ref={ref} aria-describedby={`${id}-hover`}>
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title} style={{ maxWidth: width }}>
          {rows.map((r, ri) => (
            <g key={r}>
              <text className="axis-label" x={labelW - 8} y={ri * ch + ch / 2} dy="0.32em" textAnchor="end">
                {r.length > 28 ? `${r.slice(0, 27)}…` : r}
              </text>
              {cols.map((c, ci) => {
                const cell = byKey.get(`${r}\u0000${c}`);
                if (!cell) return null;
                const v = Math.max(-1, Math.min(1, cell.value));
                const stepN = Math.min(4, Math.round(Math.abs(v) * 4));
                const cls = stepN === 0 ? "heat-0" : v > 0 ? `heat-pos-${stepN}` : `heat-neg-${stepN}`;
                return (
                  <rect
                    key={c}
                    x={labelW + ci * cw + 1}
                    y={ri * ch + 1}
                    width={cw - 2}
                    height={ch - 2}
                    rx={2}
                    className={cls}
                    onMouseEnter={() => setHover(cell)}
                    onMouseLeave={() => setHover(null)}
                  />
                );
              })}
            </g>
          ))}
          <text className="axis-label" x={labelW} y={height - 6}>
            {shortDate(cols[0] ?? "")}
          </text>
          <text className="axis-label" x={width} y={height - 6} textAnchor="end">
            {shortDate(cols[cols.length - 1] ?? "")}
          </text>
        </svg>
        <p id={`${id}-hover`} className="small muted heat-readout">
          {hover
            ? `${hover.row} · ${hover.col} · score ${hover.value.toFixed(2)}${hover.detail ? ` · ${hover.detail}` : ""}`
            : "Hover a cell for details."}
        </p>
      </div>
    </Figure>
  );
}
