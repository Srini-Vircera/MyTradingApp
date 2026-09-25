"use client";

import { Heatmap } from "@/components/charts";
import { DataTable, LoadState, PageHeader, Section } from "@/components/ui";
import { fixed, when } from "@/lib/format";
import { useApi } from "@/lib/session";
import { signalCells } from "@/lib/series";

export default function SignalsPage() {
  const s = useApi("/api/v1/signals", { limit: 500 });
  const items = s.data?.items ?? [];
  return (
    <>
      <PageHeader title="Signals">Per-strategy signals recorded by each trading cycle.</PageHeader>
      <LoadState loading={s.loading && !s.data} error={s.error} />
      <Section title="Signal heatmap">
        <Heatmap title="Normalised score by strategy and cycle" cells={signalCells(items)} />
      </Section>
      <Section title="Latest signals">
        <DataTable
          rows={items.slice(0, 100)}
          columns={[
            { key: "timestamp", label: "Signal time", render: (r) => when(r.timestamp) },
            { key: "strategy_id", label: "Strategy" },
            { key: "direction", label: "Direction" },
            { key: "normalized_score", label: "Score", numeric: true, render: (r) => fixed(r.normalized_score) },
            { key: "confidence", label: "Confidence", numeric: true, render: (r) => fixed(r.confidence) },
            { key: "suggested_exposure", label: "Suggested exposure", numeric: true, render: (r) => fixed(r.suggested_exposure) },
            { key: "data_timestamp", label: "Data as of", render: (r) => when(r.data_timestamp) },
            { key: "reason", label: "Reason" },
          ]}
        />
      </Section>
    </>
  );
}
