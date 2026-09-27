"use client";

import { useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ActionResult } from "@/components/control";
import { DataTable, JsonView, LoadState, PageHeader, Section, StatusBadge } from "@/components/ui";
import { mutate, type Row } from "@/lib/api";
import { fixed, pct, text, when } from "@/lib/format";
import type { Status } from "@/lib/mode";
import { useApi, useMutation } from "@/lib/session";

const LIFECYCLE: Record<string, Status> = {
  research: "neutral",
  validated: "neutral",
  paper: "good",
  shadow: "warning",
  live_approved: "critical",
  disabled: "neutral",
};

type Target = "research" | "validated" | "paper" | "shadow" | "disabled";
const MIN_JUSTIFICATION = 20;

function ParamForm({ s, onSaved }: { s: Row; onSaved: () => void }) {
  const schema = (s.param_schema as Row[] | undefined) ?? [];
  const current = (s.params ?? {}) as Row;
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(schema.map((p) => [String(p.name), String(current[String(p.name)] ?? p.default ?? "")])),
  );
  const [open, setOpen] = useState(false);
  const m = useMutation();
  const editable = Boolean(s.params_editable);

  function parsed(): { params: Record<string, number | string | boolean>; problems: string[] } {
    const params: Record<string, number | string | boolean> = {};
    const problems: string[] = [];
    for (const p of schema) {
      const name = String(p.name);
      const raw = values[name] ?? "";
      if (raw === String(current[name] ?? p.default ?? "")) continue;
      if (p.type === "bool") {
        params[name] = raw === "true";
      } else if (p.type === "int" || p.type === "float") {
        const n = Number(raw);
        if (raw.trim() === "" || !Number.isFinite(n) || (p.type === "int" && !Number.isInteger(n))) {
          problems.push(`${name} must be ${p.type === "int" ? "a whole number" : "a number"}`);
          continue;
        }
        if (typeof p.minimum === "number" && n < p.minimum) problems.push(`${name} must be ≥ ${p.minimum}`);
        if (typeof p.maximum === "number" && n > p.maximum) problems.push(`${name} must be ≤ ${p.maximum}`);
        params[name] = n;
      } else {
        params[name] = raw;
      }
    }
    return { params, problems };
  }
  const { params, problems } = parsed();

  return (
    <>
      <div className="form-grid">
        {schema.map((p) => {
          const name = String(p.name);
          const choices = p.choices as unknown[] | null;
          return (
            <div className="field" key={name}>
              <label htmlFor={`${s.strategy_id}-${name}`}>
                {name}{" "}
                <span className="muted small">
                  ({text(p.type)}
                  {p.minimum !== null && p.minimum !== undefined ? `, min ${text(p.minimum)}` : ""}
                  {p.maximum !== null && p.maximum !== undefined ? `, max ${text(p.maximum)}` : ""})
                </span>
              </label>
              {choices || p.type === "bool" ? (
                <select
                  id={`${s.strategy_id}-${name}`}
                  disabled={!editable}
                  value={values[name]}
                  onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
                >
                  {(choices ?? [true, false]).map((c) => (
                    <option key={String(c)} value={String(c)}>
                      {String(c)}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={`${s.strategy_id}-${name}`}
                  disabled={!editable}
                  value={values[name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
                />
              )}
              {p.description ? <div className="muted small">{text(p.description)}</div> : null}
            </div>
          );
        })}
      </div>
      {!editable && (
        <p className="muted small">
          Parameters can change only while the strategy is in research or disabled (a change makes
          a new version without evidence).
        </p>
      )}
      {problems.length > 0 && (
        <ul className="alert warning">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {editable && (
        <div className="form-actions">
          <button
            type="button"
            className="btn primary"
            disabled={problems.length > 0 || Object.keys(params).length === 0}
            onClick={() => setOpen(true)}
          >
            Save parameters…
          </button>
        </div>
      )}
      <ActionResult error={m.error} message={m.message} />
      {open && (
        <ConfirmDialog
          title={`Save parameters for ${text(s.strategy_id)}`}
          description={`New values: ${JSON.stringify(params)}. This creates a new strategy version; earlier evidence no longer applies to it.`}
          phrase=""
          submitLabel="Save parameters"
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setOpen(false)}
          onSubmit={async (v) => {
            const out = await m.run((t) =>
              mutate(t, "/api/v1/strategies/{strategy_id}/params", { actor: v.actor, reason: v.reason, params }, {
                strategy_id: String(s.strategy_id),
              }),
            );
            if (out) {
              setOpen(false);
              onSaved();
            }
          }}
        />
      )}
    </>
  );
}

function Detail({ s, phrase, onChanged }: { s: Row; phrase: string; onChanged: () => void }) {
  const [lifecycle, setLifecycle] = useState<{ to: Target; promotion: boolean } | null>(null);
  const [toggle, setToggle] = useState(false);
  const m = useMutation();
  const sid = String(s.strategy_id);
  const transitions = (s.allowed_transitions as { to: Target; promotion: boolean }[] | undefined) ?? [];
  const ev = (s.evidence ?? {}) as Row;
  const research = ev.research as Row | null;
  const backtest = ev.backtest as Row | null;
  const headline = ((backtest?.summary as Row | undefined)?.headline ?? {}) as Row;
  return (
    <Section title={`Strategy ${sid}`}>
      <dl className="kv">
        <dt>Family / implementation</dt>
        <dd>
          {text(s.family)} / {text(s.implementation)} (code {text(s.code_version)})
        </dd>
        <dt>Version</dt>
        <dd>{text(s.version_id)}</dd>
        <dt>Warm-up</dt>
        <dd>{text(s.warmup_bars)} bars</dd>
        <dt>Lifecycle</dt>
        <dd>
          <StatusBadge status={LIFECYCLE[String(s.lifecycle)] ?? "neutral"} label={text(s.lifecycle)} />{" "}
          <span className="muted small">(reviewed file: {text((s.reviewed as Row | null)?.lifecycle)})</span>
        </dd>
        <dt>Enabled</dt>
        <dd>
          {s.enabled ? "yes" : "no"} — enabled allows backtests and research; trading also needs a
          paper-or-later lifecycle.
        </dd>
        <dt>Can trade (paper/shadow)</dt>
        <dd>{s.eligible_for_paper_or_shadow ? "yes" : "no"}</dd>
        <dt>Eligible modes</dt>
        <dd>{((s.eligible_modes as string[]) ?? []).join(", ") || "none"}</dd>
        <dt>Latest research evidence</dt>
        <dd>
          {research
            ? `${research.passed ? "passed all gates" : "did not pass all gates"} · OOS Sharpe ${fixed(research.oos_sharpe)} · DSR ${fixed(research.dsr)} · PBO ${fixed(research.pbo)} (${when(research.finished_at)})`
            : "none recorded"}
        </dd>
        <dt>Latest backtest (hypothetical)</dt>
        <dd>
          {backtest
            ? `CAGR ${pct(headline.cagr)} · Sharpe ${fixed(headline.sharpe)} · max DD ${pct(headline.max_drawdown)} (${when(backtest.created_at)})`
            : "none recorded"}
        </dd>
      </dl>
      <h3>Parameters</h3>
      <ParamForm key={JSON.stringify(s.params)} s={s} onSaved={onChanged} />
      <JsonView label="Parameter grid (research)" value={s.param_grid} />
      <h3>Enable / disable</h3>
      <div className="form-actions">
        <button type="button" className="btn" onClick={() => setToggle(true)}>
          {s.enabled ? "Disable for backtests and research" : "Enable for backtests and research"}
        </button>
      </div>
      <h3>Lifecycle</h3>
      <p className="muted small">
        research → validated → paper → shadow. Each move is checked by the governance rules and
        recorded with your name. Moving up needs a written justification (at least{" "}
        {MIN_JUSTIFICATION} characters) and the confirmation phrase. <strong>Live approval is never
        available here</strong>: it requires a reviewed change to the strategy file with a written
        approval.
      </p>
      <div className="form-actions">
        {transitions.map((t) => (
          <button
            key={t.to}
            type="button"
            className={t.promotion ? "btn primary" : "btn"}
            onClick={() => setLifecycle(t)}
          >
            {t.promotion ? `Advance to ${t.to}` : `Move back to ${t.to}`}
          </button>
        ))}
      </div>
      <ActionResult error={m.error} message={m.message} />
      {toggle && (
        <ConfirmDialog
          title={`${s.enabled ? "Disable" : "Enable"} ${sid}`}
          description="Changes whether the strategy can be backtested or researched. It does not change its lifecycle."
          phrase=""
          submitLabel={s.enabled ? "Disable" : "Enable"}
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setToggle(false)}
          onSubmit={async (v) => {
            const out = await m.run((t) =>
              mutate(t, "/api/v1/strategies/{strategy_id}/enabled", { actor: v.actor, reason: v.reason, enabled: !s.enabled }, { strategy_id: sid }),
            );
            if (out) {
              setToggle(false);
              onChanged();
            }
          }}
        />
      )}
      {lifecycle && (
        <ConfirmDialog
          title={`${sid}: ${text(s.lifecycle)} → ${lifecycle.to}`}
          description={
            lifecycle.promotion
              ? "You are approving this strategy for the next stage. Review its evidence first; past, hypothetical results do not show that it will be profitable."
              : "Moves the strategy back to an earlier stage."
          }
          phrase={lifecycle.promotion ? phrase : ""}
          minReason={lifecycle.promotion ? MIN_JUSTIFICATION : 5}
          submitLabel={lifecycle.promotion ? `Approve ${lifecycle.to}` : `Move to ${lifecycle.to}`}
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setLifecycle(null)}
          onSubmit={async (v) => {
            const out = await m.run((t) =>
              mutate(
                t,
                "/api/v1/strategies/{strategy_id}/lifecycle",
                { actor: v.actor, reason: v.reason, target: lifecycle.to, confirm: v.confirm },
                { strategy_id: sid },
              ),
            );
            if (out) {
              setLifecycle(null);
              onChanged();
            }
          }}
        />
      )}
    </Section>
  );
}

export default function StrategiesPage() {
  const s = useApi("/api/v1/strategies/manager", undefined, 30_000);
  const d = s.data;
  const rows = (d?.strategies as Row[] | undefined) ?? [];
  const [selected, setSelected] = useState<string | null>(null);
  const current = rows.find((r) => r.strategy_id === selected) ?? null;
  return (
    <>
      <PageHeader title="Strategy Manager">
        The strategy catalogue with governance. Nothing here edits strategy code or formulas, and no
        strategy is shown to have an edge: evidence is hypothetical.
      </PageHeader>
      <LoadState loading={s.loading && !d} error={s.error} />
      <Section title="Catalogue">
        <DataTable
          rows={rows}
          columns={[
            { key: "strategy_id", label: "Strategy" },
            { key: "family", label: "Family" },
            { key: "version_id", label: "Version" },
            {
              key: "lifecycle",
              label: "Lifecycle",
              render: (r: Row) => <StatusBadge status={LIFECYCLE[String(r.lifecycle)] ?? "neutral"} label={text(r.lifecycle)} />,
            },
            { key: "enabled", label: "Enabled" },
            { key: "eligible_for_paper_or_shadow", label: "Can trade (paper/shadow)" },
            { key: "params", label: "Parameters", render: (r) => text(r.params) },
            { key: "warmup_bars", label: "Warm-up", numeric: true },
            {
              key: "approval",
              label: "Live approval",
              render: (r) => {
                const a = r.approval as Row | null;
                return a ? `${text(a.approved_by)} · ${text(a.approved_on)}` : "—";
              },
            },
            {
              key: "open",
              label: "",
              render: (r) => (
                <button type="button" className="btn small" onClick={() => setSelected(String(r.strategy_id))}>
                  Manage
                </button>
              ),
            },
          ]}
        />
      </Section>
      {current && <Detail s={current} phrase={text(d?.promotion_confirm)} onChanged={s.reload} />}
      <Section title="Lifecycle history">
        <DataTable
          rows={(d?.lifecycle_events as Row[] | undefined) ?? []}
          empty="No lifecycle transitions recorded."
          columns={[
            { key: "at", label: "At", render: (r) => when(r.at) },
            { key: "strategy_id", label: "Strategy" },
            { key: "from_state", label: "From" },
            { key: "to_state", label: "To" },
            { key: "actor_id", label: "By" },
            { key: "actor_kind", label: "Actor kind" },
            { key: "reason", label: "Reason" },
          ]}
        />
      </Section>
    </>
  );
}
