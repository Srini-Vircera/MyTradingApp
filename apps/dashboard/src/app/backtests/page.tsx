"use client";

import { DataTable, JsonView, LoadState, PageHeader, Section } from "@/components/ui";
import type { Row } from "@/lib/api";
import { fixed, pct, text } from "@/lib/format";
import { useApi } from "@/lib/session";

function strategyMetrics(r: Row): Row {
  const summary = r.summary as Row | null;
  const metrics = summary?.metrics as Record<string, Record<string, Row>> | undefined;
  return metrics?.all?.Strategy ?? {};
}

export default function BacktestsPage() {
  const b = useApi("/api/v1/backtests", { limit: 100 });
  const d = b.data;
  return (
    <>
      <PageHeader title="Backtests">
        Stored backtest and research reports. All backtest results are hypothetical, may include
        synthetic history, and are not a prediction of future returns. Open the full HTML report from the
        report directory on the server.
      </PageHeader>
      <LoadState loading={b.loading && !d} error={b.error} />
      <Section title="Backtest reports">
        <DataTable
          rows={d?.backtests ?? []}
          empty="No backtest reports found (run `aq backtest run`)."
          columns={[
            { key: "name", label: "Report" },
            { key: "cagr", label: "CAGR", numeric: true, render: (r) => pct(strategyMetrics(r).cagr) },
            { key: "sharpe", label: "Sharpe", numeric: true, render: (r) => fixed(strategyMetrics(r).sharpe) },
            { key: "mdd", label: "Max drawdown", numeric: true, render: (r) => pct(strategyMetrics(r).max_drawdown) },
            { key: "vol", label: "Volatility", numeric: true, render: (r) => pct(strategyMetrics(r).volatility) },
            { key: "sessions", label: "Sessions", numeric: true, render: (r) => text(strategyMetrics(r).sessions) },
            { key: "has_report", label: "HTML report" },
            { key: "details", label: "Details", render: (r) => <JsonView label="metrics.json" value={r.summary} /> },
          ]}
        />
      </Section>
      <Section title="Research runs">
        <DataTable
          rows={d?.research ?? []}
          empty="No research runs found (run `aq research run`)."
          columns={[
            { key: "name", label: "Run" },
            { key: "period", label: "Period", render: (r) => text((r.summary as Row | null)?.period) },
            { key: "trials", label: "Trials (run / registered)", render: (r) => {
              const s = (r.summary as Row | null) ?? {};
              return `${text(s.trials_this_run)} / ${text(s.trials_registered)}`;
            } },
            { key: "synthetic", label: "Synthetic sessions", render: (r) => text((r.summary as Row | null)?.synthetic_sessions) },
            { key: "has_report", label: "HTML report" },
          ]}
        />
      </Section>
    </>
  );
}
