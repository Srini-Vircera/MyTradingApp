import type { Page, Request } from "@playwright/test";

export const API = "http://localhost:8000";
export const TOKEN = "e2e-operator-token-0123456789abcdef";

export const BANNER = {
  environment: "paper",
  mode: "paper",
  uses_real_money: false,
  live_trading_enabled: false,
  config_version: "cfg0123456789abcdef",
  notice: "Paper/simulated results and backtests are hypothetical and are not a prediction of future returns.",
};

const days = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"];
const equity = [100000, 101200, 100400, 102100];
const risk = days
  .map((d, i) => ({
    decision_id: `dec-${i}`,
    cycle_id: `cyc-${d}`,
    created_at: `${d}T19:40:00+00:00`,
    approved_weights_json: i % 2 ? { QQQ: "0.4", TQQQ: "0.2" } : { TQQQ: "0.5" },
    adjustments_json: [{ rule: "vol_target", before: 0.6, after: 0.5, reason: "vol scale 0.83" }],
    drawdown_band: i === 2 ? "caution" : "normal",
    drawdown: i === 2 ? 0.11 : 0.01,
    vol_scale: 0.83,
    regime: "bull",
    blocked_risk_increasing: false,
    flags_json: [],
  }))
  .reverse();

export function fixtures(banner = BANNER): Record<string, unknown> {
  const ks = { engaged: false, reason: "released after review", actor: "operator:ann", changed_at: "2026-09-24T13:00:00+00:00", fail_safe: false };
  const cycle = {
    cycle_id: "cyc-2026-09-24",
    session_date: "2026-09-24",
    environment: "paper",
    mode: "paper",
    status: "completed",
    detail: "",
    config_version: banner.config_version,
    started_at: "2026-09-24T19:30:00+00:00",
    finished_at: "2026-09-24T20:05:00+00:00",
    steps: [
      { step: "preflight", status: "done", detail: "", at: "2026-09-24T19:30:00+00:00" },
      { step: "reconcile", status: "done", detail: "passed", at: "2026-09-24T20:05:00+00:00" },
    ],
  };
  const target = { decision_id: "dec-3", as_of: "2026-09-24T19:40:00+00:00", weights_json: { TQQQ: "0.5" }, cash_weight: "0.5", net_underlying_exposure: 1.5, config_version: banner.config_version };
  const perf = days.map((d, i) => ({ session_date: d, environment: "paper", equity: String(equity[i]), pnl: "0", daily_return: i ? equity[i]! / equity[i - 1]! - 1 : 0, drawdown: i === 2 ? 0.008 : 0, turnover: 0.1, benchmark_returns_json: { QQQ: 0.002 } }));
  const page = (items: unknown[]) => ({ banner, items, count: items.length });
  return {
    "/api/v1/configuration": {
      banner,
      config_version: banner.config_version,
      warnings: [],
      live_trading_lock: { mode: banner.mode, uses_real_money: banner.uses_real_money, live_trading_enabled_in_config: banner.live_trading_enabled, changeable_via_api: false, how_to_change: "edit the YAML configuration and restart; see docs/SAFETY.md" },
      settings: { trading: { mode: banner.mode }, broker: { api_key: "***" } },
    },
    "/api/v1/kill-switch": ks,
    "/api/v1/overview": {
      banner,
      kill_switch: ks,
      session: { date: "2026-09-25", close: "2026-09-25T16:00:00-04:00", early_close: false, order_cutoff: "2026-09-25T15:50:00-04:00", steps: [{ name: "signals", at: "2026-09-25T15:30:00-04:00" }] },
      latest_cycle: cycle,
      latest_performance: perf[perf.length - 1],
      target,
      open_orders: 0,
      last_reconciliation: { id: 1, cycle_id: cycle.cycle_id, passed: true, differences_json: {}, at: "2026-09-24T20:05:00+00:00" },
    },
    "/api/v1/performance": page(perf),
    "/api/v1/portfolio": {
      banner,
      account: { equity: "102100", cash: "51050", buying_power: "102100", is_paper: true, as_of: "2026-09-24T20:05:00+00:00" },
      positions: [{ symbol: "TQQQ", quantity: "700", market_value: "51050" }],
      expected_positions: [{ symbol: "TQQQ", quantity: "700" }],
      target,
    },
    "/api/v1/strategies": {
      banner,
      strategies: [{ strategy_id: "trend_ma_200", family: "trend", implementation: "x", version_id: "trend_ma_200@1", lifecycle: "paper", enabled: true, eligible_modes: ["backtest", "paper", "shadow"], params: {}, warmup_bars: 200, approval: null }],
      lifecycle_events: [{ at: "2026-09-01T12:00:00+00:00", strategy_id: "trend_ma_200", from_state: "validated", to_state: "paper", actor_id: "ann", actor_kind: "human", reason: "reviewed" }],
    },
    "/api/v1/signals": page(days.flatMap((d, i) => ["trend_ma_200", "mean_rev_rsi"].map((s, j) => ({ strategy_id: s, timestamp: `${d}T19:35:00+00:00`, data_timestamp: `${d}T19:30:00+00:00`, direction: "long", normalized_score: (i - 1.5) / 2 * (j ? -1 : 1), confidence: 0.7, suggested_exposure: 1, reason: "test" })))),
    "/api/v1/risk": { banner, latest_decision: risk[0], history: risk, limits: { max_gross_exposure: 2 } },
    "/api/v1/backtests": {
      banner,
      backtests: [{ name: "20260920-trend", has_report: true, summary: { metrics: { all: { Strategy: { cagr: 0.12, sharpe: 0.9, max_drawdown: -0.3, volatility: 0.2, sessions: 2500 } } } } }],
      research: [{ name: "20260921-research", has_report: true, summary: { period: "2010-2026", trials_this_run: 40, trials_registered: 400, synthetic_sessions: 0 } }],
    },
    "/api/v1/orders": page([{ client_order_id: "aq-1", cycle_id: cycle.cycle_id, symbol: "TQQQ", side: "buy", quantity: "700", order_type: "market", state: "filled", risk_increasing: true, created_at: "2026-09-24T19:50:00+00:00", events: [{ from_state: "submitted", to_state: "filled", at: "2026-09-24T19:51:00+00:00" }] }]),
    "/api/v1/shadow-orders": page([]),
    "/api/v1/reconciliation": page([{ id: 1, cycle_id: cycle.cycle_id, passed: true, differences_json: {}, at: "2026-09-24T20:05:00+00:00" }]),
    "/api/v1/executions": page([{ client_order_id: "aq-1", symbol: "TQQQ", side: "buy", qty: "700", price: "72.93", expected_price: "72.90", slippage_bps: 4.1, fee: "0", at: "2026-09-24T19:51:00+00:00" }]),
    "/api/v1/cycles": page([cycle]),
    "/api/v1/system/health": { banner, version: "0.11.0", database: { configured: true, reachable: true, revision: "0002", head: "0002", at_head: true }, kill_switch: ks, latest_cycle: cycle, recent_errors: [], recent_notifications: [] },
    "/api/v1/cycles/cyc-2026-09-24/explain": { banner, cycle, signals: [], risk: risk[0] },
    ...controlFixtures(),
  };
}

