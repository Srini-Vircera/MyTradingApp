"use client";

/** Building blocks for the control-plane pages (jobs, forms, results). */
import { useId, useState, type ReactNode } from "react";
import { mutate, type Job, type Row } from "@/lib/api";
import { pct, text, when } from "@/lib/format";
import type { Status } from "@/lib/mode";
import { loadOperator, saveOperator } from "@/lib/operator";
import { useMutation } from "@/lib/session";
import { ConfirmDialog } from "./ConfirmDialog";
import { DataTable, StatusBadge } from "./ui";

export const HYPOTHETICAL =
  "Backtests are HYPOTHETICAL: simulated fills and costs on historical data. They do not " +
  "show that a strategy is profitable and do not predict future returns.";

const JOB_STATUS: Record<string, Status> = {
  queued: "neutral",
  running: "warning",
  succeeded: "good",
  failed: "critical",
  cancelled: "neutral",
};

export function JobBadge({ status }: { status: unknown }) {
  const s = String(status ?? "unknown");
  return <StatusBadge status={JOB_STATUS[s] ?? "neutral"} label={s} />;
}

/** Operator name for queued jobs (prefilled for the tab; recorded in the audit trail). */
export function useOperator(): [string, (v: string) => void] {
  const [name, setName] = useState(() => loadOperator());
  return [
    name,
    (v: string) => {
      setName(v);
      saveOperator(v.trim());
    },
  ];
}

export function OperatorField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>Your name (recorded in the audit trail)</label>
      <input id={id} value={value} maxLength={128} autoComplete="off" onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

/** Success / error line after an action. */
export function ActionResult({ error, message }: { error: string | null; message: string | null }) {
  if (error)
    return (
      <p role="alert" className="alert critical">
        {error}
      </p>
    );
  if (message)
    return (
      <p role="status" className="alert info">
        {message}
      </p>
    );
  return null;
}

export function WorkerNote({ worker }: { worker: Row | null | undefined }) {
  if (!worker)
    return (
      <p className="alert warning" role="status">
        No worker has reported yet. Jobs wait in the queue until the worker service runs.
      </p>
    );
  const online = Boolean(worker.online);
  return (
    <p className="muted small">
      <StatusBadge status={online ? "good" : "critical"} label={online ? "worker online" : "worker offline"} />{" "}
      last seen {when(worker.last_seen_at)}
    </p>
  );
}

function Progress({ job }: { job: Job }) {
  if (job.status !== "running" && job.status !== "queued") return null;
  const p = typeof job.progress === "number" ? job.progress : null;
  return (
    <span className="small">
      {p === null ? "" : `${pct(p, 0)} · `}
      {job.message}
    </span>
  );
}

/** Jobs with cancel / retry (retry only for idempotent jobs; the API re-checks). */
export function JobTable({
  jobs,
  onChanged,
  empty = "No jobs yet.",
  extra,
}: {
  jobs: Job[];
  onChanged: () => void;
  empty?: string;
  extra?: { key: string; label: string; render: (j: Job) => ReactNode }[];
}) {
  const m = useMutation();
  const [cancelling, setCancelling] = useState<Job | null>(null);
  const [retrying, setRetrying] = useState<Job | null>(null);

  return (
    <>
      <ActionResult error={m.error} message={m.message} />
      <DataTable
        rows={jobs as unknown as Row[]}
        empty={empty}
        columns={[
          { key: "title", label: "Job" },
          { key: "status", label: "Status", render: (r) => <JobBadge status={r.status} /> },
          { key: "progress", label: "Progress", render: (r) => <Progress job={r as unknown as Job} /> },
          { key: "requested_by", label: "Requested by" },
          { key: "requested_at", label: "Requested", render: (r) => when(r.requested_at) },
          { key: "finished_at", label: "Finished", render: (r) => when(r.finished_at) },
          ...(extra ?? []).map((e) => ({ ...e, render: (r: Row) => e.render(r as unknown as Job) })),
          {
            key: "error",
            label: "Result",
            render: (r) =>
              r.error ? <span className="critical-ink">{text(r.error)}</span> : text((r as Row).config_version && `config ${text(r.config_version)}`),
          },
          {
            key: "actions",
            label: "",
            render: (r) => {
              const j = r as unknown as Job;
              return (
                <span className="row-actions">
                  {(j.status === "queued" || j.status === "running") && (
                    <button type="button" className="btn small" onClick={() => setCancelling(j)}>
                      Cancel
                    </button>
                  )}
                  {j.retryable && (
                    <button type="button" className="btn small" disabled={m.busy} onClick={() => setRetrying(j)}>
                      Run again
                    </button>
                  )}
                </span>
              );
            },
          },
        ]}
      />
      {retrying && (
        <ConfirmDialog
          title={`Run “${retrying.title}” again`}
          description="Queues a new job with the same parameters. The original job stays in the history."
          phrase=""
          minReason={0}
          submitLabel="Queue again"
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setRetrying(null)}
          onSubmit={async (v) => {
            const out = await m.run((t) =>
              mutate(t, "/api/v1/jobs/{job_id}/retry", { actor: v.actor }, { job_id: retrying.id }),
            );
            if (out) {
              setRetrying(null);
              onChanged();
            }
          }}
        />
      )}
      {cancelling && (
        <ConfirmDialog
          title={`Cancel “${cancelling.title}”`}
          description="A queued job is cancelled at once; a running job stops at its next safe checkpoint."
          phrase=""
          submitLabel="Cancel job"
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setCancelling(null)}
          onSubmit={async (v) => {
            const out = await m.run((t) =>
              mutate(t, "/api/v1/jobs/{job_id}/cancel", { actor: v.actor, reason: v.reason }, { job_id: cancelling.id }),
            );
            if (out) {
              setCancelling(null);
              onChanged();
            }
          }}
        />
      )}
    </>
  );
}

/** Labelled form controls. */
export function Field({ label, children, help }: { label: string; children: (id: string) => ReactNode; help?: string }) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {help && <div className="muted small">{help}</div>}
    </div>
  );
}

export function SyntheticWarning({ text: t }: { text?: string }) {
  return (
    <p className="alert warning" role="note">
      <strong>SYNTHETIC data</strong>{" "}
      {t ??
        "is modelled, not observed. Synthetic TQQQ/SQQQ history is an approximation calibrated to the real overlap; treat results that depend on it with extra caution."}
    </p>
  );
}
