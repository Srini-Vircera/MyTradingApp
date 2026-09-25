"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent } from "react";
import { JsonView, LoadState, PageHeader, Section } from "@/components/ui";
import { ApiError, explainCycle, type Row } from "@/lib/api";
import { useSession } from "@/lib/session";

function Explain() {
  const params = useSearchParams();
  const router = useRouter();
  const cycle = params.get("cycle") ?? "";
  const { token, signOut } = useSession();
  const [input, setInput] = useState(cycle);
  const [data, setData] = useState<Row | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setInput(cycle);
    if (!cycle || !token) return;
    const ctl = new AbortController();
    setLoading(true);
    setData(null);
    explainCycle(token, cycle, ctl.signal)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((exc: unknown) => {
        if (ctl.signal.aborted) return;
        const err = exc instanceof ApiError ? exc : new ApiError(0, "unexpected error");
        if (err.unauthorized) signOut("The API rejected the token. Enter it again.");
        setError(err);
      })
      .finally(() => !ctl.signal.aborted && setLoading(false));
    return () => ctl.abort();
  }, [cycle, token, signOut]);

  function submit(e: FormEvent) {
    e.preventDefault();
    router.push(`/explain/?cycle=${encodeURIComponent(input.trim())}`);
  }

  const sections = data ? Object.entries(data).filter(([k]) => k !== "banner") : [];
  return (
    <>
      <PageHeader title="Explain a decision">
        The stored decision chain of one trading cycle: data, signals, ensemble, risk, target, orders and
        fills.
      </PageHeader>
      <form className="card" onSubmit={submit}>
        <label htmlFor="cycle">Cycle id</label>
        <input id="cycle" value={input} onChange={(e) => setInput(e.target.value)} autoComplete="off" />
      </form>
      <LoadState loading={loading} error={error} />
      {sections.length > 0 && (
        <Section title={`Cycle ${cycle}`}>
          {sections.map(([k, v]) => (
            <JsonView key={k} label={k} value={v} />
          ))}
        </Section>
      )}
    </>
  );
}

export default function ExplainPage() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <Explain />
    </Suspense>
  );
}
