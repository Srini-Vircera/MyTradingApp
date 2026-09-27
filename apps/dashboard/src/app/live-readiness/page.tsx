"use client";

import { LoadState, PageHeader, Section, StatusBadge } from "@/components/ui";
import type { Row } from "@/lib/api";
import { text } from "@/lib/format";
import { useApi } from "@/lib/session";

export default function LiveReadinessPage() {
  const r = useApi("/api/v1/trading/live-readiness", undefined, 60_000);
  const d = r.data as Row | null;
  const items = (d?.items as Row[] | undefined) ?? [];
  return (
    <>
      <PageHeader title="Live Trading Readiness">
        Information only. Live trading is locked in this deployment and cannot be enabled from the
        dashboard.
      </PageHeader>
      <LoadState loading={r.loading && !d} error={r.error} />
      {d && (
        <>
          <p className="alert critical" role="note">
            <strong>Live trading: {d.live_possible ? "possible" : "LOCKED"}.</strong> {text(d.summary)}
          </p>
          <Section title="Prerequisites">
            <ul className="checklist">
              {items.map((i, k) => (
                <li key={k}>
                  <StatusBadge status={i.satisfied ? "good" : "critical"} label={i.satisfied ? "met" : "not met"} />
                  <div>
                    <strong>{text(i.requirement)}</strong> — {text(i.status)}
                    <div className="muted small">How: {text(i.how)}</div>
                  </div>
                </li>
              ))}
            </ul>
          </Section>
        </>
      )}
    </>
  );
}
