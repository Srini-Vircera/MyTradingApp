"use client";

import Link from "next/link";
import { LineChart } from "@/components/charts";
import { KillSwitchState } from "@/components/KillSwitch";
import { useShell } from "@/components/Shell";
import { DataTable, LoadState, PageHeader, Section, Stat, StatusBadge } from "@/components/ui";
import type { Row } from "@/lib/api";
import { money, money0, pct, text, weights, when } from "@/lib/format";
import { useApi } from "@/lib/session";
import { performanceSeries } from "@/lib/series";

export default function OverviewPage() {
  const { killSwitch } = useShell();
  const o = useApi("/api/v1/overview", undefined, 30_000);
  const perf = useApi("/api/v1/performance", { limit: 250 }, 60_000);
  const d = o.data;
  const cycle = d?.latest_cycle ?? null;
  const steps = (cycle?.steps as Row[] | undefined) ?? [];
  const recon = d?.last_reconciliation ?? null;
  const target = weights(d?.target?.weights_json);
  const series = performanceSeries(perf.data?.items ?? []);

  return (
    <>
      <PageHeader title="Overview">Latest cycle, today&apos;s schedule and account state.</PageHeader>
      <LoadState loading={o.loading && !d} error={o.error} />
      <div className="stats card">
        <Stat label="Kill switch" value={<KillSwitchState status={killSwitch} />} />
        <Stat label="Equity" value={money(d?.latest_performance?.equity)} sub={text(d?.latest_performance?.session_date)} />
        <Stat label="Daily return" value={pct(d?.latest_performance?.daily_return)} />
        <Stat label="Drawdown" value={pct(d?.latest_performance?.drawdown)} />
        <Stat label="Open orders" value={d ? d.open_orders : "—"} />
        <Stat
          label="Last reconciliation"
          value={
            recon ? (
              <StatusBadge status={recon.passed ? "good" : "critical"} label={recon.passed ? "passed" : "FAILED"} />
            ) : (
              "—"
            )
          }
          sub={when(recon?.at)}
        />
      </div>
      <div className="grid-2">
        <Section title="Latest trading cycle">
          {cycle ? (
            <>
              <dl className="kv">
                <dt>Cycle</dt>
                <dd>
                  <Link href={`/explain/?cycle=${encodeURIComponent(String(cycle.cycle_id))}`}>
                    {String(cycle.cycle_id)}
                  </Link>
                </dd>
                <dt>Session</dt>
                <dd>{text(cycle.session_date)}</dd>
                <dt>Mode</dt>
                <dd>{text(cycle.mode)}</dd>
                <dt>Status</dt>
                <dd>{text(cycle.status)}</dd>
                <dt>Detail</dt>
                <dd>{text(cycle.detail)}</dd>
              </dl>
              <DataTable
                rows={steps}
                columns={[
                  { key: "step", label: "Step" },
                  { key: "status", label: "Status" },
                  { key: "detail", label: "Detail" },
                  { key: "at", label: "At", render: (r) => when(r.at) },
                ]}
              />
            </>
          ) : (
            <p className="muted">No trading cycle recorded yet.</p>
          )}
        </Section>
        <Section title="Today's session">
          {d?.session ? (
            <>
              <dl className="kv">
                <dt>Date</dt>
                <dd>{text(d.session.date)}</dd>
                <dt>Close</dt>
                <dd>
                  {when(d.session.close)}
                  {d.session.early_close ? " (early close)" : ""}
                </dd>
                <dt>Order cut-off</dt>
                <dd>{when(d.session.order_cutoff)}</dd>
              </dl>
              <DataTable
                rows={(d.session.steps as Row[] | undefined) ?? []}
                columns={[
                  { key: "name", label: "Scheduled step" },
                  { key: "at", label: "At", render: (r) => when(r.at) },
                ]}
              />
            </>
          ) : (
            <p className="muted">Market closed today (weekend or holiday): no cycle is scheduled.</p>
          )}
        </Section>
      </div>
      <Section title="Target allocation (latest decision)">
        {Object.keys(target).length ? (
          <DataTable
            rows={Object.entries(target).map(([symbol, w]) => ({ symbol, w }))}
            columns={[
              { key: "symbol", label: "Instrument" },
              { key: "w", label: "Weight", numeric: true, render: (r) => pct(r.w, 1) },
            ]}
          />
        ) : (
          <p className="muted">No target portfolio recorded yet (all cash).</p>
        )}
        {d?.target && (
          <p className="muted small">
            Cash {pct(d.target.cash_weight, 1)} · net underlying exposure{" "}
            {Number(d.target.net_underlying_exposure ?? 0).toFixed(2)}× · as of {when(d.target.as_of)}
          </p>
        )}
      </Section>
      <Section title="Equity">
        <LoadState loading={false} error={perf.error} />
        <LineChart title="Account equity (hypothetical until validated)" points={series.equity} format={(v) => money(v)} axisFormat={money0} />
      </Section>
    </>
  );
}
