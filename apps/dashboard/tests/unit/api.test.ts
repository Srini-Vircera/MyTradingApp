import { describe, expect, it, vi } from "vitest";
import { ApiError, apiOrigin, configuredApiOrigin, explainCycle, getJson, killSwitch } from "@/lib/api";
import { mockFetch, TOKEN } from "./helpers";

describe("api client", () => {
  it("sends the token as a bearer header, never in the URL, and disables caching", async () => {
    const calls = mockFetch(() => ({ json: { banner: {}, items: [], count: 0 } }));
    await getJson(TOKEN, "/api/v1/orders", { limit: 5, state: "filled" });
    const c = calls[0]!;
    expect(c.method).toBe("GET");
    expect(c.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(c.url).not.toContain(TOKEN);
    expect(c.url).toBe("http://localhost:8000/api/v1/orders?limit=5&state=filled");
    const init = (globalThis.fetch as unknown as { mock: { calls: [unknown, RequestInit][] } }).mock
      .calls[0]![1];
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
  });

  it("maps 401 and 503 responses to readable errors", async () => {
    mockFetch((url) =>
      url.pathname.endsWith("/overview")
        ? { status: 503, json: { detail: "audit database unavailable", hint: "check DATABASE_URL" } }
        : { status: 401, json: { detail: "missing or invalid bearer token" } },
    );
    const down = await getJson(TOKEN, "/api/v1/overview").catch((e: unknown) => e);
    expect(down).toBeInstanceOf(ApiError);
    expect((down as ApiError).status).toBe(503);
    expect((down as ApiError).hint).toBe("check DATABASE_URL");
    const denied = await getJson(TOKEN, "/api/v1/risk").catch((e: unknown) => e);
    expect((denied as ApiError).unauthorized).toBe(true);
  });

  it("reports an unreachable API without leaking the token", async () => {
    globalThis.fetch = (() => Promise.reject(new TypeError("failed"))) as typeof fetch;
    const err = (await getJson(TOKEN, "/api/v1/risk").catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(0);
    expect(err.message).not.toContain(TOKEN);
  });

  it("posts kill-switch actions to the two kill-switch endpoints only", async () => {
    const calls = mockFetch(() => ({
      json: { engaged: true, reason: "r", actor: "operator:ann", changed_at: null, fail_safe: false },
    }));
    const body = { actor: "ann", reason: "manual stop", confirm: "STOP AUTOMATED TRADING" };
    await killSwitch(TOKEN, "engage", body);
    await killSwitch(TOKEN, "release", { ...body, confirm: "RE-ENABLE TRADING" });
    expect(calls.map((c) => [c.method, new URL(c.url).pathname])).toEqual([
      ["POST", "/api/v1/kill-switch/engage"],
      ["POST", "/api/v1/kill-switch/release"],
    ]);
    expect(calls[0]!.body).toEqual(body);
  });

  it("encodes cycle ids in the explain path", async () => {
    const calls = mockFetch(() => ({ json: {} }));
    await explainCycle(TOKEN, "../kill-switch/engage");
    expect(new URL(calls[0]!.url).pathname).toBe("/api/v1/cycles/..%2Fkill-switch%2Fengage/explain");
    expect(calls[0]!.method).toBe("GET");
  });
});

describe("API origin", () => {
  it("uses the page's own origin when built for the same-origin reverse proxy", async () => {
    vi.stubEnv("NEXT_PUBLIC_AQ_API_ORIGIN", "same-origin");
    expect(configuredApiOrigin()).toBeNull();
    expect(apiOrigin()).toBe(window.location.origin);
    const calls = mockFetch(() => ({ json: { banner: {}, items: [], count: 0 } }));
    await getJson(TOKEN, "/api/v1/cycles", { limit: 1 });
    expect(calls[0]!.url).toBe(`${window.location.origin}/api/v1/cycles?limit=1`);
    vi.unstubAllEnvs();
  });

  it("defaults to the local API for development", () => {
    vi.stubEnv("NEXT_PUBLIC_AQ_API_ORIGIN", "");
    expect(apiOrigin()).toBe("http://localhost:8000");
    vi.unstubAllEnvs();
  });
});
