"use client";

import { useState } from "react";
import { JobTable, WorkerNote } from "@/components/control";
import { JsonView, LoadState, PageHeader, Section } from "@/components/ui";
import type { Job } from "@/lib/api";
import { text, when } from "@/lib/format";
import { useApi } from "@/lib/session";

const FILTERS = ["", "queued", "running", "succeeded", "failed", "cancelled"] as const;

function JobDetail({ id }: { id: string }) {
  const d = useApi("/api/v1/jobs/{job_id}", undefined, 5_000, { job_id: id });
  const j = d.data;
  return (
    <Section title="Job details">
      <LoadState loading={d.loading && !j} error={d.error} />
      {j && (
        <>
          <dl className="kv">
            <dt>Job</dt>
            <dd>{j.title}</dd>
            <dt>Status</dt>
            <dd>{j.status}</dd>
            <dt>Message</dt>
            <dd>{j.message}</dd>
            <dt>Configuration version</dt>
            <dd>{text(j.config_version)}</dd>
            <dt>Started / finished</dt>
            <dd>
              {when(j.started_at)} / {when(j.finished_at)}
            </dd>
            {j.error && (
              <>
                <dt>Error</dt>
                <dd className="critical-ink">{j.error}</dd>
              </>
            )}
          </dl>
          <JsonView label="Parameters" value={j.params} />
          {j.result && <JsonView label="Result summary" value={j.result} />}
          <h3>Log</h3>
          <pre className="log">
            {j.logs.length === 0
              ? "(no log lines)"
              : j.logs.map((l) => `${text(l.at)}  ${text(l.level)}  ${text(l.message)}`).join("\n")}
          </pre>
        </>
      )}
    </Section>
  );
}

export default function JobsPage() {
  const [status, setStatus] = useState<(typeof FILTERS)[number]>("");
  const [selected, setSelected] = useState<string | null>(null);
  const jobs = useApi("/api/v1/jobs", { limit: 200, ...(status ? { status } : {}) }, 5_000);
  const d = jobs.data;
  return (
    <>
      <PageHeader title="Job Center">
        Every long task (data downloads, validation, backtests, research, broker checks) runs as a
        job on the worker; the browser only queues it and follows its progress. Jobs never contain
        credentials. Only repeatable jobs can be run again.
      </PageHeader>
      <LoadState loading={jobs.loading && !d} error={jobs.error} />
      <WorkerNote worker={d?.worker} />
      <Section
        title="Jobs"
        aside={
          <label className="inline">
            Show{" "}
            <select value={status} onChange={(e) => setStatus(e.target.value as (typeof FILTERS)[number])}>
              {FILTERS.map((f) => (
                <option key={f} value={f}>
                  {f || "all"}
                </option>
              ))}
            </select>
          </label>
        }
      >
        <JobTable
          jobs={d?.jobs ?? []}
          onChanged={jobs.reload}
          extra={[
            {
              key: "open",
              label: "Details",
              render: (j: Job) => (
                <button type="button" className="btn small" onClick={() => setSelected(j.id)}>
                  Open
                </button>
              ),
            },
          ]}
        />
      </Section>
      {selected && <JobDetail id={selected} />}
    </>
  );
}
