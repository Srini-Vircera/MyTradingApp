import { expect, test } from "@playwright/test";
import { BANNER, fixtures, mockApi, signIn, TOKEN } from "./mock-api";

const PAGES: [string, string][] = [
  ["/", "Overview"],
  ["/portfolio/", "Portfolio"],
  ["/strategies/", "Strategies"],
  ["/signals/", "Signals"],
  ["/risk/", "Risk"],
  ["/performance/", "Performance"],
  ["/backtests/", "Backtests"],
  ["/orders/", "Orders"],
  ["/executions/", "Executions"],
  ["/system/", "System Health"],
  ["/configuration/", "Configuration"],
  ["/explain/?cycle=cyc-2026-09-24", "Explain a decision"],
];

test.beforeEach(async ({ page }) => {
  const problems: string[] = [];
  page.on("pageerror", (e) => problems.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !/status of 40[134]|status of 503/.test(m.text())) problems.push(m.text());
  });
  (page as unknown as { problems: string[] }).problems = problems;
});

test.afterEach(async ({ page }) => {
  expect((page as unknown as { problems: string[] }).problems).toEqual([]);
});

test("asks for the token, then shows the PAPER banner on every page", async ({ page }) => {
  const rec = await mockApi(page);
  await page.goto("/");
  await expect(page.getByLabel("Operator token")).toHaveAttribute("type", "password");
  await signIn(page);
  for (const [path, title] of PAGES) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
    await expect(page.getByTestId("mode-banner")).toContainText("PAPER TRADING");
    await expect(page.getByRole("button", { name: "STOP AUTOMATED TRADING" })).toBeVisible();
    await expect(page.locator(".alert.critical")).toHaveCount(0);
  }
  // token only ever in the Authorization header; only GETs were made
  for (const r of rec.requests.filter((r) => r.method() !== "OPTIONS")) {
    expect(r.url()).not.toContain(TOKEN);
    expect(r.method()).toBe("GET");
    expect(r.headers().authorization).toBe(`Bearer ${TOKEN}`);
  }
  expect(await page.evaluate(() => window.localStorage.length)).toBe(0);
});

test("renders the charts", async ({ page }) => {
  await mockApi(page);
  await signIn(page, "/risk/");
  await expect(page.getByRole("img", { name: "Approved weights per decision" })).toBeVisible();
  await expect(page.getByRole("img", { name: "Band per decision" })).toBeVisible();
  await page.goto("/performance/");
  await expect(page.getByRole("img", { name: "Equity" })).toBeVisible();
  await expect(page.getByRole("img", { name: "Daily return" })).toBeVisible();
  await page.goto("/signals/");
  await expect(page.getByRole("img", { name: /Normalised score/ })).toBeVisible();
  await page.getByRole("button", { name: "Show table" }).click();
  await expect(page.getByRole("cell", { name: "mean_rev_rsi" }).first()).toBeVisible();
});

test("STOP AUTOMATED TRADING engages the kill switch after typed confirmation", async ({ page }) => {
  const rec = await mockApi(page);
  await signIn(page);
  await expect(page.getByText("Automated trading allowed").first()).toBeVisible();
  await page.getByRole("button", { name: "STOP AUTOMATED TRADING" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Your name (operator)").fill("ann");
  await dialog.getByLabel("Reason").fill("unexpected volatility");
  await dialog.getByLabel(/to confirm/).fill("stop automated trading");
  await expect(dialog.getByRole("button", { name: "Engage kill switch" })).toBeDisabled();
  await dialog.getByLabel(/to confirm/).fill("STOP AUTOMATED TRADING");
  await dialog.getByRole("button", { name: "Engage kill switch" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByText(/Trading stopped \(kill switch engaged/).first()).toBeVisible();
  expect(rec.posts).toEqual([
    { path: "/api/v1/kill-switch/engage", body: { actor: "ann", reason: "unexpected volatility", confirm: "STOP AUTOMATED TRADING" } },
  ]);
});

test("shows LIVE / REAL MONEY in red when the API reports live mode", async ({ page }) => {
  const live = { ...BANNER, environment: "production", mode: "live", uses_real_money: true, live_trading_enabled: true };
  await mockApi(page, { data: fixtures(live) });
  await signIn(page);
  const banner = page.getByTestId("mode-banner");
  await expect(banner).toContainText("LIVE TRADING — REAL MONEY");
  await expect(banner).toHaveClass(/mode-live/);
});

test("a database outage is reported without hiding the mode banner or the stop button", async ({ page }) => {
  await mockApi(page, {
    override: (p) => (p === "/api/v1/overview" ? { status: 503, body: { detail: "audit database unavailable", hint: "check DATABASE_URL" } } : null),
  });
  await signIn(page);
  await expect(page.locator(".alert.critical")).toContainText("audit database unavailable — check DATABASE_URL");
  await expect(page.getByTestId("mode-banner")).toContainText("PAPER TRADING");
  await expect(page.getByRole("button", { name: "STOP AUTOMATED TRADING" })).toBeVisible();
});

test("a rejected token returns to the sign-in screen", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await page.getByLabel("Operator token").fill("wrong-token");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("The API rejected the token. Enter it again.")).toBeVisible();
  expect(await page.evaluate(() => window.sessionStorage.length)).toBe(0);
});
