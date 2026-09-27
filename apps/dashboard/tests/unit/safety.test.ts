/**
 * Static safety checks over the dashboard source:
 * - network calls happen only in src/lib/api.ts;
 * - the only non-GET requests are POSTs to the explicit allowlist (kill switch +
 *   control plane), which equals the API's own allowlist;
 * - nothing can enable live trading, grant live approval, place orders or read secrets;
 * - every API path used exists in the committed OpenAPI schema;
 * - the token is never persisted beyond the tab (no localStorage/cookies) or
 *   baked in at build time.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";
import { MUTATIONS } from "@/lib/api";

const ROOT = join(__dirname, "..", "..");
const SRC = join(ROOT, "src");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(f) ? [p] : [];
  });
}

const sources = files(SRC)
  .filter((f) => !f.endsWith("api-types.ts"))
  .map((f) => ({ path: relative(ROOT, f), code: stripComments(readFileSync(f, "utf8")) }));

function stripComments(code: string): string {
  return code.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

const openapi = JSON.parse(readFileSync(join(ROOT, "..", "api", "openapi.json"), "utf8")) as {
  paths: Record<string, Record<string, unknown>>;
};

describe("dashboard safety boundaries", () => {
  it("makes network requests only from the API client", () => {
    const offenders = sources
      .filter((s) => /\bfetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon/.test(s.code))
      .map((s) => s.path);
    expect(offenders).toEqual(["src/lib/api.ts"]);
  });

  it("never uses PUT, PATCH or DELETE; POSTs only through the allowlisted client calls", () => {
    for (const s of sources) {
      expect(s.code, s.path).not.toMatch(/["'](PUT|PATCH|DELETE)["']/);
    }
    const posts = sources.filter((s) => /["']POST["']/.test(s.code)).map((s) => s.path);
    expect(posts).toEqual(["src/lib/api.ts"]);
    const api = sources.find((s) => s.path === "src/lib/api.ts")!.code;
    // killSwitch, mutate (allowlist-checked) and uploadCsv (fixed path)
    expect(api.match(/request\(token, "POST"/g) ?? []).toHaveLength(3);
    expect(api).toMatch(/\/api\/v1\/kill-switch\/\$\{action\}/);
    expect(api).toMatch(/action: "engage" \| "release"/);
    expect(api).toMatch(/if \(!\(MUTATIONS as readonly string\[\]\)\.includes\(path\)\)/);
    expect(api).toMatch(/buildUrl\("\/api\/v1\/data\/uploads"/);
  });

  it("the dashboard's writes are exactly the API's allowlisted writes", () => {
    const writes = Object.entries(openapi.paths).flatMap(([p, ops]) =>
      Object.keys(ops)
        .filter((m) => m !== "get" && m !== "parameters")
        .map((m) => `${m.toUpperCase()} ${p}`),
    );
    expect(writes.every((w) => w.startsWith("POST "))).toBe(true);
    const client = [
      ...MUTATIONS,
      "/api/v1/data/uploads",
      "/api/v1/kill-switch/engage",
      "/api/v1/kill-switch/release",
    ].map((p) => `POST ${p}`);
    expect(new Set(client).size).toBe(client.length);
    expect([...client].sort()).toEqual([...writes].sort());
    for (const w of writes) {
      expect(w).not.toMatch(/live|order|secret|credential|env|shell|exec|submit/i);
    }
  });

  it("the lifecycle form can never request live approval", () => {
    const lifecycle = (openapi as unknown as {
      components: { schemas: Record<string, { properties: Record<string, { enum?: string[] }> }> };
    }).components.schemas.LifecycleRequest!;
    const targets = lifecycle.properties.target!.enum ?? [];
    expect(targets).toEqual(expect.arrayContaining(["research", "validated", "paper", "shadow"]));
    expect(targets).not.toContain("live_approved");
    const mode = (openapi as unknown as {
      components: { schemas: Record<string, { properties: Record<string, { enum?: string[] }> }> };
    }).components.schemas.ModeRequest!;
    expect(mode.properties.target!.enum).toEqual(["shadow", "paper"]);
    for (const s of sources) {
      expect(s.code, s.path).not.toMatch(/target:\s*["']live/);
      expect(s.code, s.path).not.toMatch(/["']\/api\/v1\/trading\/mode["'][^)]*live/);
    }
  });

  it("uses only API paths that exist in the committed schema", () => {
    const known = Object.keys(openapi.paths);
    const used = new Set(
      sources.flatMap((s) => [...s.code.matchAll(/["'`](\/api\/v1\/[^"'`?]*)["'`]/g)].map((m) => m[1]!)),
    );
    expect(used.size).toBeGreaterThan(10);
    for (const p of used) {
      const pattern = new RegExp(`^${p.replace(/\$?\{[^}]+\}/g, "[^/]+")}$`);
      expect(
        known.some((k) => pattern.test(k.replace(/\{[^}]+\}/g, "X"))),
        `${p} is not in apps/api/openapi.json`,
      ).toBe(true);
    }
  });

  it("never persists the token beyond the tab or reads it from the build", () => {
    for (const s of sources) {
      expect(s.code, s.path).not.toMatch(/localStorage|document\.cookie|indexedDB/);
      expect(s.code, s.path).not.toMatch(/process\.env\.\w*(TOKEN|SECRET|KEY|PASSWORD)/i);
      expect(s.code, s.path).not.toMatch(/console\.(log|info|debug)/);
    }
  });

  it("has no controls that enable live trading or touch orders", () => {
    // Shadow <-> paper and lifecycle moves up to shadow are deliberate, audited controls now;
    // anything that sounds like live trading or direct order handling stays forbidden.
    const forbidden =
      /(go live|enable live|live trading on|switch (to )?live|approve (for )?live|live.approv|place order|submit order|cancel order|liquidate|flatten|real money on)/i;
    for (const s of sources) {
      const labels = [
        ...[...s.code.matchAll(/<button[^>]*>([\s\S]*?)<\/button>/g)].map((m) => m[1]!),
        ...[...s.code.matchAll(/submitLabel=["{`]([^"}`]*)["}`]/g)].map((m) => m[1]!),
      ];
      if (s.path.endsWith("KillSwitch.tsx")) expect(labels.length).toBeGreaterThan(2);
      for (const label of labels) expect(label, s.path).not.toMatch(forbidden);
    }
  });

  it("never names deployment secrets or reads them from the build", () => {
    for (const s of sources) {
      for (const name of ["ALPACA_API_SECRET_KEY", "POLYGON_API_KEY", "DATABASE_URL", "SMTP_PASSWORD", "AQ_LIVE_TRADING_CONFIRM"]) {
        expect(s.code, s.path).not.toContain(name);
      }
    }
  });

  it("uploads send file bytes to a fixed path; the server picks the stored name", () => {
    const api = sources.find((s) => s.path === "src/lib/api.ts")!.code;
    expect(api).toMatch(/new RawBody\(file, "text\/csv"\)/);
    const data = sources.find((s) => s.path === "src/app/data/page.tsx")!.code;
    expect(data).toMatch(/replace\(\/\[\^A-Za-z0-9 _\.\(\)-\]\/g, "_"\)/);
    expect(data).not.toMatch(/webkitRelativePath|\.path\b/);
  });
});