export const JOB_ID = "b".repeat(32);
const worker = { worker_id: "w-1", online: true, last_seen_at: "2026-09-25T14:00:00+00:00", status: { master_gate: false, credentials_configured: { ALPACA_API_KEY_ID: true, ALPACA_API_SECRET_KEY: true } } };
export const JOB = {
  id: JOB_ID, job_type: "backtest.run", title: "Backtest", params: { strategies: ["baseline_buy_hold"], source: "file" },
  status: "succeeded", requested_by: "ann", requested_at: "2026-09-25T14:00:00+00:00", started_at: "2026-09-25T14:00:01+00:00",
  finished_at: "2026-09-25T14:00:09+00:00", heartbeat_at: null, progress: 1, message: "finished", result: null, error: null,
  config_version: "development-abc", retry_of: null, cancel_requested: false, retryable: true,
};
const curve = ["2016-09-26", "2020-01-02", "2023-01-03", "2026-09-25"].map((d, i) => ({ date: d, equity: [100000, 140000, 120000, 210000][i], drawdown: [0, 0, -0.14, 0][i], synthetic: false }));
const SUMMARY = {
  disclaimer: "HYPOTHETICAL backtest results.",
  strategies: ["baseline_buy_hold"], source: "file", period: { start: "2016-09-26", end: "2026-09-25" }, config_version: "development-abc",
  initial_capital: 100000, ending_equity: 210000,
  headline: { total_return: 1.1, cagr: 0.077, volatility: 0.2, sharpe: 0.5, sortino: 0.7, max_drawdown: -0.3, max_drawdown_duration_sessions: 400, calmar: 0.26, annual_turnover: 0.1, trades: 1 },
  fills: 1, round_trips: 0, metrics: { all: { Strategy: { total_return: 1.1, cagr: 0.077 }, QQQ: { total_return: 1.15, cagr: 0.08 } } },
  has_synthetic: false, unpriced_instruments: ["TQQQ", "SQQQ"], notes: [], curve,
};

