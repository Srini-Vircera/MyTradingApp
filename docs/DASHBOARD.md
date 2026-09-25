# Operator dashboard (Milestone 12)

A Next.js + TypeScript app in `apps/dashboard`. It is a **static export**:
plain HTML/JS served by any web server, running entirely in the browser and
talking only to the operator API (`docs/API.md`). There is no dashboard
server, so nothing holds the operator token except the operator's browser tab,
and nothing in the dashboard can reach a broker.

## What it can and cannot do

| Can | Cannot |
|---|---|
| Show every read endpoint of the API | Change the trading mode (paper/shadow/live) |
| Engage the kill switch (STOP AUTOMATED TRADING) | Edit configuration |
| Release the kill switch (typed `RE-ENABLE TRADING`) | Place, change or cancel orders |
| | Promote or approve strategies |

Its only writes are the API's only writes: the two kill-switch actions. Both
need a named operator, a reason and the exact confirmation phrase; the button
stays disabled until the phrase matches exactly, and the API checks it again.
If the API is unreachable, use `aq kill-switch engage` on the server.

## Pages

| Page | Content |
|---|---|
| Overview | kill switch, equity/return/drawdown, open orders, last reconciliation, latest cycle and its steps, today's schedule (early closes), target weights, equity chart |
| Portfolio | account (paper vs real-money badge), broker vs expected positions with a match check, target weights |
| Strategies | catalogue with lifecycle and eligible modes, approvals, lifecycle history |
| Signals | heatmap of normalised scores (strategy × cycle), latest signals |
| Risk | band, drawdown, vol scale, regime, block state, flags; allocation over time; band history; drawdown; adjustments; limits |
| Performance | equity, drawdown, daily returns, daily table |
| Backtests | stored backtest summaries (CAGR, Sharpe, max drawdown…) and research runs |
| Orders | order intents with state and broker events (UNKNOWN highlighted), shadow orders, reconciliation |
| Executions | fills with expected price and slippage |
| System Health | version, database/migrations, kill switch (with release flow), recent cycles, errors, notifications |
| Configuration | trading-mode lock, warnings, redacted resolved settings (read-only) |
| Explain | the stored decision chain of one cycle (linked from Overview and System Health) |

Every page shows the **mode banner** (amber PAPER, blue SHADOW, red
LIVE — REAL MONEY; a missing or unrecognised mode is shown in red as
unknown), the kill-switch state and the STOP button. It also carries the
notice that paper, simulated and backtest results are hypothetical.

Charts are dependency-free SVG. Each has a hover tooltip and a data-table
view, and statuses use an icon plus a label as well as colour.
Light and dark themes follow the OS setting.

## Security

- **Token:** the operator enters `AQ_API_TOKEN` at runtime.
  - It is kept in `sessionStorage` for that tab only; it is never built into the bundle, written to `localStorage` or cookies, put in a URL, or logged.
  - A 401 clears it.
- **Requests:** API calls send `Authorization: Bearer …` with `cache: no-store`, no credentials and no referrer.
- **Content Security Policy (production build):** scripts and styles come from the dashboard's own origin; network requests go only to the configured API origin. Web-server headers (frame-ancestors, HSTS) arrive with TLS deployment in M13.
- **API origin:** set at build time with `NEXT_PUBLIC_AQ_API_ORIGIN` (default `http://localhost:8000`). It is a URL, not a secret. The dashboard's origin must be listed in the API's `api.cors_origins`, which is `http://localhost:3000` by default.

## Develop and test

```bash
cd apps/dashboard
npm ci
npm run dev                  # http://localhost:3000 (API: aq --env paper api serve)
npm run build && npm run serve   # production static export on :3000
npm run gen:api              # regenerate src/lib/api-types.ts after an API change
npm run check:api            # fail if the types are stale
npm run typecheck && npm test && npm run test:e2e
```

- **Unit tests (Vitest + Testing Library):**
  - API client: bearer header, the token never in the URL, 401/503 handling.
  - Kill-switch dialogs: the exact phrase, cancel, refusal.
  - Mode banner, token storage, chart edge cases, data transforms.
  - A static safety suite: network calls only from `src/lib/api.ts`; POST only for the kill switch; every API path exists in the schema; no persistent token storage; no mode, order or promotion controls.
- **End-to-end tests (Playwright):** run against the built export with a fully mocked API:
  - every page, with the PAPER banner and only GET requests;
  - the charts;
  - the STOP flow;
  - the LIVE banner;
  - a database outage;
  - a rejected token.
- **CI:** the `dashboard` job runs all of the above.
