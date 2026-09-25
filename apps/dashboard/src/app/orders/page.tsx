"use client";

import { DataTable, LoadState, PageHeader, Section, StatusBadge } from "@/components/ui";
import type { Row } from "@/lib/api";
import { fixed, text, when } from "@/lib/format";
import type { Status } from "@/lib/mode";
import { useApi } from "@/lib/session";

const STATE: Record<string, Status> = {
  filled: "good",
  cancelled: "neutral",
  expired: "neutral",
  rejected: "serious",
  unknown: "critical",
  failed: "critical",
};

export default function OrdersPage() {
  const o = useApi("/api/v1/orders", { limit: 200 }, 30_000);
  const sh = useApi("/api/v1/shadow-orders", { limit: 200 });
  const rc = useApi("/api/v1/reconciliation", { limit: 50 });
  const unknown = (o.data?.items ?? []).filter((r) => r.state === "unknown");
  return (
    <>
      <PageHeader title="Orders">
        Order intents and their broker events. Orders are placed only by the trading cycle; this dashboard
        cannot place, change or cancel orders.
      </PageHeader>
      <LoadState loading={o.loading && !o.data} error={o.error} />
      {unknown.length > 0 && (
        <p className="alert critical" role="alert">
          {unknown.length} order(s) in UNKNOWN state: trading is blocked until they are investigated and
          reconciled (see <code>aq trade status</code>).
        </p>
      )}
      <Section title="Order intents">
        <DataTable
          rows={o.data?.items ?? []}
          empty="No orders recorded."
          columns={[
            { key: "created_at", label: "Created", render: (r) => when(r.created_at) },
            { key: "symbol", label: "Instrument" },
            { key: "side", label: "Side" },
            { key: "quantity", label: "Qty", numeric: true, render: (r) => fixed(r.quantity, 4) },
            { key: "order_type", label: "Type" },
            {
              key: "state",
              label: "State",
              render: (r) => <StatusBadge status={STATE[String(r.state)] ?? "warning"} label={text(r.state)} />,
            },
            { key: "risk_increasing", label: "Risk-increasing" },
            { key: "client_order_id", label: "Client id" },
            {
              key: "events",
              label: "Events",
              render: (r) => {
                const ev = (r.events as Row[] | undefined) ?? [];
                return ev.length ? (
                  <details>
                    <summary>{ev.length}</summary>
                    <ul className="small">
                      {ev.map((e, i) => (
                        <li key={i}>
                          {when(e.at)}: {text(e.from_state)} → {text(e.to_state)}
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : (
                  "—"
                );
              },
            },
          ]}
        />
      </Section>
      <Section title="Shadow orders (computed, never sent)">
        <LoadState loading={false} error={sh.error} />
        <DataTable
          rows={sh.data?.items ?? []}
          empty="No shadow orders recorded."
          columns={[
            { key: "created_at", label: "Created", render: (r) => when(r.created_at) },
            { key: "symbol", label: "Instrument" },
            { key: "side", label: "Side" },
            { key: "quantity", label: "Qty", numeric: true, render: (r) => fixed(r.quantity, 4) },
            { key: "risk_increasing", label: "Risk-increasing" },
            { key: "cycle_id", label: "Cycle" },
          ]}
        />
      </Section>
      <Section title="Reconciliation">
        <LoadState loading={false} error={rc.error} />
        <DataTable
          rows={rc.data?.items ?? []}
          empty="No reconciliation reports."
          columns={[
            { key: "at", label: "At", render: (r) => when(r.at) },
            {
              key: "passed",
              label: "Result",
              render: (r) => <StatusBadge status={r.passed ? "good" : "critical"} label={r.passed ? "passed" : "FAILED"} />,
            },
            { key: "differences_json", label: "Differences", render: (r) => text(r.differences_json) },
            { key: "cycle_id", label: "Cycle" },
          ]}
        />
      </Section>
    </>
  );
}