function controlFixtures(): Record<string, unknown> {
  return {
    "/api/v1/jobs": { jobs: [JOB], worker },
    [`/api/v1/jobs/${JOB_ID}`]: { ...JOB, logs: [{ at: "2026-09-25T14:00:02+00:00", level: "info", message: "loading file price history" }] },
    "/api/v1/data/datasets": {
      datasets: [{ source: "file", symbol: "QQQ", frequency: "1d", adjustment: "all", rows: 2514, first: "2016-09-26", last: "2026-09-25", validation_passed: true, is_synthetic: false, fresh: true, freshness: "newest bar 2026-09-25", issues: [], warnings: ["no corporate-actions data for this symbol: total return may be understated"], updated_at: "2026-09-25T14:00:00+00:00" }],
      updated_at: "2026-09-25T14:00:00+00:00", primary_provider: "file", synthetic_warning: "SYNTHETIC data warning",
    },
    "/api/v1/data/options": { providers: [{ name: "file", label: "Uploaded / imported files", configured: true }, { name: "polygon", label: "Polygon", configured: false }], default_provider: "file", symbols: ["QQQ", "TQQQ", "SQQQ"] },
    "/api/v1/data/uploads": { uploads: [] },
    "/api/v1/backtests/options": {
      strategies: [{ strategy_id: "baseline_buy_hold", label: "buy and hold", long_only_1x: true }],
      sources: ["file"],
      defaults: { source: "file", initial_capital: 100000, execution: "near_close", execution_delay_bars: 0, use_synthetic_history: false, costs: { commission_per_share: 0, commission_per_order: 0, commission_minimum: 0, slippage_bps: 1, impact_coefficient_bps: 5, max_participation: 0.01 } },
    },
    "/api/v1/backtests/runs": { runs: [{ job_id: JOB_ID, strategies: ["baseline_buy_hold"], summary: { headline: SUMMARY.headline } }], jobs: [JOB] },
    [`/api/v1/backtests/runs/${JOB_ID}`]: { run: { job_id: JOB_ID, summary: SUMMARY }, job: JOB },
    "/api/v1/research/runs": { jobs: [], candidates: ["mom_time_series"], eligible: ["baseline_buy_hold", "mom_time_series"], defaults: { simulations: 1000, scheme: "rolling", use_synthetic_history: false }, gates: {}, promotion: "never automatic" },
    "/api/v1/strategies/manager": {
      strategies: [{ strategy_id: "baseline_buy_hold", family: "benchmark", implementation: "baseline_buy_hold", version_id: "v1", lifecycle: "research", enabled: true, eligible_for_paper_or_shadow: false, params: {}, param_schema: [], param_grid: {}, warmup_bars: 0, params_editable: true, eligible_modes: ["backtest"], evidence: {}, allowed_transitions: [{ to: "validated", promotion: true }, { to: "disabled", promotion: false }], reviewed: { lifecycle: "research" } }],
      lifecycle_events: [], promotion_confirm: "APPROVE PROMOTION", runtime_revision: 0,
    },
    "/api/v1/settings": {
      config_version: "development-abc", reviewed_config_version: "development-abc",
      runtime: { revision: 0, overlay: {} },
      settings: [
        { path: "backtest.initial_capital", category: "Backtesting", class: "runtime", label: "Starting capital (USD)", value: 100000, reviewed_value: 100000, overridden: false, tighter: null, choices: [], why: "" },
        { path: "risk.max_daily_loss", category: "Risk", class: "runtime_confirm", label: "Daily loss halt", value: 0.06, reviewed_value: 0.06, overridden: false, tighter: "lower", choices: [], why: "" },
        { path: "trading.live_trading.enabled", category: "Trading", class: "immutable", label: "enabled", value: false, reviewed_value: false, overridden: false, tighter: null, choices: [], why: "reviewed change only" },
      ],
      secrets: [{ name: "ALPACA_API_KEY_ID", category: "Broker", label: "Alpaca paper key id", configured: true }],
      classes: { runtime: "safe to change here" }, history: [], risk_confirm: "TIGHTEN RISK LIMITS", worker_note: "Applies to the next job.",
    },
    "/api/v1/trading/control": {
      environment: "production", mode: "shadow", mode_label: "SHADOW MODE — NO ORDERS SENT", uses_real_money: false, real_money_possible: false,
      live_trading: "LOCKED / NOT AVAILABLE",
      mode_explanations: { shadow: "Strategies, signals and orders are calculated and recorded, but no orders are sent to a broker.", paper: "Orders may be submitted only to the verified Alpaca paper-trading account and use simulated funds." },
      worker: { online: true, last_seen: "2026-09-25T14:00:00+00:00", worker_id: "w-1" },
      automation: { label: "STOPPED", state: "stopped", desired: "stopped", detail: "master gate off", restart_needed: false, master_gate: false, available: false, unavailable_reason: "Automation unavailable — deployment scheduler master gate is OFF." },
      scheduler: { master_gate: false, desired: "stopped", state: "stopped", detail: "master gate off", restart_needed: false },
      kill_switch: { engaged: true, label: "ENGAGED", known: true },
      broker: { provider: "alpaca", status: "ALPACA PAPER (not verified)", identity: "Alpaca PAPER (simulated funds)", endpoint_host: "paper-api.alpaca.markets", paper_endpoint: true, credentials_configured: true, verification: null },
      eligible_strategies: [], eligible_count: 0, data_freshness: [], reconciliation: null, latest_preflight: null,
      start_readiness: {
        ready: false,
        items: [
          { key: "master_gate", label: "Scheduler master gate (AQ_SCHEDULER_ENABLED)", ok: false, detail: "Automation unavailable — deployment scheduler master gate is OFF.", fix: "A deliberate deployment change on the worker service." },
          { key: "mode", label: "Trading mode: SHADOW", ok: true, detail: "SHADOW — no orders sent", fix: "" },
          { key: "strategies", label: "At least one strategy approved for SHADOW", ok: false, detail: "no strategy is eligible for shadow", fix: "Advance a strategy to paper in the Strategy Manager." },
        ],
      },
      paper_switch: { allowed: false, problems: ["the Alpaca paper account has not been verified yet"], checks: [], verified_at: null },
      confirmations: { start_shadow: "START SHADOW TRADING", start_paper: "START PAPER TRADING", paper_mode: "Switch to PAPER trading", shadow_mode: "Switch to SHADOW mode" },
    },
    "/api/v1/trading/live-readiness": { live_possible: false, summary: "Live trading is locked.", items: [{ requirement: "A live broker adapter", satisfied: false, status: "none", how: "separate review" }] },
  };
}

