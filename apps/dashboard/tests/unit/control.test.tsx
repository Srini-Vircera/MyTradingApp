import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import BacktestsPage from "@/app/backtests/page";
import DataPage from "@/app/data/page";
import LiveReadinessPage from "@/app/live-readiness/page";
import StrategiesPage from "@/app/strategies/page";
import { fillPath, MAX_UPLOAD_BYTES, mutate, MUTATIONS, uploadCsv } from "@/lib/api";
import { mockFetch, TOKEN, withSession } from "./helpers";

const JOB = {
  id: "a".repeat(32),
  job_type: "backtest.run",
  title: "Backtest",
  params: { strategies: ["baseline_buy_hold"] },
  status: "queued",
  requested_by: "ann",
  requested_at: "2026-09-26T10:00:00Z",
  started_at: null,
  finished_at: null,
  heartbeat_at: null,
  progress: null,
  message: "waiting for the worker",
  result: null,
  error: null,
  config_version: null,
  retry_of: null,
  cancel_requested: false,
  retryable: false,
};

describe("control-plane client", () => {
  it("posts only allowlisted paths with encoded path parameters", async () => {
    const calls = mockFetch(() => ({ json: { ok: true, message: "queued", job: JOB, detail: {} } }));
    await mutate(TOKEN, "/api/v1/jobs/{job_id}/cancel", { actor: "ann", reason: "not needed" }, { job_id: "../x" });
    expect(new URL(calls[0]!.url).pathname).toBe("/api/v1/jobs/..%2Fx/cancel");
    expect(calls[0]!.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    await expect(
      mutate(TOKEN, "/api/v1/orders" as (typeof MUTATIONS)[number], {} as never),
    ).rejects.toThrow("not an allowed mutation");
    expect(calls).toHaveLength(1);
    expect(() => fillPath("/api/v1/jobs/{job_id}/cancel")).toThrow("missing path parameter");
  });

  it("uploads raw CSV bytes to the fixed upload path and refuses oversized files", async () => {
    const calls = mockFetch(() => ({ json: { ok: true, message: "validated", detail: {} } }));
    const file = new Blob(["date,open\n"], { type: "text/csv" });
    await uploadCsv(TOKEN, { symbol: "QQQ", kind: "bars", frequency: "1d", filename: "q.csv", actor: "ann" }, file);
    const url = new URL(calls[0]!.url);
    expect(url.pathname).toBe("/api/v1/data/uploads");
    expect(url.searchParams.get("symbol")).toBe("QQQ");
    expect(calls[0]!.headers["Content-Type"]).toBe("text/csv");
    const big = { size: MAX_UPLOAD_BYTES + 1 } as Blob;
    await expect(
      uploadCsv(TOKEN, { symbol: "QQQ", kind: "bars", frequency: "1d", filename: "q.csv", actor: "ann" }, big),
    ).rejects.toMatchObject({ status: 413 });
    expect(calls).toHaveLength(1);
  });
});

const OPTIONS = {
  strategies: [
    { strategy_id: "baseline_buy_hold", label: "buy and hold", long_only_1x: true },
    { strategy_id: "mom_time_series", label: "momentum", long_only_1x: false },
  ],
  sources: ["file"],
  defaults: {
    source: "file",
    initial_capital: 100000,
    execution: "near_close",
    execution_delay_bars: 0,
    use_synthetic_history: false,
    costs: { commission_per_share: 0, commission_per_order: 0, commission_minimum: 0, slippage_bps: 1, impact_coefficient_bps: 5, max_participation: 0.01 },
  },
};

describe("Backtest workspace", () => {
  it("shows the hypothetical disclaimer and queues a QQQ buy-and-hold job", async () => {
    const calls = mockFetch((url, method) => {
      if (method === "POST") return { json: { ok: true, message: "queued", job: JOB, detail: {} } };
      if (url.pathname.endsWith("/backtests/options")) return { json: OPTIONS };
      if (url.pathname.endsWith("/backtests/runs")) return { json: { runs: [], jobs: [] } };
      if (url.pathname.endsWith("/settings")) return { json: { config_version: "dev-abc" } };
      if (url.pathname.includes("/backtests/runs/")) return { json: { run: null, job: JOB } };
      if (url.pathname.endsWith("/jobs")) return { json: { jobs: [], worker: null } };
      return { json: { backtests: [], research: [] } };
    });
    withSession(<BacktestsPage />);
    expect(screen.getAllByText(/HYPOTHETICAL/).length).toBeGreaterThan(0);
    const run = await screen.findByRole("button", { name: "Run backtest" });
    expect(run).toBeDisabled(); // no operator name yet
    await userEvent.type(screen.getAllByLabelText(/Your name/)[0]!, "ann");
    expect(run).toBeEnabled();
    await userEvent.click(run);
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(new URL(post.url).pathname).toBe("/api/v1/jobs/backtest");
    expect(post.body).toMatchObject({ actor: "ann", params: { strategies: ["baseline_buy_hold"], initial_capital: 100000, costs: {} } });
    expect(screen.queryByText(/profit(able)? strategy|guaranteed/i)).toBeNull();
  });

  it("validates the form before anything is sent", async () => {
    const calls = mockFetch((url) =>
      url.pathname.endsWith("/backtests/options")
        ? { json: OPTIONS }
        : { json: { runs: [], jobs: [], backtests: [], research: [], worker: null } },
    );
    withSession(<BacktestsPage />);
    await screen.findByRole("button", { name: "Run backtest" });
    await userEvent.type(screen.getAllByLabelText(/Your name/)[0]!, "ann");
    fireEvent.change(screen.getByLabelText("Starting capital (USD)"), { target: { value: "-5" } });
    expect(screen.getByText(/starting capital must be a positive amount/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run backtest" })).toBeDisabled();
    expect(calls.every((c) => c.method === "GET")).toBe(true);
  });
});

describe("Data Manager", () => {
  it("labels synthetic data and the missing corporate-actions warning", async () => {
    mockFetch((url) => {
      if (url.pathname.endsWith("/data/datasets"))
        return {
          json: {
            datasets: [
              { source: "file", symbol: "QQQ", frequency: "1d", adjustment: "all", rows: 2514, first: "2016-09-26", last: "2026-09-25", validation_passed: true, is_synthetic: false, fresh: true, freshness: "ok", issues: [], warnings: ["no corporate-actions data for this symbol"] },
              { source: "synthetic", symbol: "TQQQ", frequency: "1d", adjustment: "all", rows: 100, validation_passed: true, is_synthetic: true, fresh: true, issues: [], warnings: [] },
            ],
            synthetic_warning: "SYNTHETIC calibration warning",
          },
        };
      return { json: { jobs: [], worker: null, uploads: [], providers: [] } };
    });
    withSession(<DataPage />);
    expect(await screen.findByText("REAL")).toBeTruthy();
    expect(screen.getAllByText("SYNTHETIC").length).toBeGreaterThan(0);
    expect(screen.getByText(/no corporate-actions data/)).toBeTruthy();
    expect(screen.getByText("2514")).toBeTruthy();
  });
});

describe("Strategy Manager", () => {
  it("offers lifecycle moves from the API only and never live approval", async () => {
    mockFetch(() => ({
      json: {
        strategies: [
          {
            strategy_id: "baseline_buy_hold", family: "benchmark", version_id: "v1", lifecycle: "validated",
            enabled: true, eligible_for_paper_or_shadow: false, params: {}, param_schema: [], param_grid: {},
            warmup_bars: 0, params_editable: false, eligible_modes: ["backtest"], evidence: {},
            allowed_transitions: [{ to: "paper", promotion: true }, { to: "research", promotion: false }],
          },
        ],
        lifecycle_events: [],
        promotion_confirm: "APPROVE PROMOTION",
      },
    }));
    withSession(<StrategiesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Manage" }));
    expect(screen.getByRole("button", { name: "Advance to paper" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Move back to research" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /live/i })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Advance to paper" }));
    expect(screen.getByLabelText(/to confirm/)).toBeTruthy();
    expect(screen.getByLabelText(/Reason \(at least 20 characters\)/)).toBeTruthy();
  });
});

describe("Live readiness", () => {
  it("is informational and locked", async () => {
    mockFetch(() => ({
      json: { live_possible: false, summary: "Live trading is locked.", items: [{ requirement: "A live broker adapter", satisfied: false, status: "none", how: "review" }] },
    }));
    withSession(<LiveReadinessPage />);
    expect(await screen.findByText(/LOCKED/)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
