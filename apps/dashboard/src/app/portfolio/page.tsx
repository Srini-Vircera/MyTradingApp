"use client";

import { DataTable, LoadState, PageHeader, Section, Stat, StatusBadge } from "@/components/ui";
import { fixed, money, pct, text, weights, when } from "@/lib/format";
import { useApi } from "@/lib/session";
import { comparePositions } from "@/lib/series";

export default function PortfolioPage() {
  const p = useApi("/api/v1/portfolio", undefined, 30_000);
  const d = p.data;
  const rows = comparePositions(d?.positions ?? [], d?.expected_positions ?? []);
  const target = weights(d?.target?.weights_json);
  return (
    <>
      <PageHeader title="Portfolio">Broker account, positions and the latest target.</PageHeader>
      <LoadState loading={p.loading && !d} error={p.error} />
      <div className="stats card">
        <Stat label="Equity" value={money(d?.account?.equity)} sub={when(d?.account?.as_of)} />
        <Stat label="Cash" value={money(d?.account?.cash)} />
        <Stat label="Buying power" value={money(d?.account?.buying_power)} />
        <Stat
          label="Account type"
          value={
            d?.account ? (
              <StatusBadge
                status={d.account.is_paper ? "good" : "critical"}
                label={d.account.is_paper ? "paper account" : "REAL-MONEY ACCOUNT"}
              />
            ) : (
              "—"
            )
          }
        />
      </div>
      <Section title="Positions: broker vs expected">
        <DataTable
          rows={rows.map((r) => ({ ...r }))}
          empty="No positions recorded."
          columns={[
            { key: "symbol", label: "Instrument" },
            { key: "broker", label: "Broker qty", numeric: true, render: (r) => fixed(r.broker, 4) },
            { key: "expected", label: "Expected qty", numeric: true, render: (r) => fixed(r.expected, 4) },
            { key: "market_value", label: "Market value", numeric: true, render: (r) => money(r.market_value) },
            {
              key: "matches",
              label: "Check",
              render: (r) => (
                <StatusBadge status={r.matches ? "good" : "critical"} label={r.matches ? "matches" : "differs"} />
              ),
            },
          ]}
        />
      </Section>
      <Section title="Target weights">
        <DataTable
          rows={Object.entries(target).map(([symbol, w]) => ({ symbol, w }))}
          empty="No target recorded (all cash)."
          columns={[
            { key: "symbol", label: "Instrument" },
            { key: "w", label: "Weight", numeric: true, render: (r) => pct(r.w, 1) },
          ]}
        />
        {d?.target && (
          <p className="muted small">
            Cash {pct(d.target.cash_weight, 1)} · decision {text(d.target.decision_id)} · config{" "}
            {text(d.target.config_version)}
          </p>
        )}
      </Section>
    </>
  );
}