export interface Recorded {
  requests: Request[];
  posts: { path: string; body: unknown }[];
}

/**
 * Intercept every call to the API origin. Unknown paths return 404; a request
 * without the right bearer token returns 401 (like the real API).
 */
export async function mockApi(
  page: Page,
  opts: { data?: Record<string, unknown>; override?: (path: string) => { status: number; body: unknown } | null } = {},
): Promise<Recorded> {
  const data = opts.data ?? fixtures();
  const rec: Recorded = { requests: [], posts: [] };
  let ks = data["/api/v1/kill-switch"] as Record<string, unknown>;
  await page.route(`${API}/**`, async (route) => {
    const req = route.request();
    rec.requests.push(req);
    const url = new URL(req.url());
    const json = (status: number, body: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify(body),
      });
    if (req.method() === "OPTIONS") {
      return route.fulfill({ status: 200, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, POST" } });
    }
    if (req.headers().authorization !== `Bearer ${TOKEN}`) return json(401, { detail: "missing or invalid bearer token" });
    const o = opts.override?.(url.pathname);
    if (o) return json(o.status, o.body);
    if (req.method() === "POST") {
      const body = req.postDataJSON() as Record<string, string>;
      rec.posts.push({ path: url.pathname, body });
      if (url.pathname.endsWith("/engage") && body.confirm === "STOP AUTOMATED TRADING") {
        ks = { engaged: true, reason: body.reason, actor: `operator:${body.actor}`, changed_at: "2026-09-25T14:00:00+00:00", fail_safe: false };
        return json(200, ks);
      }
      if (url.pathname === "/api/v1/jobs/backtest") return json(200, { ok: true, message: "queued", job: { ...JOB, status: "queued" }, detail: {} });
      return json(400, { detail: "refused" });
    }
    if (url.pathname === "/api/v1/kill-switch") return json(200, ks);
    const body = data[url.pathname];
    return body === undefined ? json(404, { detail: "Not Found" }) : json(200, body);
  });
  return rec;
}

export async function signIn(page: Page, path = "/") {
  await page.goto(path);
  await page.getByLabel("Operator token").fill(TOKEN);
  await page.getByRole("button", { name: "Continue" }).click();
}
