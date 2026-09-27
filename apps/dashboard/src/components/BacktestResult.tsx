"use client";

/** Backtest results: one run (ensemble) or an independent side-by-side comparison. */
import { useState } from "react";
import { LineChart, type Point } from "./charts";
import { SyntheticWarning } from "./control";
import { DataTable, JsonView, Stat } from "./ui";
import type { Row } from "@/lib/api";
import { fixed, money, money0, num, pct, text } from "@/lib/format";

function DataLabel({ s }: { s: Row }) {
  return s.has_synthetic ? (
    <span className="tag synthetic">INCLUDES SYNTHETIC DATA</span>
  ) : (
    <span className="tag real">REAL DATA ONLY</span>
  );
}

function Unpriced({ s }: { s: Row }) {
  const unpriced = (s.unpriced_instruments as string[] | undefined) ?? [];
  if (!unpriced.length) return null;
  return (
    <p className="muted small">
      No {unpriced.join("/")} data was loaded; the selected strategies never hold them (the engine would stop the run if they did).
    </p>
  );
}

function Notes({ s }: { s: Row }) {
  const notes = (s.notes as string[] | undefined) ?? [];
  if (!notes.length) return null;
  return (
    <details>
      <summary>Notes</summary>
      <ul>
        {notes.map((n, i) => (
          <li key={i}>{n}</li>
        ))}
      </ul>
    </details>
  );
}

/** Golden/Death Cross details derived from the recorded decisions. */
export function CrossoverView({ crossover }: { crossover: Row | undefined }) {
  const entries = Object.entries((crossover ?? {}) as Record<string, Row>);
  if (!entries.length) return null;
  return (
    <>
      {entries.map(([sid, x]) => {
        const events = (x.events as Row[] | undefined) ?? [];
        const periods = (x.regime_periods as Row[] | undefined) ?? [];
        return (
          <section key={sid} className="card" aria-label={`Golden/Death Cross details for ${sid}`}>
            <h3>Golden Cross / Death Cross — {sid}</h3>
            <dl className="kv">
              <dt>Moving averages</dt>
              <dd>
                fast {text(x.fast_period)} / slow {text(x.slow_period)} {text(x.ma_type)}{" "}
                {x.classic ? <span className="tag real">CLASSIC</span> : <span className="tag synthetic">VARIANT</span>}
              </dd>
              <dt>Bearish behaviour</dt>
              <dd>{text(x.bearish_action)}</dd>
              <dt>First signal (warm-up complete)</dt>
              <dd>{text(x.first_signal)}</dd>
              <dt>Regime at the end</dt>
              <dd>{text(x.current_regime)}</dd>
              <dt>Golden Cross dates</dt>
              <dd>{((x.golden_cross_dates as string[]) ?? []).join(", ") || "none in this period"}</dd>
              <dt>Death Cross dates</dt>
              <dd>{((x.death_cross_dates as string[]) ?? []).join(", ") || "none in this period"}</dd>
            </dl>
            <DataTable
              caption="Cross events and the orders they caused (hypothetical)"
              rows={events}
              empty="No crossover inside the backtest period (the regime did not change)."
              columns={[
                { key: "date", label: "Bar" },
                { key: "type", label: "Event", render: (e) => (e.type === "golden" ? "Golden Cross" : "Death Cross") },
                { key: "fast_ma", label: "Fast MA", numeric: true, render: (e) => fixed(e.fast_ma) },
                { key: "slow_ma", label: "Slow MA", numeric: true, render: (e) => fixed(e.slow_ma) },
                {
                  key: "orders",
                  label: "Resulting fills",
                  render: (e) =>
                    ((e.orders as Row[]) ?? []).map((o) => `${text(o.date)} ${text(o.side)} ${fixed(o.quantity)} ${text(o.symbol)}`).join("; ") ||
                    (e.refused ? `refused: ${text(e.refused)}` : "none (already at target or blocked by risk limits)"),
                },
              ]}
            />
            <DataTable
              caption="Regime history"
              rows={periods}
              columns={[
                { key: "regime", label: "Regime" },
                { key: "from", label: "From" },
                { key: "to", label: "To", render: (p) => text(p.to ?? "end of backtest") },
              ]}
            />
          </section>
        );
      })}
    </>
  );
}

