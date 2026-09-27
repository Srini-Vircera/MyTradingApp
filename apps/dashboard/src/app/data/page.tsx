"use client";

import { useState, type FormEvent } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  ActionResult,
  Field,
  JobTable,
  OperatorField,
  SyntheticWarning,
  useOperator,
  WorkerNote,
} from "@/components/control";
import { DataTable, LoadState, PageHeader, Section, StatusBadge } from "@/components/ui";
import { MAX_UPLOAD_BYTES, mutate, uploadCsv, type Row } from "@/lib/api";
import { text, when } from "@/lib/format";
import { useApi, useMutation } from "@/lib/session";

const SYMBOL = /^[A-Z][A-Z0-9.]{0,9}$/;
type Freq = "1d" | "1min" | "5min" | "15min" | "30min";
type Provider = "file" | "alpaca" | "polygon";

function symbolsOf(v: string): string[] | undefined {
  const list = v
    .split(/[\s,]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  return list.length ? list : undefined;
}

function badSymbols(v: string): string[] {
  return (symbolsOf(v) ?? []).filter((s) => !SYMBOL.test(s));
}

function DatasetStatus({ r }: { r: Row }) {
  if (!r.validation_passed) return <StatusBadge status="critical" label="failed validation" />;
  if (r.fresh === false) return <StatusBadge status="warning" label="stale" />;
  return <StatusBadge status="good" label="valid" />;
}

function Preview({ upload }: { upload: Row }) {
  const p = (upload.preview ?? {}) as Row;
  const errors = (p.errors as string[] | undefined) ?? [];
  const issues = (p.issues as string[] | undefined) ?? [];
  const warnings = (p.warnings as string[] | undefined) ?? [];
  const head = (p.head as Row[] | undefined) ?? [];
  const tail = (p.tail as Row[] | undefined) ?? [];
  const cols = head[0] ? Object.keys(head[0]) : [];
  return (
    <div>
      <dl className="kv">
        <dt>File</dt>
        <dd>
          {text(upload.filename_hint)} · {text(upload.size_bytes)} bytes · sha256 {String(upload.sha256).slice(0, 12)}…
        </dd>
        <dt>Stored as</dt>
        <dd>
          {text(upload.symbol)} · {text(upload.kind)} · {text(upload.frequency)} (the server chooses the file name)
        </dd>
        <dt>Rows / range</dt>
        <dd>
          {text(p.rows)} rows · {text(p.first)} → {text(p.last)}
        </dd>
        <dt>Validation</dt>
        <dd>
          {upload.status === "invalid" ? (
            <StatusBadge status="critical" label="not usable" />
          ) : (
            <StatusBadge status="good" label="passed — review, then import" />
          )}
        </dd>
      </dl>
      {[...errors, ...issues].length > 0 && (
        <div className="alert critical" role="alert">
          <strong>Problems</strong>
          <ul>
            {[...errors, ...issues].map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      {warnings.length > 0 && (
        <div className="alert warning">
          <strong>Warnings</strong>
          <ul>
            {warnings.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      {head.length > 0 && (
        <DataTable
          caption="First and last rows as parsed"
          rows={[...head, ...tail]}
          columns={cols.map((c) => ({ key: c, label: c }))}
        />
      )}
    </div>
  );
}

function UploadPanel({ onChanged }: { onChanged: () => void }) {
  const [actor, setActor] = useOperator();
  const [symbol, setSymbol] = useState("QQQ");
  const [kind, setKind] = useState<"bars" | "actions">("bars");
  const [frequency, setFrequency] = useState<Freq>("1d");
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<Row | null>(null);
  const [discarding, setDiscarding] = useState(false);
  const m = useMutation();

  const tooBig = file !== null && file.size > MAX_UPLOAD_BYTES;
  const ok = actor.trim().length >= 2 && SYMBOL.test(symbol) && file !== null && !tooBig && !m.busy;

  async function send(e: FormEvent) {
    e.preventDefault();
    if (!ok || !file) return;
    setUpload(null);
    const name = file.name.replace(/[^A-Za-z0-9 _.()-]/g, "_").slice(-100) || "upload.csv";
    const out = await m.run((t) =>
      uploadCsv(t, { symbol, kind, frequency, filename: name.endsWith(".csv") ? name : `${name}.csv`, actor: actor.trim() }, file),
    );
    const u = (out?.detail as Row | undefined)?.upload as Row | undefined;
    if (u) setUpload(u);
    onChanged();
  }

  async function importIt() {
    if (!upload) return;
    const out = await m.run((t) =>
      mutate(t, "/api/v1/data/uploads/{upload_id}/import", { actor: actor.trim() }, { upload_id: String(upload.id) }),
    );
    if (out) {
      setUpload({ ...upload, status: "queued" });
      onChanged();
    }
  }

  return (
    <Section title="Upload a CSV file">
      <p className="muted">
        Daily bars need <code>date,open,high,low,close,volume</code> (extra columns such as{" "}
        <code>div,split</code> are ignored and reported). The file is validated first — dates,
        OHLC consistency, duplicates, malformed values — and becomes usable only after you review
        the preview and import it. The worker stores it under a server-chosen name.
      </p>
      <form onSubmit={send}>
        <div className="form-grid">
          <OperatorField value={actor} onChange={setActor} />
          <Field label="Symbol">
            {(id) => (
              <input id={id} value={symbol} maxLength={10} onChange={(e) => setSymbol(e.target.value.toUpperCase().trim())} />
            )}
          </Field>
          <Field label="Contents">
            {(id) => (
              <select id={id} value={kind} onChange={(e) => setKind(e.target.value as "bars" | "actions")}>
                <option value="bars">Price bars</option>
                <option value="actions">Corporate actions (splits/dividends)</option>
              </select>
            )}
          </Field>
          <Field label="Bar frequency">
            {(id) => (
              <select id={id} value={frequency} onChange={(e) => setFrequency(e.target.value as Freq)}>
                {["1d", "1min", "5min", "15min", "30min"].map((f) => (
                  <option key={f}>{f}</option>
                ))}
              </select>
            )}
          </Field>
          <Field label="CSV file (max 25 MB)">
            {(id) => (
              <input
                id={id}
                type="file"
                accept=".csv,text/csv"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            )}
          </Field>
        </div>
        {!SYMBOL.test(symbol) && <p className="alert warning">Symbol must be upper-case letters/digits (e.g. QQQ).</p>}
        {tooBig && <p className="alert critical">The file is larger than 25 MB.</p>}
        <div className="form-actions">
          <button type="submit" className="btn primary" disabled={!ok}>
            {m.busy ? "Checking…" : "Upload and validate"}
          </button>
        </div>
      </form>
      <ActionResult error={m.error} message={m.message} />
      {upload && (
        <>
          <Preview upload={upload} />
          <div className="form-actions">
            {upload.status === "validated" && (
              <button type="button" className="btn primary" disabled={m.busy} onClick={() => void importIt()}>
                Import into the data store
              </button>
            )}
            {(upload.status === "validated" || upload.status === "invalid") && (
              <button type="button" className="btn" onClick={() => setDiscarding(true)}>
                Discard upload
              </button>
            )}
            {upload.status === "queued" && <span className="muted">Import queued — follow it in the jobs list below.</span>}
          </div>
        </>
      )}
      {discarding && upload && (
        <ConfirmDialog
          title="Discard this upload"
          description="The file content is deleted; the record stays in the audit trail."
          phrase=""
          submitLabel="Discard"
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setDiscarding(false)}
          onSubmit={async (v) => {
            const out = await m.run((t) =>
              mutate(t, "/api/v1/data/uploads/{upload_id}/discard", { actor: v.actor, reason: v.reason }, { upload_id: String(upload.id) }),
            );
            if (out) {
              setDiscarding(false);
              setUpload(null);
              onChanged();
            }
          }}
        />
      )}
    </Section>
  );
}

function DownloadPanel({ options, onChanged }: { options: Row | null; onChanged: () => void }) {
  const [actor, setActor] = useOperator();
  const providers = (options?.providers as Row[] | undefined) ?? [];
  const [provider, setProvider] = useState<Provider | "">("");
  const [symbols, setSymbols] = useState("");
  const [frequency, setFrequency] = useState<Freq>("1d");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const m = useMutation();
  const bad = badSymbols(symbols);
  const ok = actor.trim().length >= 2 && bad.length === 0 && !(start && end && end < start) && !m.busy;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ok) return;
    const out = await m.run((t) =>
      mutate(t, "/api/v1/jobs/data/download", {
        actor: actor.trim(),
        params: {
          provider: provider || null,
          symbols: symbolsOf(symbols) ?? null,
          frequency,
          start: start || null,
          end: end || null,
        },
      }),
    );
    if (out) onChanged();
  }

  return (
    <Section title="Download or update data">
      <form onSubmit={submit}>
        <div className="form-grid">
          <OperatorField value={actor} onChange={setActor} />
          <Field label="Provider" help="Credentials stay on the worker; only whether they are configured is shown.">
            {(id) => (
              <select id={id} value={provider} onChange={(e) => setProvider(e.target.value as Provider | "")}>
                <option value="">Default ({text(options?.default_provider)})</option>
                {providers.map((p) => (
                  <option key={String(p.name)} value={String(p.name)} disabled={!p.configured}>
                    {text(p.label)}
                    {p.configured ? "" : " — not configured on the worker"}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Symbols (blank = QQQ, TQQQ, SQQQ + benchmarks)">
            {(id) => <input id={id} value={symbols} placeholder="QQQ, TQQQ" onChange={(e) => setSymbols(e.target.value)} />}
          </Field>
          <Field label="Frequency">
            {(id) => (
              <select id={id} value={frequency} onChange={(e) => setFrequency(e.target.value as Freq)}>
                {["1d", "1min", "5min", "15min", "30min"].map((f) => (
                  <option key={f}>{f}</option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Start date (blank = configured history start)">
            {(id) => <input id={id} type="date" value={start} onChange={(e) => setStart(e.target.value)} />}
          </Field>
          <Field label="End date (blank = latest)">
            {(id) => <input id={id} type="date" value={end} onChange={(e) => setEnd(e.target.value)} />}
          </Field>
        </div>
        {bad.length > 0 && <p className="alert warning">Not valid symbols: {bad.join(", ")}</p>}
        <div className="form-actions">
          <button type="submit" className="btn primary" disabled={!ok}>
            Queue download
          </button>
          <span className="muted small">
            The “Uploaded / imported files” provider re-reads the files already imported on the worker.
          </span>
        </div>
      </form>
      <ActionResult error={m.error} message={m.message} />
    </Section>
  );
}

function MaintenancePanel({ onChanged, warning }: { onChanged: () => void; warning: string }) {
  const [actor, setActor] = useOperator();
  const [symbols, setSymbols] = useState("");
  const [requireFresh, setRequireFresh] = useState(false);
  const [synth, setSynth] = useState(false);
  const m = useMutation();
  const bad = badSymbols(symbols);
  const ok = actor.trim().length >= 2 && bad.length === 0 && !m.busy;

  async function queue(kind: "validate" | "inventory") {
    const out =
      kind === "validate"
        ? await m.run((t) =>
            mutate(t, "/api/v1/jobs/data/validate", {
              actor: actor.trim(),
              params: { symbols: symbolsOf(symbols) ?? null, frequency: "1d", require_fresh: requireFresh },
            }),
          )
        : await m.run((t) => mutate(t, "/api/v1/jobs/data/inventory", { actor: actor.trim(), params: {} }));
    if (out) onChanged();
  }

  return (
    <Section title="Check data">
      <div className="form-grid">
        <OperatorField value={actor} onChange={setActor} />
        <Field label="Symbols to validate (blank = all tradeable)">
          {(id) => <input id={id} value={symbols} onChange={(e) => setSymbols(e.target.value)} />}
        </Field>
      </div>
      <div className="checks">
        <label>
          <input type="checkbox" checked={requireFresh} onChange={(e) => setRequireFresh(e.target.checked)} /> Also fail
          stale data (freshness check)
        </label>
      </div>
      <div className="form-actions">
        <button type="button" className="btn primary" disabled={!ok} onClick={() => void queue("validate")}>
          Validate data
        </button>
        <button type="button" className="btn" disabled={!ok} onClick={() => void queue("inventory")}>
          Refresh dataset list
        </button>
        <button type="button" className="btn" disabled={!ok} onClick={() => setSynth(true)}>
          Build SYNTHETIC TQQQ/SQQQ history…
        </button>
      </div>
      <ActionResult error={m.error} message={m.message} />
      {synth && (
        <ConfirmDialog
          title="Build SYNTHETIC leveraged-ETF history"
          description="Creates modelled TQQQ/SQQQ prices before their launch from QQQ. The result is labelled SYNTHETIC everywhere and is never used for trading."
          phrase=""
          submitLabel="Queue synthetic build"
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setSynth(false)}
          onSubmit={async (v) => {
            const out = await m.run((t) => mutate(t, "/api/v1/jobs/data/synthesize", { actor: v.actor, params: {} }));
            if (out) {
              setSynth(false);
              onChanged();
            }
          }}
        >
          <SyntheticWarning text={warning} />
        </ConfirmDialog>
      )}
    </Section>
  );
}

export default function DataPage() {
  const ds = useApi("/api/v1/data/datasets", undefined, 30_000);
  const opts = useApi("/api/v1/data/options", undefined, 60_000);
  const jobs = useApi("/api/v1/jobs", { limit: 30, job_type: "data" }, 5_000);
  const uploads = useApi("/api/v1/data/uploads", { limit: 20 }, 30_000);
  const reload = () => {
    ds.reload();
    jobs.reload();
    uploads.reload();
  };
  const d = ds.data;
  const warning = text(d?.synthetic_warning);
  const rows = ((d?.datasets as Row[] | undefined) ?? []).filter((r) => r.adjustment === "all" || r.is_synthetic);
  return (
    <>
      <PageHeader title="Data Manager">
        Market data stored on the worker. Uploads and downloads run as jobs; nothing is used by a
        backtest until it has passed validation.
      </PageHeader>
      <LoadState loading={ds.loading && !d} error={ds.error} />
      <WorkerNote worker={jobs.data?.worker} />
      <Section title="Datasets" aside={<span className="muted small">updated {when(d?.updated_at)}</span>}>
        <DataTable
          rows={rows}
          empty="No datasets yet. Upload a CSV or queue a download."
          columns={[
            { key: "symbol", label: "Symbol" },
            {
              key: "source",
              label: "Source",
              render: (r) => (
                <>
                  {text(r.source)}{" "}
                  {r.is_synthetic ? <span className="tag synthetic">SYNTHETIC</span> : <span className="tag real">REAL</span>}
                </>
              ),
            },
            { key: "frequency", label: "Frequency" },
            { key: "adjustment", label: "Adjustment" },
            { key: "first", label: "First date" },
            { key: "last", label: "Last date" },
            { key: "rows", label: "Rows", numeric: true },
            { key: "status", label: "Validation", render: (r) => <DatasetStatus r={r} /> },
            { key: "freshness", label: "Freshness" },
            {
              key: "warnings",
              label: "Warnings",
              render: (r) => {
                const w = [...((r.issues as string[]) ?? []), ...((r.warnings as string[]) ?? [])];
                return w.length ? (
                  <ul className="compact">
                    {w.map((x, i) => (
                      <li key={i}>{x}</li>
                    ))}
                  </ul>
                ) : (
                  "—"
                );
              },
            },
          ]}
        />
        <p className="muted small">
          Shown: the adjusted (“all”) series used by backtests. Raw and split-adjusted copies are
          kept alongside. Without a corporate-actions file, dividends are not included, so total
          return may be understated.
        </p>
      </Section>
      {rows.some((r) => r.is_synthetic) && <SyntheticWarning text={warning} />}
      <UploadPanel onChanged={reload} />
      <DownloadPanel options={opts.data ?? null} onChanged={reload} />
      <MaintenancePanel onChanged={reload} warning={warning} />
      <Section title="Recent uploads">
        <DataTable
          rows={(uploads.data?.uploads as Row[] | undefined) ?? []}
          empty="No uploads yet."
          columns={[
            { key: "created_at", label: "Uploaded", render: (r) => when(r.created_at) },
            { key: "symbol", label: "Symbol" },
            { key: "kind", label: "Contents" },
            { key: "filename_hint", label: "File" },
            { key: "status", label: "Status" },
            { key: "requested_by", label: "By" },
          ]}
        />
      </Section>
      <Section title="Data jobs">
        <JobTable jobs={jobs.data?.jobs ?? []} onChanged={reload} />
      </Section>
    </>
  );
}
