"use client";

import { AllocationChart, BandHistory, LineChart } from "@/components/charts";
import { DataTable, JsonView, LoadState, PageHeader, Section, Stat, StatusBadge } from "@/components/ui";
import type { Row } from "@/lib/api";
import { fixed, pct, text, when } from "@/lib/format";
import { bandStatus } from "@/lib/mode";
import { useApi } from "@/lib/session";
import { allocationSeries, bandSeries, riskDrawdownSeries } from "@/lib/series";

export default function RiskPage() {
  const r = useApi("/api/v1/risk", { limit: 250 });
  const d = r.data;
  const latest = d?.latest_decision ?? null;
  const history = d?.history ?? [];
  const flags = (latest?.flags_json as string[] | undefined) ?? [];
  return (
    <>
      <PageHeader title="Risk">Risk-engine decisions, drawdown bands and allocation history.</PageHeader>
      <LoadState loading={r.loading && !d} error={r.error} />
      <div className="stats card">
        <Stat
          label="Drawdown band"
          value={latest ? <StatusBadge status={bandStatus(latest.drawdown_band)} label={text(latest.drawdown_band)} /> : "—"}
          sub={when(latest?.created_at)}
        />
        <Stat label="Drawdown" value={pct(latest?.drawdown)} />
        <Stat label="Volatility scale" value={fixed(latest?.vol_scale)} />
        <Stat label="Regime" value={text(latest?.regime)} />
        <Stat
          label="Risk-increasing orders"
          value={
            latest ? (
              <StatusBadge
                status={latest.blocked_risk_increasing ? "critical" : "good"}
                label={latest.blocked_risk_increasing ? "blocked" : "allowed"}
              />
            ) : (
              "—"
            )
          }
        />
      </div>
      {flags.length > 0 && (
        <p className="alert warning" role="status">
          Flags: {flags.join(", ")}
        </p>
      )}
      <Section title="Allocation over time">
        <AllocationChart title="Approved weights per decision" points={allocationSeries(history)} />
      </Section>
      <div className="grid-2">
        <Section title="Drawdown band history">
          <BandHistory title="Band per decision" items={bandSeries(history)} />
        </Section>
        <Section title="Drawdown at decision time">
          <LineChart title="Drawdown" points={riskDrawdownSeries(history)} format={(v) => pct(v, 1)} area tone="critical" />
        </Section>
      </div>
      <Section title="Adjustments in the latest decision">
        <DataTable
          rows={(latest?.adjustments_json as Row[] | undefined) ?? []}
          empty="No adjustments: the proposal passed unchanged."
          columns={[
            { key: "rule", label: "Rule" },
            { key: "before", label: "Before" },
            { key: "after", label: "After" },
            { key: "reason", label: "Reason" },
          ]}
        />
      </Section>
      <Section title="Configured limits (read-only)">
        <JsonView label="Risk limits" value={d?.limits ?? {}} />
      </Section>
    </>
  );
}
