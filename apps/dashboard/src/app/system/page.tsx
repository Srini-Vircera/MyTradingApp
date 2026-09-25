"use client";

import Link from "next/link";
import { KillSwitchPanel } from "@/components/KillSwitch";
import { useShell } from "@/components/Shell";
import { DataTable, LoadState, PageHeader, Section, Stat, StatusBadge } from "@/components/ui";
import { text, when } from "@/lib/format";
import { useApi } from "@/lib/session";

export default function SystemPage() {
  const { killSwitch, setKillSwitch } = useShell();
  const h = useApi("/api/v1/system/health", { limit: 50 }, 30_000);
  const c = useApi("/api/v1/cycles", { limit: 30 }, 60_000);
  const d = h.data;
  const db = d?.database ?? {};
  const dbStatus = !db.configured
    ? { s: "critical" as const, l: "not configured" }
    : db.reachable === false
      ? { s: "critical" as const, l: "unreachable" }
      : db.at_head === false
        ? { s: "serious" as const, l: "migrations pending" }
        : { s: "good" as const, l: "ok" };
  return (
    <>
      <PageHeader title="System Health">Services, the kill switch, recent cycles, errors and notifications.</PageHeader>
      <LoadState loading={h.loading && !d} error={h.error} />
      <div className="stats card">
        <Stat label="Version" value={d?.version ?? "—"} />
        <Stat label="Audit database" value={d ? <StatusBadge status={dbStatus.s} label={dbStatus.l} /> : "—"} sub={db.revision ? `revision ${text(db.revision)}` : undefined} />
        <Stat label="Latest cycle" value={text(d?.latest_cycle?.status)} sub={text(d?.latest_cycle?.session_date)} />
      </div>
      <Section title="Kill switch">
        <KillSwitchPanel status={killSwitch} onChanged={setKillSwitch} />
      </Section>
      <Section title="Recent trading cycles">
        <LoadState loading={false} error={c.error} />
        <DataTable
          rows={c.data?.items ?? []}
          empty="No cycles recorded."
          columns={[
            { key: "session_date", label: "Session" },
            {
              key: "cycle_id",
              label: "Cycle",
              render: (r) => (
                <Link href={`/explain/?cycle=${encodeURIComponent(String(r.cycle_id))}`}>{String(r.cycle_id)}</Link>
              ),
            },
            { key: "mode", label: "Mode" },
            { key: "status", label: "Status" },
            { key: "detail", label: "Detail" },
            { key: "finished_at", label: "Finished", render: (r) => when(r.finished_at) },
          ]}
        />
      </Section>
      <div className="grid-2">
        <Section title="Recent errors">
          <DataTable
            rows={d?.recent_errors ?? []}
            empty="No errors recorded."
            columns={[
              { key: "at", label: "At", render: (r) => when(r.at) },
              { key: "component", label: "Component" },
              { key: "error_type", label: "Type" },
              { key: "message", label: "Message" },
            ]}
          />
        </Section>
        <Section title="Recent notifications">
          <DataTable
            rows={d?.recent_notifications ?? []}
            empty="No notifications sent."
            columns={[
              { key: "at", label: "At", render: (r) => when(r.at) },
              { key: "event_type", label: "Event" },
              { key: "severity", label: "Severity" },
              { key: "channel", label: "Channel" },
              {
                key: "delivered",
                label: "Delivered",
                render: (r) => <StatusBadge status={r.delivered ? "good" : "serious"} label={r.delivered ? "yes" : "no"} />,
              },
            ]}
          />
        </Section>
      </div>
    </>
  );
}
