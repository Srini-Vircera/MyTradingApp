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
