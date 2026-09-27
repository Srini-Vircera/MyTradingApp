"use client";

import { useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ActionResult } from "@/components/control";
import { DataTable, LoadState, PageHeader, Section, StatusBadge } from "@/components/ui";
import { mutate, type Row } from "@/lib/api";
import { text, when } from "@/lib/format";
import type { Status } from "@/lib/mode";
import { useApi, useMutation } from "@/lib/session";

const CATEGORIES = [
  "Data",
  "Backtesting",
  "Research",
  "Strategies",
  "Risk",
  "Trading",
  "Broker",
  "Notifications",
  "System",
] as const;

const CLASS_BADGE: Record<string, [Status, string]> = {
  runtime: ["good", "editable"],
  runtime_confirm: ["warning", "tighten-only"],
  restart: ["neutral", "needs redeploy"],
  secret: ["neutral", "secret"],
  immutable: ["critical", "locked"],
};

type Pending = Record<string, { value?: string | number | boolean; reset?: boolean }>;

function editable(r: Row): boolean {
  return r.class === "runtime" || r.class === "runtime_confirm";
}

/** Parse the typed text as the same JSON type as the current value. */
function parseAs(current: unknown, raw: string): { ok: boolean; value?: string | number | boolean } {
  if (typeof current === "number") {
    const n = Number(raw);
    return raw.trim() !== "" && Number.isFinite(n) ? { ok: true, value: n } : { ok: false };
  }
  if (typeof current === "boolean") return { ok: raw === "true" || raw === "false", value: raw === "true" };
  return raw.trim() ? { ok: true, value: raw.trim() } : { ok: false };
}

function Editor({ r, pending, setPending }: { r: Row; pending: Pending; setPending: (p: Pending) => void }) {
  const path = String(r.path);
  const p = pending[path];
  const choices = (r.choices as string[] | undefined) ?? [];
  const shown = p?.reset ? String(r.reviewed_value) : p?.value !== undefined ? String(p.value) : String(r.value);
  const [raw, setRaw] = useState(shown);
  const parsed = parseAs(r.value, raw);
  const update = (v: string) => {
    setRaw(v);
    const q = parseAs(r.value, v);
    const next = { ...pending };
    if (q.ok && String(q.value) !== String(r.value)) next[path] = { value: q.value };
    else delete next[path];
    setPending(next);
  };
  const input =
    choices.length || typeof r.value === "boolean" ? (
      <select aria-label={String(r.label)} value={raw} onChange={(e) => update(e.target.value)}>
        {(choices.length ? choices : ["true", "false"]).map((c) => (
          <option key={c}>{c}</option>
        ))}
      </select>
    ) : (
      <input aria-label={String(r.label)} value={raw} onChange={(e) => update(e.target.value)} />
    );
  return (
    <span className="setting-edit">
      {input}
      {!parsed.ok && <span className="critical-ink small">invalid value</span>}
      {Boolean(r.overridden) && (
        <button
          type="button"
          className="btn small"
          onClick={() => {
            setRaw(String(r.reviewed_value));
            setPending({ ...pending, [path]: { reset: true } });
          }}
        >
          Reset to reviewed
        </button>
      )}
    </span>
  );
}

