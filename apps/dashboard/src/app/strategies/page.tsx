"use client";

import { DataTable, LoadState, PageHeader, Section, StatusBadge } from "@/components/ui";
import type { Row } from "@/lib/api";
import { text, when } from "@/lib/format";
import type { Status } from "@/lib/mode";
import { useApi } from "@/lib/session";

const LIFECYCLE: Record<string, Status> = {
  research: "neutral",
  validated: "neutral",
  shadow: "warning",
  paper: "good",
  live_approved: "critical",
  retired: "neutral",
  suspended: "serious",
};

export default function StrategiesPage() {
  const s = useApi("/api/v1/strategies", { limit: 200 });
  const d = s.data;
  return (
    <>
      <PageHeader title="Strategies">
        Catalogue and lifecycle history. Promotion is a deliberate human action outside this dashboard
        (CLI with a named approver); nothing here can promote a strategy.
      </PageHeader>
      <LoadState loading={s.loading && !d} error={s.error} />
      <Section title="Catalogue">
        <DataTable
          rows={d?.strategies ?? []}
          columns={[
            { key: "strategy_id", label: "Strategy" },
            { key: "family", label: "Family" },
            {
              key: "lifecycle",
              label: "Lifecycle",
              render: (r: Row) => (
                <StatusBadge status={LIFECYCLE[String(r.lifecycle)] ?? "neutral"} label={text(r.lifecycle)} />
              ),
            },
            { key: "enabled", label: "Enabled" },
            { key: "eligible_modes", label: "Eligible in", render: (r) => (r.eligible_modes as string[]).join(", ") || "—" },
            {
              key: "approval",
              label: "Approval",
              render: (r) => {
                const a = r.approval as Row | null;
                return a ? `${text(a.approved_by)} · ${text(a.approved_on)}` : "—";
              },
            },
            { key: "version_id", label: "Version" },
          ]}
        />
      </Section>
      <Section title="Lifecycle events">
        <DataTable
          rows={d?.lifecycle_events ?? []}
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
