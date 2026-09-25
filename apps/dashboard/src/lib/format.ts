/** Formatting helpers. The API sends decimals as strings (exact); convert only for display. */

export function num(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

export function pct(v: unknown, digits = 2): string {
  const n = num(v);
  return n === null ? "—" : `${(n * 100).toFixed(digits)}%`;
}

export function money(v: unknown): string {
  const n = num(v);
  return n === null
    ? "—"
    : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

/** Whole dollars, for chart axes. */
export function money0(v: unknown): string {
  const n = num(v);
  return n === null
    ? "—"
    : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

export function fixed(v: unknown, digits = 2): string {
  const n = num(v);
  return n === null ? "—" : n.toFixed(digits);
}

export function when(v: unknown): string {
  if (typeof v !== "string" || !v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleString("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

export function text(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** Weights as a {symbol: number} map (decimal strings accepted). */
export function weights(v: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (v && typeof v === "object" && !Array.isArray(v)) {
    for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
      const n = num(x);
      if (n !== null) out[k] = n;
    }
  }
  return out;
}
