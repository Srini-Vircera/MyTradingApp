import type { ReactNode } from "react";
import type { ApiError, Row } from "@/lib/api";
import { text } from "@/lib/format";
import type { Status } from "@/lib/mode";

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="page-header">
      <h1>{title}</h1>
      {children && <div className="muted">{children}</div>}
    </div>
  );
}

export function Section({
  title,
  children,
  aside,
}: {
  title: string;
  children: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <section className="card" aria-label={title}>
      <div className="section-head">
        <h2>{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** Loading / error state for a page. 503 means the audit database is down or not configured. */
export function LoadState({ loading, error }: { loading: boolean; error: ApiError | null }) {
  if (error) {
    return (
      <p role="alert" className="alert critical">
        {error.status === 503 ? "Audit database unavailable: " : ""}
        {error.detail}
        {error.hint ? ` — ${error.hint}` : ""}
      </p>
    );
  }
  return loading ? <p className="muted">Loading…</p> : null;
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

const ICON: Record<Status, string> = {
  good: "●",
  warning: "▲",
  serious: "◆",
  critical: "■",
  neutral: "○",
};

/** A status is always an icon plus a label, never colour alone. */
export function StatusBadge({ status, label }: { status: Status; label: string }) {
  return (
    <span className={`pill ${status}`}>
      <span aria-hidden="true">{ICON[status]}</span> {label}
    </span>
  );
}

export interface Column {
  key: string;
  label: string;
  render?: (row: Row) => ReactNode;
  numeric?: boolean;
}

export function DataTable({
  rows,
  columns,
  empty = "Nothing recorded yet.",
  caption,
}: {
  rows: Row[];
  columns: Column[];
  empty?: string;
  caption?: string;
}) {
  if (rows.length === 0) return <p className="muted">{empty}</p>;
  return (
    <div className="table-wrap">
      <table>
        {caption && <caption>{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} scope="col" className={c.numeric ? "num" : undefined}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c.key} className={c.numeric ? "num" : undefined}>
                  {c.render ? c.render(r) : text(r[c.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function JsonView({ value, label }: { value: unknown; label: string }) {
  return (
    <details className="json">
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
