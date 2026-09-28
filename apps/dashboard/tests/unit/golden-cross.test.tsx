import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import BacktestsPage from "@/app/backtests/page";
import { ComparisonResult, CrossoverView } from "@/components/BacktestResult";
import { paramOverrides } from "@/components/StrategyParams";
import { mockFetch, withSession } from "./helpers";

const SCHEMA = [
  { name: "signal_symbol", type: "str", default: "QQQ", minimum: null, maximum: null, choices: null },
  { name: "max_long_exposure", type: "float", default: 1, minimum: 0, maximum: 3, choices: null },
  { name: "max_short_exposure", type: "float", default: 0, minimum: 0, maximum: 3, choices: null },
  { name: "fast_period", type: "int", default: 50, minimum: 2, maximum: 250, choices: null },
  { name: "slow_period", type: "int", default: 200, minimum: 3, maximum: 400, choices: null },
  { name: "ma_type", type: "str", default: "SMA", minimum: null, maximum: null, choices: ["SMA", "EMA"] },
  { name: "bearish_action", type: "str", default: "cash", minimum: null, maximum: null, choices: ["cash", "qqq_reduced", "sqqq"] },
  { name: "reduced_exposure", type: "float", default: 0.5, minimum: 0, maximum: 1, choices: null },
];
const GDC = {
  strategy_id: "golden_death_cross",
  implementation: "golden_death_cross",
  title: "Golden Cross / Death Cross",
  summary: "Classic moving-average trend strategy.",
  long_only_1x: true,
  params: { signal_symbol: "QQQ", max_long_exposure: 1, max_short_exposure: 0, fast_period: 50, slow_period: 200, ma_type: "SMA", bearish_action: "cash", reduced_exposure: 0.5 },
  param_schema: SCHEMA,
};

describe("per-run Golden/Death Cross parameters", () => {
  it("sends only changed, typed values", () => {
    expect(paramOverrides(GDC, {})).toEqual({ overrides: {}, problems: [] });
    const r = paramOverrides(GDC, { fast_period: "20", slow_period: "100", ma_type: "EMA" });
    expect(r).toEqual({ overrides: { fast_period: 20, slow_period: 100, ma_type: "EMA" }, problems: [] });
  });

  it("rejects invalid pairs and values before sending", () => {
    expect(paramOverrides(GDC, { fast_period: "200" }).problems.join()).toMatch(/slow MA period must be greater/);
    expect(paramOverrides(GDC, { fast_period: "1" }).problems.join()).toMatch(/≥ 2/);
    expect(paramOverrides(GDC, { fast_period: "20.5" }).problems.join()).toMatch(/whole number/);
    expect(paramOverrides(GDC, { ma_type: "WMA" }).problems.join()).toMatch(/one of SMA, EMA/);
    expect(paramOverrides(GDC, { bearish_action: "sqqq" }).problems.join()).toMatch(/inverse exposure above 0/);
  });
});

const OPTIONS = {
  strategies: [GDC, { strategy_id: "baseline_buy_hold", long_only_1x: true, params: {}, param_schema: SCHEMA.slice(0, 3) }],
  sources: ["file"],
  defaults: { source: "file", initial_capital: 100000, execution: "near_close", execution_delay_bars: 0, use_synthetic_history: false, costs: {} },
  run_modes: { ensemble: "Combined", independent: "Independent" },
};

