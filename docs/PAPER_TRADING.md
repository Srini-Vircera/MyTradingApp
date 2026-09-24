# Paper-trading and shadow-mode architecture

## Trading cycle (one per session, M10)

Times are *minutes before the session close* (`config/base.yaml → schedule`), so
a 13:00 early close shifts everything automatically. Weekends and holidays
produce no cycle. The example offsets below give 15:40–16:15 ET on a normal day.

| Offset | Step | Refuses (fail closed) when |
|---|---|---|
| −20 | health check: DB, broker, clock, kill switch, calendar | anything unreachable |
| −18 | market data update + validation | stale / missing / invalid bars |
| −15 | indicators | insufficient history, NaN in required values |
| −13 | strategies → signals (persisted) | any enabled strategy fails |
| −12 | ensemble → proposal → **risk engine** (persisted) | risk calculation fails |
| −11 | target portfolio; broker account, positions, open orders fetched and confirmed | positions/equity unconfirmed; unknown or duplicate open orders |
| −10 | **pre-trade gate** → order planner → order manager | any gate failure (risk-reducing may proceed if only RISK_INCREASING-scoped checks fail) |
| −5 | fill monitoring; no *new* orders after this point | — |
| +15 | reconciliation, daily performance, end-of-day summary | discrepancies → halt + alert |

A cycle is keyed by `cyc-<env>-<session_date>`. If the process restarts
mid-cycle it resumes the same cycle, regenerates the same deterministic
`client_order_id`s, and discovers already-submitted orders instead of
duplicating them.

## Order planning (idempotent by construction)

```
required_delta(symbol) = target_qty
                       − broker_confirmed_position
                       − Σ remaining qty of open orders in SUBMITTED / ACKNOWLEDGED /
                         PARTIALLY_FILLED / UNKNOWN
```

Example from the requirements: target 1,500, confirmed 1,000, open BUY 500 →
delta 0 → no order. Any open order in `UNKNOWN` makes exposure uncertain ⇒
refusal (`UNCERTAIN_OPEN_ORDERS`) until investigated. Deltas below the
rebalance threshold are skipped. Sells are sequenced before buys so buying
power is released first.

## Order manager

1. Build `OrderRequest` with deterministic `client_order_id`.
2. **Persist intent** (`CREATED`), validate (`VALIDATED`) — DB failure here ⇒ nothing is sent.
3. Transmit → `SUBMITTED`. On timeout/disconnect → `UNKNOWN` (never retried).
4. Investigate UNKNOWN by `get_order_by_client_id`: found → adopt broker state; not found after a grace period and the broker confirms no such order → mark `CANCELLED` (never re-send in the same cycle without allocating a new sequence from persisted state and passing the gate again).
5. Poll/stream updates → `ACKNOWLEDGED` / `PARTIALLY_FILLED` / `FILLED` / `CANCELLED` / `REJECTED`, each transition validated by the state machine and appended to `order_events`.
6. Record executions with expected price and slippage.

## Reconciliation

After every cycle: broker positions vs. expected positions (previous positions
+ confirmed fills), broker open orders vs. internal non-terminal orders,
broker cash/equity vs. expected within tolerance. Any discrepancy beyond
tolerance ⇒ `RECONCILIATION_FAILURE` persisted, alert sent, and the next cycle's
pre-trade gate refuses risk-increasing trading until a human acknowledges.

## Modes

| Mode | Reads broker | Submits | Purpose |
|---|---|---|---|
| `backtest` | no | simulated | research |
| `shadow` | yes (any account, read-only use) | **no** — writes `shadow_orders` | validate against a real account without controlling it |
| `paper` | yes (paper account) | yes, paper | end-to-end rehearsal |
| `live` | yes | yes, real money | only after the checklist in [SAFETY.md](SAFETY.md) |

Shadow mode runs the identical pipeline and planner; the only difference is
the final `submit_order` call is replaced by persistence of what *would* have
been sent. Paper and shadow can run side by side (different environments).

## Paper vs. backtest report (M13)

For each paper session: backtest the same config over the same day(s) and
compare signals (should match exactly), targets (exactly), fills vs. modelled
prices (slippage distribution), daily P&L difference, and errors. Persistent
divergence blocks promotion beyond PAPER.
