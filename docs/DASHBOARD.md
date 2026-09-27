# Operator dashboard (Milestone 12, control plane in PR #4)

A Next.js + TypeScript app in `apps/dashboard`. It is a **static export**:
plain HTML/JS served by any web server, running entirely in the browser and
talking only to the operator API (`docs/API.md`). There is no dashboard
server, so nothing holds the operator token except the operator's browser tab,
and nothing in the dashboa## What it can and cannot do

| Can (audited, through the API allowlist) | Cannot |
|---|---|
| Show every read endpoint of the API | Place, change or cancel an order directly |
| Engage / release the kill switch (typed phrases) | Enable live trading or select live mode |
| Queue worker jobs: data download/validate/synthetic/inventory, backtests, research, a read-only broker check | Grant `live_approved` (reviewed repository change only) |
| Upload a CSV, review the validation preview, import or discard it | Edit strategy code or formulas |
| Change strategy parameters (research/disabled only), enable/disable, lifecycle up to `shadow` | Change or see deployment secrets or Railway variables |
| Change runtime settings (risk limits tighten-only) | Turn on the scheduler master gate `AQ_SCHEDULER_ENABLED` |
| Start/stop the shadow or paper scheduler; switch shadow ↔ paper | Loosen a risk limit |

Every write goes through `mutate()` / `uploadCsv()` / `killSwitch()` in
`src/lib/api.ts`. `mutate()` accepts only the paths in `MUTATIONS`, which a test
compares with the API's allowlist in the OpenAPI schema. Routine actions need the
operator's name (recorded in the audit trail); dangerous ones also need a reason
and an exact typed phrase, with the button disabled until it matches (the API
checks it again): `START SHADOW TRADING`, `START PAPER TRADING`,
`ENABLE PAPER TRADING`, `USE SHADOW MODE`, `TIGHTEN RISK LIMITS`,
`APPROVE PROMOTION`, and the kill-switch phrases. If the API is unreachable, use
`aq kill-switch engage` on the server.

## Pages

| Page | Content |
|---|---|
| Overview | kill switch, equity/return/drawdown, open orders, last reconciliation, latest cycle and its steps, today's schedule (early closes), target weights, equity chart |
| Trading Control | environment, mode, **whether real money is possible (never, here)**, scheduler state and both switches, kill switch, broker identity/endpoint/verification, eligible strategies, data freshness, reconciliation, pre-flight; start/stop, shadow ↔ paper; paper mode is framed as **SIMULATED FUNDS** |
| Portfolio | account (paper vs real-money badge), broker vs expected positions with a match check, target weights |
| Signals | heatmap of normalised scores (strategy × cycle), latest signals |
| Risk | band, drawdown, vol scale, regime, block state, flags; allocation over time; band history; drawdown; adjustments; limits |
| Performance | equity, drawdown, daily returns, daily table |
| Orders / Executions | order intents with state and broker events, shadow orders, reconciliation; fills with slippage |
| Data | dataset list (symbol, source, frequency, adjustment, dates, rows, validation, freshness, warnings; REAL vs **SYNTHETIC** labels), CSV upload with preview, download/update, validate, refresh, synthetic history (with the calibration warning), data jobs |
| Backtests | new-backtest form (strategies from the catalogue, source, dates, capital, execution timing and delay, synthetic on/off, costs; defaults from the configuration), runs, result with a prominent **hypothetical** disclaimer, headline metrics, equity and drawdown charts, benchmark comparison, real/synthetic data label; report files from the CLI |
| Research | launch form, runs, ranked scorecards with every gate; never promotes |
| Strategies | Strategy Manager: catalogue, parameters (schema-generated form), grid, warm-up, enabled vs eligibility, lifecycle moves with justification, latest research/backtest evidence, lifecycle history |
| Jobs | Job Center: all jobs with status filter, progress, logs, cancel, run again (repeatable jobs only) |
| Settings | settings by category (Data, Backtesting, Research, Strategies, Risk, Trading, Broker, Notifications, System), each marked editable / tighten-only / needs redeploy / secret / locked; secrets only as "configured yes/no"; change history |
| System Health | version, database/migrations, kill switch (with release flow), recent cycles, errors, notifications |
| Configuration | trading-mode lock, warnings, the reviewed (YAML) settings, redacted |
| Live Readiness | information only: every live-trading prerequisite and why it is not met; no controls |
| Explain | the stored decision chain of one cycle (linked from Overview and System Health) |

w and System Health) |

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
- **Content Security Policy (production build):** scripts and styles come from the dashboard's own origin; network requests go only to the configured API origin. The container's Caddy server adds frame-ancestors, HSTS and the other headers.
- **API origin:** set at build time with `NEXT_PUBLIC_AQ_API_ORIGIN` (default `http://localhost:8000`). It is a URL, not a secret.
  - Directly: the dashboard's origin must be listed in the API's `api.cors_origins`, which is `http://localhost:3000` by default.
  - Container image: builds with `same-origin`, and its Caddy server proxies `/api/v1` to the private API. No CORS is involved and the policy allows `connect-src 'self'` only ([DEPLOYMENT_RAILWAY.md](DEPLOYMENT_RAILWAY.md)).

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
  - Control-plane client: allowlisted paths only, encoded path parameters, raw CSV uploads with a size cap.
  - Pages: the backtest form (validation, the queued request, the disclaimer), the data page's REAL/SYNTHETIC labels and warnings, the Strategy Manager offering no live approval, Live Readiness without controls.
  - A static safety suite: network calls only from `src/lib/api.ts`; writes equal to the API's allowlist (POST only); no live-mode or live-approval request anywhere; no secret names; every API path exists in the schema; no persistent token storage; no live-trading or order controls.
- **End-to-end tests (Playwright):** run against the built export with a fully mocked API:
  - every page, with the PAPER banner and only GET requests while browsing;
  - running a QQQ `baseline_buy_hold` backtest from the browser and seeing its result;
  - Trading Control (real money not possible, no live control, paper needs the phrase);
  - Live Readiness (information only);
  - the charts;
  - the STOP flow;
  - the LIVE banner;
  - a database outage;
  - a rejected token.
- **CI:** the `dashboard` job runs all of the above.
