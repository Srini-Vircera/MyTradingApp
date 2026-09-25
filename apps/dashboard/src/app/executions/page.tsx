"use client";

import { DataTable, LoadState, PageHeader, Section } from "@/components/ui";
import { fixed, money, when } from "@/lib/format";
import { useApi } from "@/lib/session";

export default function ExecutionsPage() {
  const e = useApi("/api/v1/executions", { limit: 300 });
  return (
    <>
      <PageHeader title="Executions">Fills reported by the broker, with slippage against the expected price.</PageHeader>
      <LoadState loading={e.loading && !e.data} error={e.error} />
      <Section title="Fills">
        <DataTable
          rows={e.data?.items ?? []}
          empty="No executions recorded."
          columns={[
            { key: "at", label: "At", render: (r) => when(r.at) },
            { key: "symbol", label: "Instrument" },
            { key: "side", label: "Side" },
            { key: "qty", label: "Qty", numeric: true, render: (r) => fixed(r.qty, 4) },
            { key: "price", label: "Price", numeric: true, render: (r) => money(r.price) },
            { key: "expected_price", label: "Expected", numeric: true, render: (r) => money(r.expected_price) },
            { key: "slippage_bps", label: "Slippage (bps)", numeric: true, render: (r) => fixed(r.slippage_bps, 1) },
            { key: "fee", label: "Fee", numeric: true, render: (r) => money(r.fee) },
            { key: "client_order_id", label: "Client id" },
          ]}
        />
      </Section>
    </>
  );
}
