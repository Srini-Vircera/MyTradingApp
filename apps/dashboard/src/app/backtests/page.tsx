"use client";

import { useEffect, useState, type FormEvent } from "react";
import { LineChart, type Point } from "@/components/charts";
import {
  ActionResult,
  Field,
  HYPOTHETICAL,
  JobBadge,
  JobTable,
  OperatorField,
  SyntheticWarning,
  useOperator,
  WorkerNote,
} from "@/components/control";
import { DataTable, JsonView, LoadState, PageHeader, Section, Stat } from "@/components/ui";
import { mutate, type Job, type Row } from "@/lib/api";
import { fixed, money, money0, num, pct, text } from "@/lib/format";
import { useApi, useMutation } from "@/lib/session";

type Execution = "near_close" | "next_open" | "next_close" | "closing_auction";
const EXECUTION_LABEL: Record<Execution, string> = {
  near_close: "Near the close (same session)",
  next_open: "Next session's open",
  next_close: "Next session's close",
  closing_auction: "Closing auction",
};
const COSTS = [
  ["commission_per_share", "Commission per share ($)"],
  ["commission_per_order", "Commission per order ($)"],
  ["commission_minimum", "Minimum commission ($)"],
  ["slippage_bps", "Slippage (bps)"],
  ["impact_coefficient_bps", "Market impact coefficient (bps)"],
  ["max_participation", "Max share of daily volume"],
] as const;
type CostKey = (typeof COSTS)[number][0];

function Disclaimer() {
  return (
    <p className="alert disclaimer" role="note">
      {HYPOTHETICAL}
    </p>
  );
}

