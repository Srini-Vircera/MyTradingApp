/**
 * Static safety checks over the dashboard source:
 * - network calls happen only in src/lib/api.ts;
 * - the only non-GET requests are the two kill-switch actions;
 * - every API path used exists in the committed OpenAPI schema;
 * - the token is never persisted beyond the tab (no localStorage/cookies) or
 *   baked in at build time.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

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

  it("never uses PUT, PATCH or DELETE, and POSTs only in the kill-switch call", () => {
    for (const s of sources) {
      expect(s.code, s.path).not.toMatch(/["'](PUT|PATCH|DELETE)["']/);
    }
    const posts = sources.filter((s) => /["']POST["']/.test(s.code)).map((s) => s.path);
    expect(posts).toEqual(["src/lib/api.ts"]);
    const api = sources.find((s) => s.path === "src/lib/api.ts")!.code;
    const postCalls = api.match(/request\(token, "POST"/g) ?? [];
    expect(postCalls).toHaveLength(1);
    expect(api).toMatch(/\/api\/v1\/kill-switch\/\$\{action\}/);
    expect(api).toMatch(/action: "engage" \| "release"/);
  });

  it("the API schema exposes exactly the two kill-switch writes", () => {
    const writes = Object.entries(openapi.paths).flatMap(([p, ops]) =>
      Object.keys(ops)
        .filter((m) => m !== "get" && m !== "parameters")
        .map((m) => `${m.toUpperCase()} ${p}`),
    );
    expect(writes.sort()).toEqual([
      "POST /api/v1/kill-switch/engage",
      "POST /api/v1/kill-switch/release",
    ]);
  });

  it("uses only API paths that exist in the committed schema", () => {
    const known = Object.keys(openapi.paths);
    const used = new Set(
      sources.flatMap((s) => [...s.code.matchAll(/["'`](\/api\/v1\/[^"'`?]*)["'`]/g)].map((m) => m[1]!)),
    );
    expect(used.size).toBeGreaterThan(10);
    for (const p of used) {
      const pattern = new RegExp(`^${p.replace(/\$\{[^}]+\}/g, "[^/]+")}$`);
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

  it("has no controls that change the mode, orders or strategy lifecycle", () => {
    const forbidden =
      /(promote|approve strategy|go live|enable live|switch (to )?(live|paper|mode)|place order|submit order|cancel order|liquidate|flatten)/i;
    for (const s of sources) {
      const labels = [
        ...[...s.code.matchAll(/<button[^>]*>([\s\S]*?)<\/button>/g)].map((m) => m[1]!),
        ...[...s.code.matchAll(/submitLabel="([^"]*)"/g)].map((m) => m[1]!),
      ];
      if (s.path.endsWith("KillSwitch.tsx")) expect(labels.length).toBeGreaterThan(2);
      for (const label of labels) expect(label, s.path).not.toMatch(forbidden);
    }
  });
});