describe("Backtests form", () => {
  it("offers an explicit run mode and sends per-run parameters", async () => {
    const calls = mockFetch((url, method) => {
      if (method === "POST") return { json: { ok: true, message: "queued", job: { id: "a".repeat(32), status: "queued" }, detail: {} } };
      if (url.pathname.endsWith("/backtests/options")) return { json: OPTIONS };
      return { json: { runs: [], jobs: [], worker: null, config_version: "x" } };
    });
    withSession(<BacktestsPage />);
    await userEvent.click(await screen.findByRole("checkbox", { name: /golden_death_cross/ }));
    // two strategies selected -> the run mode appears; default stays the combined ensemble
    const ensemble = screen.getByRole("radio", { name: /Combined ensemble/ });
    expect(ensemble).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: /Independent comparison/ }));
    expect(screen.getByText(/classic Golden Cross \/ Death Cross/)).toBeTruthy();
    expect(screen.getByText("CLASSIC 50/200 SMA")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Fast MA period"), { target: { value: "20" } });
    fireEvent.change(screen.getByLabelText("Slow MA period"), { target: { value: "100" } });
    fireEvent.change(screen.getByLabelText("MA type"), { target: { value: "EMA" } });
    expect(screen.getByText("VARIANT")).toBeTruthy();
    await userEvent.type(screen.getAllByLabelText(/Your name/)[0]!, "ann");
    await userEvent.click(screen.getByRole("button", { name: "Run backtest" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const body = calls.find((c) => c.method === "POST")!.body as { params: Record<string, unknown> };
    expect(body.params).toMatchObject({
      strategies: ["baseline_buy_hold", "golden_death_cross"],
      run_mode: "independent",
      strategy_params: { golden_death_cross: { fast_period: 20, slow_period: 100, ma_type: "EMA" } },
    });
  });

  it("blocks an invalid fast/slow pair", async () => {
    mockFetch((url) => (url.pathname.endsWith("/backtests/options") ? { json: OPTIONS } : { json: { runs: [], jobs: [], worker: null } }));
    withSession(<BacktestsPage />);
    await userEvent.click(await screen.findByRole("checkbox", { name: /golden_death_cross/ }));
    await userEvent.type(screen.getAllByLabelText(/Your name/)[0]!, "ann");
    fireEvent.change(screen.getByLabelText("Fast MA period"), { target: { value: "250" } });
    expect(screen.getByText(/slow MA period must be greater/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run backtest" })).toBeDisabled();
  });
});

describe("results", () => {
  const run = (sid: string, ret: number) => ({
    strategies: [sid], period: { start: "2017-01-03", end: "2019-12-31" }, source: "file", initial_capital: 100000,
    ending_equity: 100000 * (1 + ret), headline: { total_return: ret }, curve: [], metrics: {}, fills: 3,
    crossover: sid === "golden_death_cross" ? { golden_death_cross: {
      fast_period: 50, slow_period: 200, ma_type: "SMA", classic: true, bearish_action: "cash", first_signal: "2016-10-19",
      current_regime: "bullish", golden_cross_dates: ["2019-06-20"], death_cross_dates: ["2018-12-07"],
      events: [{ date: "2018-12-07", type: "death", fast_ma: 1, slow_ma: 2, orders: [{ date: "2018-12-07", side: "sell", quantity: 10, symbol: "QQQ" }] }],
      regime_periods: [{ regime: "bearish", from: "2018-12-07", to: "2019-06-20" }, { regime: "bullish", from: "2019-06-20", to: null }],
    } } : {},
  });

  it("shows an independent comparison side by side, never as an ensemble", () => {
    render(
      <ComparisonResult
        s={{
          mode: "independent", period: { start: "2017-01-03", end: "2019-12-31" }, source: "file", initial_capital: 100000,
          comparison: [{ strategy: "golden_death_cross", total_return: 0.1 }, { strategy: "baseline_buy_hold", total_return: 0.2 }],
          benchmarks: [{ series: "QQQ", total_return: 0.2 }],
          runs: [run("golden_death_cross", 0.1), run("baseline_buy_hold", 0.2)],
        }}
      />,
    );
    expect(screen.getByText(/Independent comparison/)).toBeTruthy();
    expect(screen.getByText(/not an ensemble/)).toBeTruthy();
    expect(screen.getByText("Side-by-side results (hypothetical)")).toBeTruthy();
    expect(screen.getAllByText("baseline_buy_hold").length).toBeGreaterThan(0);
  });

  it("lists Golden and Death Cross dates and the resulting fills", () => {
    render(<CrossoverView crossover={run("golden_death_cross", 0.1).crossover} />);
    expect(screen.getByText("2019-06-20", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText("2018-12-07", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText(/sell 10.00 QQQ/)).toBeTruthy();
    expect(screen.getByText("CLASSIC")).toBeTruthy();
  });
});