function NewBacktest({ options, onQueued }: { options: Row; onQueued: (j: Job) => void }) {
  const defaults = (options.defaults ?? {}) as Row;
  const strategies = (options.strategies as Row[] | undefined) ?? [];
  const sources = (options.sources as string[] | undefined) ?? [];
  const [actor, setActor] = useOperator();
  const [chosen, setChosen] = useState<string[]>(["baseline_buy_hold"]);
  const [source, setSource] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [capital, setCapital] = useState(String(defaults.initial_capital ?? ""));
  const [execution, setExecution] = useState<Execution>((defaults.execution as Execution) ?? "near_close");
  const [delay, setDelay] = useState(String(defaults.execution_delay_bars ?? 0));
  const [synthetic, setSynthetic] = useState(Boolean(defaults.use_synthetic_history));
  const [costs, setCosts] = useState<Record<string, string>>(() =>
    Object.fromEntries(COSTS.map(([k]) => [k, String(((defaults.costs ?? {}) as Row)[k] ?? "")])),
  );
  const m = useMutation();

  useEffect(() => {
    setCapital(String(defaults.initial_capital ?? ""));
    setExecution((defaults.execution as Execution) ?? "near_close");
    setDelay(String(defaults.execution_delay_bars ?? 0));
    setSynthetic(Boolean(defaults.use_synthetic_history));
  }, [JSON.stringify(defaults)]);

  const capitalN = num(capital);
  const delayN = num(delay);
  const leveraged = chosen.some((sid) => !strategies.find((s) => s.strategy_id === sid)?.long_only_1x);
  const problems = [
    chosen.length === 0 && "choose at least one strategy",
    chosen.length > 10 && "at most 10 strategies per run",
    (capitalN === null || capitalN <= 0) && "starting capital must be a positive amount",
    (delayN === null || delayN < 0 || delayN > 20 || !Number.isInteger(delayN)) && "delay must be 0–20 sessions",
    start && end && end < start && "the end date is before the start date",
    ...COSTS.map(([k, label]) => {
      const v = num(costs[k]);
      return costs[k] !== "" && (v === null || v < 0) ? `${label} must be zero or more` : false;
    }),
  ].filter(Boolean) as string[];
  const ok = actor.trim().length >= 2 && problems.length === 0 && !m.busy;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ok) return;
    const costOverrides: Partial<Record<CostKey, number>> = {};
    for (const [k] of COSTS) {
      const v = num(costs[k]);
      const d = num(((defaults.costs ?? {}) as Row)[k]);
      if (v !== null && v !== d) costOverrides[k] = v;
    }
    const out = await m.run((t) =>
      mutate(t, "/api/v1/jobs/backtest", {
        actor: actor.trim(),
        params: {
          strategies: chosen,
          source: source || null,
          start: start || null,
          end: end || null,
          initial_capital: capitalN,
          execution,
          execution_delay_bars: delayN === null ? null : Math.trunc(delayN),
          use_synthetic_history: synthetic,
          costs: costOverrides,
        },
      }),
    );
    if (out?.job) onQueued(out.job);
  }

  return (
    <Section title="New backtest">
      <form onSubmit={submit}>
        <fieldset className="plain">
          <legend>Strategies</legend>
          <div className="checks">
            {strategies.map((s) => {
              const id = String(s.strategy_id);
              return (
                <label key={id} title={text(s.label)}>
                  <input
                    type="checkbox"
                    checked={chosen.includes(id)}
                    onChange={(e) =>
                      setChosen((c) => (e.target.checked ? [...c, id] : c.filter((x) => x !== id)))
                    }
                  />
                  {id}
                </label>
              );
            })}
          </div>
        </fieldset>
        <div className="form-grid">
          <OperatorField value={actor} onChange={setActor} />
          <Field label="Data source">
            {(id) => (
              <select id={id} value={source} onChange={(e) => setSource(e.target.value)}>
                <option value="">Default ({text(defaults.source)})</option>
                {sources.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Start date (blank = all available)">
            {(id) => <input id={id} type="date" value={start} onChange={(e) => setStart(e.target.value)} />}
          </Field>
          <Field label="End date (blank = latest)">
            {(id) => <input id={id} type="date" value={end} onChange={(e) => setEnd(e.target.value)} />}
          </Field>
          <Field label="Starting capital (USD)">
            {(id) => <input id={id} inputMode="decimal" value={capital} onChange={(e) => setCapital(e.target.value)} />}
          </Field>
          <Field label="Execution timing">
            {(id) => (
              <select id={id} value={execution} onChange={(e) => setExecution(e.target.value as Execution)}>
                {(Object.keys(EXECUTION_LABEL) as Execution[]).map((k) => (
                  <option key={k} value={k}>
                    {EXECUTION_LABEL[k]}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Extra execution delay (sessions)">
            {(id) => <input id={id} inputMode="numeric" value={delay} onChange={(e) => setDelay(e.target.value)} />}
          </Field>
        </div>
        <div className="checks">
          <label>
            <input type="checkbox" checked={synthetic} onChange={(e) => setSynthetic(e.target.checked)} /> Extend
            TQQQ/SQQQ with SYNTHETIC history
          </label>
        </div>
        {synthetic && <SyntheticWarning />}
        <details>
          <summary>Trading costs</summary>
          <div className="form-grid">
            {COSTS.map(([k, label]) => (
              <Field key={k} label={label}>
                {(id) => (
                  <input
                    id={id}
                    inputMode="decimal"
                    value={costs[k] ?? ""}
                    onChange={(e) => setCosts((c) => ({ ...c, [k]: e.target.value }))}
                  />
                )}
              </Field>
            ))}
          </div>
        </details>
        {leveraged && (
          <p className="muted small">
            Some selected strategies can hold TQQQ/SQQQ: they need TQQQ/SQQQ data (or synthetic history).
          </p>
        )}
        {problems.length > 0 && (
          <ul className="alert warning">
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        )}
        <div className="form-actions">
          <button type="submit" className="btn primary" disabled={!ok}>
            {m.busy ? "Queuing…" : "Run backtest"}
          </button>
          <span className="muted small">Defaults come from the current configuration ({text(options.config_version)}).</span>
        </div>
      </form>
      <ActionResult error={m.error} message={m.message} />
    </Section>
  );
}

function RunResult({ jobId }: { jobId: string }) {
  const r = useApi("/api/v1/backtests/runs/{job_id}", undefined, 5_000, { job_id: jobId });
  const run = (r.data?.run ?? null) as Row | null;
  const job = (r.data?.job ?? null) as Job | null;
  const s = (run?.summary ?? null) as Row | null;
  const headline = (s?.headline ?? {}) as Row;
  const curve = (s?.curve as Row[] | undefined) ?? [];
  const equity: Point[] = curve.map((p) => ({ x: String(p.date), y: num(p.equity) ?? NaN }));
  const drawdown: Point[] = curve.map((p) => ({ x: String(p.date), y: num(p.drawdown) ?? NaN }));
  const all = ((s?.metrics as Record<string, Record<string, Row>> | undefined)?.all ?? {}) as Record<string, Row>;
  const compare = Object.entries(all).map(([series, v]) => ({ series, ...v }));
  const unpriced = (s?.unpriced_instruments as string[] | undefined) ?? [];
  const notes = (s?.notes as string[] | undefined) ?? [];
  return (
    <Section title="Result" aside={job && <JobBadge status={job.status} />}>
      <LoadState loading={r.loading && !r.data} error={r.error} />
      {job && !run && (
        <p className="muted">
          {job.status === "failed" ? <span className="critical-ink">{job.error}</span> : `${job.message}…`}
        </p>
      )}
      {s && (
        <>
          <Disclaimer />
          <p>
            {(s.strategies as string[]).join(", ")} · {text(s.source)} ·{" "}
            {text((s.period as Row).start)} → {text((s.period as Row).end)} · config {text(s.config_version)}{" "}
            {s.has_synthetic ? <span className="tag synthetic">INCLUDES SYNTHETIC DATA</span> : <span className="tag real">REAL DATA ONLY</span>}
          </p>
          {s.has_synthetic ? <SyntheticWarning /> : null}
          {unpriced.length > 0 && (
            <p className="muted small">
              No {unpriced.join("/")} data was loaded; the selected strategies never hold them (the engine would stop the run if they did).
            </p>
          )}
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
          {notes.length > 0 && (
            <details>
              <summary>Notes</summary>
              <ul>
                {notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </details>
          )}
          <JsonView label="All metrics" value={s.metrics} />
        </>
      )}
    </Section>
  );
}

export default function BacktestsPage() {
  const opts = useApi("/api/v1/backtests/options", undefined, 60_000);
  const runs = useApi("/api/v1/backtests/runs", { limit: 50 }, 5_000);
  const legacy = useApi("/api/v1/backtests", { limit: 50 }, 120_000);
  const settings = useApi("/api/v1/settings", undefined, 120_000);
  const worker = useApi("/api/v1/jobs", { limit: 1 }, 30_000);
  const [selected, setSelected] = useState<string | null>(null);
  const runRows = (runs.data?.runs as Row[] | undefined) ?? [];
  const byJob = new Map(runRows.map((r) => [String(r.job_id), r]));
  const jobs = (runs.data?.jobs as Job[] | undefined) ?? [];
  const options = opts.data ? { ...opts.data, config_version: settings.data?.config_version } : null;
  return (
    <>
      <PageHeader title="Backtests">
        Run the platform&apos;s single backtest engine (same risk engine, costs and look-ahead
        protections as always) on the data stored by the worker. Results are saved in the
        database.
      </PageHeader>
      <Disclaimer />
      <LoadState loading={opts.loading && !opts.data} error={opts.error} />
      <WorkerNote worker={worker.data?.worker} />
      {options && (
        <NewBacktest
          options={options}
          onQueued={(j) => {
            setSelected(j.id);
            runs.reload();
          }}
        />
      )}
      {selected && <RunResult jobId={selected} />}
      <Section title="Backtest runs">
        <JobTable
          jobs={jobs}
          onChanged={runs.reload}
          empty="No backtests yet."
          extra={[
            { key: "strategies", label: "Strategies", render: (j) => ((j.params.strategies as string[]) ?? []).join(", ") },
            {
              key: "ret",
              label: "Total return (hyp.)",
              render: (j) => pct(((byJob.get(j.id)?.summary as Row | undefined)?.headline as Row | undefined)?.total_return),
            },
            {
              key: "open",
              label: "",
              render: (j) => (
                <button type="button" className="btn small" onClick={() => setSelected(j.id)}>
                  View
                </button>
              ),
            },
          ]}
        />
      </Section>
      <Section title="Report files on the worker (from the command line)">
        <DataTable
          rows={(legacy.data?.backtests as Row[] | undefined) ?? []}
          empty="No report files visible to the API. Runs started here are listed above."
          columns={[
            { key: "name", label: "Report" },
            { key: "has_report", label: "HTML report" },
          ]}
        />
      </Section>
    </>
  );
}