export default function SettingsPage() {
  const s = useApi("/api/v1/settings", undefined, 60_000);
  const d = s.data as Row | null;
  const rows = (d?.settings as Row[] | undefined) ?? [];
  const [pending, setPending] = useState<Pending>({});
  const [saving, setSaving] = useState(false);
  const [version, setVersion] = useState(0);
  const m = useMutation();
  const byPath = new Map(rows.map((r) => [String(r.path), r]));
  const riskChange = Object.keys(pending).some((p) => byPath.get(p)?.class === "runtime_confirm");
  const runtime = (d?.runtime ?? {}) as Row;
  const secrets = (d?.secrets as Row[] | undefined) ?? [];
  const classes = (d?.classes ?? {}) as Record<string, string>;

  return (
    <>
      <PageHeader title="Settings">
        Structured configuration. Only settings marked “editable” or “tighten-only” change here;
        every change is validated, versioned and recorded with your name and reason.{" "}
        {text(d?.worker_note)}
      </PageHeader>
      <LoadState loading={s.loading && !d} error={s.error} />
      {d && (
        <>
          <p className="muted small">
            Active configuration <code>{text(d.config_version)}</code> (reviewed files:{" "}
            <code>{text(d.reviewed_config_version)}</code>) · runtime revision {text(runtime.revision)}
            {runtime.updated_by ? ` by ${text(runtime.updated_by)} ${when(runtime.updated_at)}` : ""}
          </p>
          <ul className="legend">
            {Object.entries(classes).map(([k, v]) => (
              <li key={k}>
                <StatusBadge status={(CLASS_BADGE[k] ?? ["neutral", k])[0]} label={(CLASS_BADGE[k] ?? ["neutral", k])[1]} /> {v}
              </li>
            ))}
          </ul>
          <div className="form-actions sticky-actions">
            <button type="button" className="btn primary" disabled={Object.keys(pending).length === 0} onClick={() => setSaving(true)}>
              Review and save {Object.keys(pending).length || ""} change{Object.keys(pending).length === 1 ? "" : "s"}…
            </button>
            {Object.keys(pending).length > 0 && (
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setPending({});
                  setVersion((v) => v + 1);
                }}
              >
                Discard edits
              </button>
            )}
          </div>
          <ActionResult error={m.error} message={m.message} />
          {CATEGORIES.map((cat) => {
            const inCat = rows.filter((r) => r.category === cat);
            const sec = secrets.filter((x) => x.category === cat);
            if (!inCat.length && !sec.length) return null;
            const ordered = [...inCat.filter(editable), ...inCat.filter((r) => !editable(r))];
            return (
              <Section key={cat} title={cat}>
                {cat === "Risk" && (
                  <p className="alert warning">
                    Risk limits can only be made stricter here, and need the confirmation phrase.
                    Loosening a limit requires a reviewed change to the risk file.
                  </p>
                )}
                <DataTable
                  key={version}
                  rows={ordered}
                  columns={[
                    { key: "label", label: "Setting", render: (r) => <span title={String(r.path)}>{text(r.label)}</span> },
                    {
                      key: "class",
                      label: "Kind",
                      render: (r) => {
                        const [st, lbl] = CLASS_BADGE[String(r.class)] ?? ["neutral", String(r.class)];
                        return <StatusBadge status={st} label={lbl} />;
                      },
                    },
                    {
                      key: "value",
                      label: "Value",
                      render: (r) =>
                        editable(r) ? (
                          <Editor r={r} pending={pending} setPending={setPending} />
                        ) : (
                          <code>{text(r.value)}</code>
                        ),
                    },
                    {
                      key: "reviewed_value",
                      label: "Reviewed value",
                      render: (r) => (r.overridden ? <code>{text(r.reviewed_value)}</code> : "same"),
                    },
                    { key: "why", label: "How to change", render: (r) => (editable(r) ? (r.tighter ? `stricter = ${text(r.tighter)}` : "here") : text(r.why)) },
                  ]}
                />
                {sec.length > 0 && (
                  <DataTable
                    caption="Secrets (set in the deployment platform; values are never shown or sent to the browser)"
                    rows={sec}
                    columns={[
                      { key: "label", label: "Secret" },
                      { key: "name", label: "Variable", render: (r) => <code>{text(r.name)}</code> },
                      {
                        key: "configured",
                        label: "Configured on the worker",
                        render: (r) => (r.configured === null || r.configured === undefined ? "unknown" : r.configured ? "yes" : "no"),
                      },
                    ]}
                  />
                )}
              </Section>
            );
          })}
          <Section title="Change history">
            <DataTable
              rows={(d.history as Row[] | undefined) ?? []}
              empty="No runtime changes yet."
              columns={[
                { key: "at", label: "At", render: (r) => when(r.at) },
                { key: "actor", label: "By" },
                { key: "path", label: "Setting" },
                { key: "old", label: "Old" },
                { key: "new", label: "New" },
                { key: "reason", label: "Reason" },
                { key: "config_version_after", label: "Config version" },
              ]}
            />
          </Section>
        </>
      )}
      {saving && d && (
        <ConfirmDialog
          title="Save settings"
          description={`${Object.entries(pending)
            .map(([p, v]) => `${p} → ${v.reset ? "reviewed value" : String(v.value)}`)
            .join("; ")}. Applies to the next job; a running scheduler needs a stop/start.`}
          phrase={riskChange ? text(d.risk_confirm) : ""}
          submitLabel="Save"
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setSaving(false)}
          onSubmit={async (v) => {
            const out = await m.run((t) =>
              mutate(t, "/api/v1/settings", {
                actor: v.actor,
                reason: v.reason,
                confirm: v.confirm,
                expected_revision: Number(runtime.revision ?? 0),
                changes: Object.entries(pending).map(([path, c]) =>
                  c.reset ? { path, reset: true, value: null } : { path, value: c.value ?? null, reset: false },
                ),
              }),
            );
            if (out) {
              setSaving(false);
              setPending({});
              setVersion((x) => x + 1);
              s.reload();
            }
          }}
        />
      )}
    </>
  );
}
