# Operator API

Milestone 11. Code: `src/adaptive_quant/api/`; schema: [`apps/api/openapi.json`](../apps/api/openapi.json).

The API gives read-only views of the platform plus the kill switch. **It cannot change the trading mode, edit configuration, place or cancel orders, or promote strategies.** Tests enforce this.

## Running

```bash
# .env: AQ_API_TOKEN=<at least 32 random characters>, DATABASE_URL=...
python -c 'import secrets; print(secrets.token_urlsafe(48))'   # generate a token
aq --env paper api serve        # http://127.0.0.1:8000/api/v1 (api.host / api.port)
aq api openapi > apps/api/openapi.json   # regenerate the committed schema
```

**Defaults:**
- The server binds to **localhost**. Expose it only behind TLS (M13).
- Without `DATABASE_URL` the database-backed pages return 503; configuration, strategies, system health and the kill switch still work.

## Security

| Control | Behaviour |
|---|---|
| Authentication | One operator bearer token (`Authorization: Bearer <AQ_API_TOKEN>`), compared in constant time. The server refuses to start with a missing or short token (`api.min_bearer_chars`, default 32). Failed attempts are logged without the supplied value. Only `GET /api/v1/health/live` is public |
| Allowed mutations | Exactly two: `POST /kill-switch/engage` and `POST /kill-switch/release`. A test lists every route (including those in the OpenAPI schema) and fails if any other non-GET route exists |
| Kill-switch confirmation | `engage` needs `confirm: "STOP AUTOMATED TRADING"`; `release` needs `confirm: "RE-ENABLE TRADING"`. Both need a named `actor` and a `reason`. Unknown fields are rejected. Actions are recorded in the kill-switch audit file and `kill_switch_events` (as `api:<actor>`) |
| Trading mode | Read-only. `/configuration` reports `changeable_via_api: false`, and the only way to change the mode is editing YAML and restarting (see [SAFETY.md](SAFETY.md)) |
| Import boundaries | The API package cannot import brokers, the order planner or manager, the trading cycle or the scheduler (static test) |
| Responses | `Cache-Control: no-store`, `nosniff`, `X-Frame-Options: DENY`, a restrictive CSP and `Referrer-Policy: no-referrer`. The configuration is redacted. Database problems return 503 without internals |
| CORS | Explicit origins only (`api.cors_origins`, default `http://localhost:3000`); `*` is refused by config validation |
| Docs UI | Disabled (no third-party assets); the schema is at `/api/v1/openapi.json` |
| Page size | Every list takes `limit` (1 .. `api.max_page_size`) |

## Endpoints (`/api/v1`)

| Page | Endpoint | Contents |
|---|---|---|
| Overview | `GET /overview` | Mode banner, kill switch, today's session and schedule (early close), latest cycle and its steps, latest performance, target weights, open-order count, last reconciliation |
| Portfolio | `GET /portfolio` | Latest account snapshot, broker and expected positions, target portfolio |
| Strategies | `GET /strategies` | Configured strategies with lifecycle, version, parameters, eligible modes and approval; lifecycle events |
| Signals | `GET /signals?cycle_id=` | Stored strategy signals |
| Risk | `GET /risk` | Latest risk decision (band, volatility scale, every adjustment), history, configured limits |
| Performance | `GET /performance` | Daily performance series |
| Backtests | `GET /backtests` | Stored backtest (`metrics.json`) and research (`research.json`) summaries |
| Orders | `GET /orders?state=&cycle_id=`, `GET /shadow-orders` | Order intents with their state history; shadow orders |
| Executions | `GET /executions` | Fills with expected price and slippage |
| Reconciliation | `GET /reconciliation` | Reconciliation reports (including acknowledgements) |
| Cycles | `GET /cycles`, `GET /cycles/{id}/explain` | Trading cycles and the full decision chain of one |
| System health | `GET /system/health` | Version, database reachability and schema revision, kill switch, latest cycle, recent errors and notifications |
| Configuration | `GET /configuration` | Redacted resolved settings, config version, warnings, live-trading lock (read-only) |
| Kill switch | `GET /kill-switch`, `POST /kill-switch/engage`, `POST /kill-switch/release` | Status, and the two confirmed actions |

Every page response includes a `banner` with environment, mode, `uses_real_money`, `live_trading_enabled`, config version, and a notice that simulated results are hypothetical.
