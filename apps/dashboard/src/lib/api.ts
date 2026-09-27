/**
 * Typed client for the operator API (types generated from apps/api/openapi.json).
 *
 * - Every request sends the operator bearer token in the Authorization header,
 *   never in the URL; responses are never cached; no cookies or referrer.
 * - Reads are GET only. Writes are POSTs to the explicit allowlist in
 *   ``MUTATIONS`` (the same list the API enforces; see tests/unit/safety.test.ts):
 *   the kill switch plus the control plane (queue worker jobs, CSV uploads,
 *   strategy governance, runtime settings, shadow/paper scheduler control).
 *   Nothing here can place an order, enable live trading or touch a secret.
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

export const SAME_ORIGIN = "same-origin";

/**
 * The configured API origin (a URL, not a secret), or ``null`` when the API is
 * served from the dashboard's own origin through its reverse proxy (the
 * container deployment: NEXT_PUBLIC_AQ_API_ORIGIN=same-origin).
 */
export function configuredApiOrigin(): string | null {
  const configured = (process.env.NEXT_PUBLIC_AQ_API_ORIGIN ?? "").trim();
  if (configured === SAME_ORIGIN) return null;
  return configured || "http://localhost:8000";
}

/** Origin of the operator API. */
export function apiOrigin(): string {
  return configuredApiOrigin() ?? window.location.origin;
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

/** A raw request body sent as-is (CSV uploads), instead of JSON. */
class RawBody {
  constructor(
    readonly data: Blob,
    readonly contentType: string,
  ) {}
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
  let payload: BodyInit | undefined;
  if (body instanceof RawBody) {
    headers["Content-Type"] = body.contentType;
    payload = body.data;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: payload,
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal,
    });
  } catch (exc) {
    if (exc instanceof DOMException && exc.name === "AbortError") throw exc;
    throw new ApiError(0, "operator API unreachable", `is it running at ${apiOrigin()}?`);
  }
  let result: unknown = null;
  try {
    result = await res.json();
  } catch {
    result = null;
  }
  if (!res.ok) {
    const p = (result ?? {}) as { detail?: unknown; hint?: unknown };
    const detail =
      typeof p.detail === "string"
        ? p.detail
        : Array.isArray(p.detail)
          ? "request rejected by the API (validation error)"
          : res.statusText || "request failed";
    throw new ApiError(res.status, detail, typeof p.hint === "string" ? p.hint : undefined);
  }
  return result;
}

/** Fill ``{name}`` path parameters, each URL-encoded (never a raw user string in a path). */
export function fillPath(template: string, params: Record<string, string> = {}): string {
  return template.replace(/\{([a-z_]+)\}/g, (_m, name: string) => {
    const v = params[name];
    if (v === undefined || v === "") throw new Error(`missing path parameter ${name}`);
    return encodeURIComponent(v);
  });
}

/** GET one of the API's read endpoints (``params`` fills ``{name}`` path segments). */
export async function getJson<P extends ReadPath>(
  token: string,
  path: P,
  query?: ReadQuery<P>,
  signal?: AbortSignal,
  params?: Record<string, string>,
): Promise<ReadResponse<P>> {
  const url = buildUrl(fillPath(path, params), query as Record<string, unknown> | undefined);
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

/**
 * The complete allowlist of control-plane writes the dashboard may send
 * (the kill switch has its own function above). It must equal the API's
 * allowlist; tests/unit/safety.test.ts checks both against the OpenAPI schema.
 */
export const MUTATIONS = [
  "/api/v1/jobs/data/download",
  "/api/v1/jobs/data/validate",
  "/api/v1/jobs/data/synthesize",
  "/api/v1/jobs/data/inventory",
  "/api/v1/jobs/backtest",
  "/api/v1/jobs/research",
  "/api/v1/jobs/broker/verify",
  "/api/v1/jobs/{job_id}/cancel",
  "/api/v1/jobs/{job_id}/retry",
  "/api/v1/data/uploads/{upload_id}/import",
  "/api/v1/data/uploads/{upload_id}/discard",
  "/api/v1/strategies/{strategy_id}/params",
  "/api/v1/strategies/{strategy_id}/enabled",
  "/api/v1/strategies/{strategy_id}/lifecycle",
  "/api/v1/settings",
  "/api/v1/trading/scheduler/start",
  "/api/v1/trading/scheduler/stop",
  "/api/v1/trading/mode",
] as const satisfies readonly (keyof paths)[];

export type MutationPath = (typeof MUTATIONS)[number];
type PostOp<P extends MutationPath> = NonNullable<paths[P]["post"]>;
export type MutationBody<P extends MutationPath> = PostOp<P> extends {
  requestBody: { content: { "application/json": infer B } };
}
  ? B
  : never;
export type MutationResult = Schemas["MutationResult"];
export type Job = Schemas["Job"];

/** POST one allowlisted control-plane mutation (the API re-validates everything). */
export async function mutate<P extends MutationPath>(
  token: string,
  path: P,
  body: MutationBody<P>,
  params?: Record<string, string>,
): Promise<MutationResult> {
  if (!(MUTATIONS as readonly string[]).includes(path)) throw new Error("not an allowed mutation");
  const url = buildUrl(fillPath(path, params));
  return (await request(token, "POST", url, body)) as MutationResult;
}

export type UploadQuery = NonNullable<
  NonNullable<paths["/api/v1/data/uploads"]["post"]>["parameters"]["query"]
>;
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

/**
 * Upload a CSV for validation. Only the file's bytes and a plain display name are
 * sent; the server chooses where the data is stored (never a client path).
 */
export async function uploadCsv(
  token: string,
  query: UploadQuery,
  file: Blob,
): Promise<MutationResult> {
  if (file.size > MAX_UPLOAD_BYTES) throw new ApiError(413, "file is too large (max 25 MB)");
  const url = buildUrl("/api/v1/data/uploads", query as unknown as Record<string, unknown>);
  return (await request(token, "POST", url, new RawBody(file, "text/csv"))) as MutationResult;
}