export function SingleResult({ s }: { s: Row }) {
  const headline = (s.headline ?? {}) as Row;
  const curve = (s.curve as Row[] | undefined) ?? [];
  const equity: Point[] = curve.map((p) => ({ x: String(p.date), y: num(p.equity) ?? NaN }));
  const drawdown: Point[] = curve.map((p) => ({ x: String(p.date), y: num(p.drawdown) ?? NaN }));
  const all = ((s.metrics as Record<string, Record<string, Row>> | undefined)?.all ?? {}) as Record<string, Row>;
  const compare = Object.entries(all).map(([series, v]) => ({ series, ...v }));
  const several = ((s.strategies as string[]) ?? []).length > 1;
  return (
    <>
      <p>
        {several ? <strong>Combined ensemble (one portfolio): </strong> : null}
        {((s.strategies as string[]) ?? []).join(", ")} · {text(s.source)} · {text((s.period as Row).start)} →{" "}
        {text((s.period as Row).end)} · config {text(s.config_version)} <DataLabel s={s} />
      </p>
      {s.has_synthetic ? <SyntheticWarning /> : null}
      <Unpriced s={s} />
      <div className="stats">
        <Stat label="Starting capital" value={money0(s.initial_capital)} />
        <Stat label="Ending equity (hypothetical)" value={money0(s.ending_equity)} />
        <Stat label="Total return" value={pct(headline.total_return)} />
        <Stat label="CAGR" value={pct(headline.cagr)} />
        <Stat label="Volatility" value={pct(headline.volatility)} />
        <Stat label="Sharpe" value={fixed(headline.sharpe)} />
        <Stat label="Sortino" value={fixed(headline.sortino)} />
        <Stat label="Max drawdown" value={pct(headline.max_drawdown)} />
        <Stat label="Longest drawdown" value={`${text(headline.max_drawdown_duration_sessions)} sessions`} />
        <Stat label="Calmar" value={fixed(headline.calmar)} />
        <Stat label="Annual turnover" value={fixed(headline.annual_turnover)} />
        <Stat label="Trades (fills)" value={`${text(headline.trades)} (${text(s.fills)})`} />
      </div>
      <LineChart title="Equity (hypothetical)" points={equity} format={money} axisFormat={money0} />
      <LineChart title="Drawdown" points={drawdown} format={(v) => pct(v)} area includeZero tone="critical" />
      <DataTable
        caption="Comparison with benchmarks (whole period)"
        rows={compare}
        columns={[
          { key: "series", label: "Series" },
          { key: "total_return", label: "Total return", numeric: true, render: (x) => pct(x.total_return) },
          { key: "cagr", label: "CAGR", numeric: true, render: (x) => pct(x.cagr) },
          { key: "volatility", label: "Volatility", numeric: true, render: (x) => pct(x.volatility) },
          { key: "sharpe", label: "Sharpe", numeric: true, render: (x) => fixed(x.sharpe) },
          { key: "max_drawdown", label: "Max drawdown", numeric: true, render: (x) => pct(x.max_drawdown) },
        ]}
      />
      <CrossoverView crossover={s.crossover as Row | undefined} />
      <Notes s={s} />
      <JsonView label="All metrics" value={s.metrics} />
    </>
  );
}

const METRIC_COLUMNS = [
  { key: "ending_equity", label: "Ending equity", numeric: true, render: (x: Row) => money0(x.ending_equity) },
  { key: "total_return", label: "Total return", numeric: true, render: (x: Row) => pct(x.total_return) },
  { key: "cagr", label: "CAGR", numeric: true, render: (x: Row) => pct(x.cagr) },
  { key: "volatility", label: "Volatility", numeric: true, render: (x: Row) => pct(x.volatility) },
  { key: "sharpe", label: "Sharpe", numeric: true, render: (x: Row) => fixed(x.sharpe) },
  { key: "sortino", label: "Sortino", numeric: true, render: (x: Row) => fixed(x.sortino) },
  { key: "max_drawdown", label: "Max DD", numeric: true, render: (x: Row) => pct(x.max_drawdown) },
  { key: "max_drawdown_duration_sessions", label: "Longest DD (sessions)", numeric: true },
  { key: "calmar", label: "Calmar", numeric: true, render: (x: Row) => fixed(x.calmar) },
  { key: "annual_turnover", label: "Turnover", numeric: true, render: (x: Row) => fixed(x.annual_turnover) },
  { key: "trades", label: "Trades", numeric: true },
];

/** Independent comparison: separate backtests over the identical period, side by side. */
export function ComparisonResult({ s }: { s: Row }) {
  const runs = (s.runs as Row[] | undefined) ?? [];
  const [pick, setPick] = useState(0);
  const run = runs[Math.min(pick, runs.length - 1)];
  return (
    <>
      <p>
        <strong>Independent comparison</strong> — {runs.length} separate backtests (not an ensemble) over the identical
        period {text((s.period as Row).start)} → {text((s.period as Row).end)}, with the same data ({text(s.source)}),
        starting capital {money0(s.initial_capital)}, execution, costs and risk settings. <DataLabel s={s} />
      </p>
      {s.has_synthetic ? <SyntheticWarning /> : null}
      <Unpriced s={s} />
      <DataTable
        caption="Side-by-side results (hypothetical)"
        rows={(s.comparison as Row[] | undefined) ?? []}
        columns={[{ key: "strategy", label: "Strategy" }, ...METRIC_COLUMNS]}
      />
      <DataTable
        caption="Benchmarks over the same period"
        rows={(s.benchmarks as Row[] | undefined) ?? []}
        columns={[{ key: "series", label: "Benchmark" }, ...METRIC_COLUMNS.filter((c) => c.key !== "ending_equity" && c.key !== "trades")]}
      />
      {runs.length > 0 && (
        <>
          <label className="inline">
            Details for{" "}
            <select value={pick} onChange={(e) => setPick(Number(e.target.value))}>
              {runs.map((r, i) => (
                <option key={i} value={i}>
                  {((r.strategies as string[]) ?? []).join(", ")}
                </option>
              ))}
            </select>
          </label>
          {run && <SingleResult s={run} />}
        </>
      )}
      <Notes s={s} />
    </>
  );
}
