"use client";

import { useState, type FormEvent } from "react";
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
import { DataTable, LoadState, PageHeader, Section, StatusBadge } from "@/components/ui";
import { mutate, type Job, type Row } from "@/lib/api";
import { fixed, num, pct, text } from "@/lib/format";
import { useApi, useMutation } from "@/lib/session";

function Launch({ info, onQueued }: { info: Row; onQueued: (j: Job) => void }) {
  const defaults = (info.defaults ?? {}) as Row;
  const candidates = (info.candidates as string[] | undefined) ?? [];
  const eligible = (info.eligible as string[] | undefined) ?? [];
  const [actor, setActor] = useOperator();
  const [chosen, setChosen] = useState<string[]>([]);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [sims, setSims] = useState(String(defaults.simulations ?? 1000));
  const [scheme, setScheme] = useState<"rolling" | "anchored">((defaults.scheme as "rolling" | "anchored") ?? "rolling");
  const [synthetic, setSynthetic] = useState(Boolean(defaults.use_synthetic_history));
  const m = useMutation();
  const simsN = num(sims);
  const ok =
    actor.trim().length >= 2 &&
    simsN !== null &&
    simsN >= 10 &&
    simsN <= 100_000 &&
    chosen.length <= 25 &&
    !(start && end && end < start) &&
    !m.busy;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ok) return;
    const out = await m.run((t) =>
      mutate(t, "/api/v1/jobs/research", {
        actor: actor.trim(),
        params: {
          strategies: chosen.length ? chosen : null,
          start: start || null,
          end: end || null,
          simulations: simsN === null ? null : Math.trunc(simsN),
          scheme,
          use_synthetic_history: synthetic,
        },
      }),
    );
    if (out?.job) onQueued(out.job);
  }

  return (
    <Section title="Start a research run">
      <p className="muted">
        Walk-forward optimisation, parameter-robustness, deflated Sharpe ratio, probability of
        backtest overfitting, bootstrap, false-discovery control, Monte Carlo and the promotion
        gates — the same pipeline as <code>aq research run</code>. Runs can take a long time.
      </p>
      <form onSubmit={submit}>
        <fieldset className="plain">
          <legend>Strategies (none ticked = the default candidates: {candidates.join(", ") || "—"})</legend>
          <div className="checks">
            {eligible.map((id) => (
              <label key={id}>
                <input
                  type="checkbox"
                  checked={chosen.includes(id)}
                  onChange={(e) => setChosen((c) => (e.target.checked ? [...c, id] : c.filter((x) => x !== id)))}
                />
                {id}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="form-grid">
          <OperatorField value={actor} onChange={setActor} />
          <Field label="Start date (blank = all available)">
            {(id) => <input id={id} type="date" value={start} onChange={(e) => setStart(e.target.value)} />}
          </Field>
          <Field label="End date (blank = latest)">
            {(id) => <input id={id} type="date" value={end} onChange={(e) => setEnd(e.target.value)} />}
          </Field>
          <Field label="Monte Carlo resamples">
            {(id) => <input id={id} inputMode="numeric" value={sims} onChange={(e) => setSims(e.target.value)} />}
          </Field>
          <Field label="Walk-forward scheme">
            {(id) => (
              <select id={id} value={scheme} onChange={(e) => setScheme(e.target.value as "rolling" | "anchored")}>
                <option value="rolling">Rolling windows</option>
                <option value="anchored">Anchored (expanding) windows</option>
              </select>
            )}
          </Field>
        </div>
        <div className="checks">
          <label>
            <input type="checkbox" checked={synthetic} onChange={(e) => setSynthetic(e.target.checked)} /> Extend
            TQQQ/SQQQ with SYNTHETIC history
          </label>
        </div>
        {synthetic && <SyntheticWarning />}
        <div className="form-actions">
          <button type="submit" className="btn primary" disabled={!ok}>
            {m.busy ? "Queuing…" : "Start research run"}
          </button>
        </div>
      </form>
      <ActionResult error={m.error} message={m.message} />
    </Section>
  );
}

function Scorecards({ job }: { job: Job }) {
  const r = (job.result ?? null) as Row | null;
  if (!r) return <p className="muted">{job.status === "failed" ? job.error : `${job.message}…`}</p>;
  const ranked = (r.ranked as Row[] | undefined) ?? [];
  const transitions = (r.governance_transitions as Row[] | undefined) ?? [];
  const errors = Object.entries((r.errors ?? {}) as Record<string, string>);
  return (
    <>
      <p className="alert disclaimer">{HYPOTHETICAL}</p>
      <p>
        {text((r.period as Row)?.start)} → {text((r.period as Row)?.end)} · {text(r.sessions)} sessions ·{" "}
        {text(r.synthetic_sessions)} synthetic · trials this run {text(r.trials_this_run)} (registered{" "}
        {text(r.trials_registered)}) · config {text(r.config_version)}
      </p>
      {num(r.synthetic_sessions) ? <SyntheticWarning /> : null}
      <DataTable
        caption="Ranked scorecards (out-of-sample, after costs)"
        rows={ranked}
        columns={[
          { key: "rank", label: "#", numeric: true },
          { key: "strategy_id", label: "Strategy" },
          { key: "params", label: "Parameters" },
          {
            key: "passed",
            label: "Gates",
            render: (x) =>
              x.passed ? <StatusBadge status="good" label="all passed" /> : <StatusBadge status="warning" label="not passed" />,
          },
          { key: "oos_sharpe", label: "OOS Sharpe", numeric: true, render: (x) => fixed(x.oos_sharpe) },
          { key: "oos_benchmark_sharpe", label: "Benchmark Sharpe", numeric: true, render: (x) => fixed(x.oos_benchmark_sharpe) },
          { key: "oos_max_drawdown", label: "OOS max DD", numeric: true, render: (x) => pct(x.oos_max_drawdown) },
          { key: "dsr", label: "DSR", numeric: true, render: (x) => fixed(x.dsr) },
          { key: "pbo", label: "PBO", numeric: true, render: (x) => fixed(x.pbo) },
          { key: "bh_adjusted_p", label: "FDR-adjusted p", numeric: true, render: (x) => fixed(x.bh_adjusted_p, 3) },
          { key: "robustness", label: "Robustness", numeric: true, render: (x) => fixed(x.robustness) },
          { key: "potentially_overfit", label: "Overfit flag" },
          {
            key: "gates",
            label: "Gate details",
            render: (x) => (
              <details>
                <summary>{((x.gates as Row[]) ?? []).filter((g) => !g.passed).length} failed</summary>
                <ul className="compact">
                  {((x.gates as Row[]) ?? []).map((g, i) => (
                    <li key={i}>
                      {g.passed ? "✓" : "✗"} {text(g.name)}: {text(g.value)} ({text(g.rule)} {text(g.threshold)})
                    </li>
                  ))}
                </ul>
              </details>
            ),
          },
        ]}
      />
      {r.reality_check ? (
        <p className="muted small">
          Reality check across {text((r.reality_check as Row).models)} models: p = {fixed((r.reality_check as Row).reality_check_p, 3)},
          SPA p = {fixed((r.reality_check as Row).spa_p, 3)}.
        </p>
      ) : null}
      {errors.length > 0 && (
        <div className="alert warning">
          {errors.map(([k, v]) => (
            <div key={k}>
              {k}: {v}
            </div>
          ))}
        </div>
      )}
      <p className="alert info">
        {text(r.promotion)}{" "}
        {transitions.length > 0 &&
          `Governance ledger entries: ${transitions.map((t) => `${text(t.strategy_id)} ${text(t.from)}→${text(t.to)}`).join(", ")} (recorded only; the Strategy Manager decides).`}
      </p>
    </>
  );
}

export default function ResearchPage() {
  const info = useApi("/api/v1/research/runs", { limit: 30 }, 5_000);
  const worker = useApi("/api/v1/jobs", { limit: 1 }, 30_000);
  const [selected, setSelected] = useState<string | null>(null);
  const jobs = (info.data?.jobs as Job[] | undefined) ?? [];
  const current = jobs.find((j) => j.id === selected) ?? jobs.find((j) => j.status === "succeeded") ?? null;
  return (
    <>
      <PageHeader title="Research">
        Evidence about strategies, never a verdict: a research run cannot move a strategy to
        paper, shadow or live. Promotions are separate, human decisions in the Strategy Manager.
      </PageHeader>
      <LoadState loading={info.loading && !info.data} error={info.error} />
      <WorkerNote worker={worker.data?.worker} />
      {info.data && <Launch info={info.data} onQueued={(j) => { setSelected(j.id); info.reload(); }} />}
      <Section title="Research runs">
        <JobTable
          jobs={jobs}
          onChanged={info.reload}
          empty="No research runs yet."
          extra={[
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
      {current && (
        <Section title="Scorecards" aside={<JobBadge status={current.status} />}>
          <Scorecards job={current} />
        </Section>
      )}
    </>
  );
}
