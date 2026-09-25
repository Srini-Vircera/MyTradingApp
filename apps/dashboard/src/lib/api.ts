/**
 * Typed client for the operator API (types generated from apps/api/openapi.json).
 *
 * - Every request sends the operator bearer token in the Authorization header,
 *   never in the URL; responses are never cached; no cookies or referrer.
 * - Reads are GET only. The ONLY writes are the two kill-switch actions below
 *   (the API itself exposes nothing else that changes state; see
 *   tests/unit/safety.test.ts).
 */
import type { components, paths } from "./api-types";

export type Schemas = components["schemas"];
export type Banner = Schemas["Banner"];
export type KillSwitchView = Schemas["KillSwitchView"];
export type KillSwitchRequest = Schemas["KillSwitchRequest"];
export type Row = Record<string, unknown>;

/** API paths that have a GET operation. */
export type ReadPath = {
  [P in keyof paths]: paths[P]["get"] extends undefined ? never : P;
}[keyof paths];

type GetOp<P extends ReadPath> = NonNullable<paths[P]["get"]>;
export type ReadResponse<P extends ReadPath> = GetOp<P> extends {
  responses: { 200: { content: { "application/json": infer R } } };
}
  ? R
  : never;
export type ReadQuery<P extends ReadPath> = GetOp<P> extends { parameters: { query?: infer Q } }
  ? NonNullable<Q>
  : never;

export const ENGAGE_CONFIRMATION = "STOP AUTOMATED TRADING";
export const RELEASE_CONFIRMATION = "RE-ENABLE TRADING";

/** Origin of the operator API (a public URL, not a secret). */
export function apiOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_AQ_API_ORIGIN;
  return (configured && configured.trim()) || "http://localhost:8000";
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly hint?: string,
  ) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
  }

  get unauthorized(): boolean {
    return this.status === 401;
  }
}

function buildUrl(path: string, query?: Record<string, unknown>): string {
  const url = new URL(path, apiOrigin());
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  return url.toString();
}

async function request(
  token: string,
  method: "GET" | "POST",
  url: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<unknown> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    Authorization: `Bearer ${token}`,
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal,
    });
  } catch (exc) {
    if (exc instanceof DOMException && exc.name === "AbortError") throw exc;
    throw new ApiError(0, "operator API unreachable", `is it running at ${apiOrigin()}?`);
  }
  let payload: unknown = null;
  try {
    payload = await res.json();
  } catch {
    payload = null;
  }
  if (!res.ok) {
    const p = (payload ?? {}) as { detail?: unknown; hint?: unknown };
    const detail =
      typeof p.detail === "string"
        ? p.detail
        : Array.isArray(p.detail)
          ? "request rejected by the API (validation error)"
          : res.statusText || "request failed";
    throw new ApiError(res.status, detail, typeof p.hint === "string" ? p.hint : undefined);
  }
  return payload;
}

/** GET one of the API's read endpoints. */
export async function getJson<P extends ReadPath>(
  token: string,
  path: P,
  query?: ReadQuery<P>,
  signal?: AbortSignal,
): Promise<ReadResponse<P>> {
  const url = buildUrl(path, query as Record<string, unknown> | undefined);
  return (await request(token, "GET", url, undefined, signal)) as ReadResponse<P>;
}

/** The stored decision chain of one trading cycle. */
export async function explainCycle(
  token: string,
  cycleId: string,
  signal?: AbortSignal,
): Promise<Row> {
  const path = `/api/v1/cycles/${encodeURIComponent(cycleId)}/explain`;
  return (await request(token, "GET", buildUrl(path), undefined, signal)) as Row;
}

/**
 * The only state-changing calls the dashboard can make: engage or release the
 * kill switch. The API re-checks the typed confirmation phrase.
 */
export async function killSwitch(
  token: string,
  action: "engage" | "release",
  body: KillSwitchRequest,
): Promise<KillSwitchView> {
  const url = buildUrl(`/api/v1/kill-switch/${action}`);
  return (await request(token, "POST", url, body)) as KillSwitchView;
}
