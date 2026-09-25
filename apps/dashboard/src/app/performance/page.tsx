"use client";

import { LineChart, ReturnBars } from "@/components/charts";
import { DataTable, LoadState, PageHeader, Section } from "@/components/ui";
import { money, money0, pct, text } from "@/lib/format";
import { useApi } from "@/lib/session";
import { performanceSeries } from "@/lib/series";

export default function PerformancePage() {
  const p = useApi("/api/v1/performance", { limit: 500 });
  const items = p.data?.items ?? [];
  const s = performanceSeries(items);
  return (
    <>
      <PageHeader title="Performance">
        Daily results of this environment. Paper and simulated results are hypothetical and are not a
        prediction of future returns.
      </PageHeader>
      <LoadState loading={p.loading && !p.data} error={p.error} />
      <Section title="Equity">
        <LineChart title="Equity" points={s.equity} format={(v) => money(v)} axisFormat={money0} />
      </Section>
      <div className="grid-2">
        <Section title="Drawdown">
          <LineChart title="Drawdown from peak" points={s.drawdown} format={(v) => pct(v, 1)} area tone="critical" />
        </Section>
        <Section title="Daily returns">
          <ReturnBars title="Daily return" points={s.returns} format={(v) => pct(v, 2)} />
        </Section>
      </div>
      <Section title="Daily table">
        <DataTable
          rows={[...items].reverse()}
          columns={[
            { key: "session_date", label: "Session" },
            { key: "equity", label: "Equity", numeric: true, render: (r) => money(r.equity) },
            { key: "pnl", label: "P&L", numeric: true, render: (r) => money(r.pnl) },
            { key: "daily_return", label: "Return", numeric: true, render: (r) => pct(r.daily_return) },
            { key: "drawdown", label: "Drawdown", numeric: true, render: (r) => pct(r.drawdown) },
            { key: "turnover", label: "Turnover", numeric: true, render: (r) => pct(r.turnover, 1) },
            { key: "benchmark_returns_json", label: "Benchmarks", render: (r) => text(r.benchmark_returns_json) },
          ]}
        />
      </Section>
    </>
  );
}
