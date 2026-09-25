import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { SessionProvider } from "@/lib/session";
import { saveToken } from "@/lib/token";

export const TOKEN = "t".repeat(40);

export function withSession(ui: ReactNode) {
  saveToken(TOKEN);
  return render(<SessionProvider>{ui}</SessionProvider>);
}

export interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

/** Replace fetch with a router of canned JSON responses; records every call. */
export function mockFetch(
  handler: (url: URL, method: string, body: unknown) => { status?: number; json: unknown },
) {
  const calls: Call[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ url: url.toString(), method, headers: (init?.headers ?? {}) as Record<string, string>, body });
    const r = handler(url, method, body);
    return new Response(JSON.stringify(r.json), {
      status: r.status ?? 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

export const BANNER = {
  environment: "paper",
  mode: "paper",
  uses_real_money: false,
  live_trading_enabled: false,
  config_version: "abc123def4567890",
  notice: "Paper/simulated results and backtests are hypothetical and are not a prediction of future returns.",
};
