# Paper-trading and shadow-mode architecture

> **Milestone 9 implemented** the broker adapters, order planner, order manager and
> reconciliation (`src/adaptive_quant/trading/`). See [Implementation notes](#implementation-notes-m9).
> The scheduled trading cycle that wires them together is Milestone 10.

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

## Implementation notes (M9)

| Component | Module | Behaviour |
|---|---|---|
| Alpaca paper adapter | `brokers/alpaca.py` | Refuses any endpoint other than `https://paper-api.alpaca.markets`. Credentials are sent in headers only. Reads are retried (transport, 429, 5xx). **Order submission is never retried**: a timeout or 5xx raises `BrokerError` (the order becomes UNKNOWN). HTTP 422 for a duplicate client ID raises `DuplicateClientOrderId`; 403 or other 422s raise `OrderRejectedByBroker`. On-close orders are sent with `time_in_force: cls` |
| Simulated broker | `brokers/simulated.py` | A cash account (no margin, no shorting) with idempotent client IDs, partial fills, slippage and buying-power checks. Fault injection: `timeout_after_accept`, `timeout_before_accept`, `reject`, unavailable, manual (unexpected) positions, price changes |
| Broker factory | `brokers/factory.py` | Builds the Alpaca paper adapter from config and secrets; refuses live mode |
| Order planner | `orders/planner.py` | Idempotent deltas (see above). Refuses the whole plan on: an UNKNOWN order, a foreign or unseen open order, duplicate IDs, a blocked account, a missing price, or an unknown instrument. Rounds toward zero; skips moves inside the rebalance band; applies `max_order_notional`; limits buys to `(1 − buying_power_buffer) ×` buying power; skips risk-increasing orders when the price moved more than `max_price_deviation` since the risk decision; sequences sells first. Order IDs come from sequences allocated from persisted state |
| Order manager | `orders/manager.py` | Persist CREATED → VALIDATED → **SUBMITTED before transmission** → submit; adopt the broker state and record fills (with slippage vs expected). Timeout → UNKNOWN + one lookup, never a resend. `sync()` adopts broker state. `recover()` resolves every non-terminal intent after a restart. Shadow mode records `shadow_orders` only. Live mode is refused |
| Reconciliation | `reconciliation/reconciler.py` | Checks broker positions against start positions plus fills, open orders both ways, and cash. A failure is persisted and blocks **risk-increasing** trading (`ReconciliationCheck`) until a named person acknowledges it with a written reason (≥ 20 characters) |
| Pre-trade checks | `safety/broker_checks.py` | `BrokerStateCheck`: account, positions and open orders readable; paper account; not blocked; positive equity. `ReconciliationCheck` |

**Risk-increasing flag.** A buy is always risk-increasing. A sell is risk-reducing only if it moves net underlying exposure towards zero; for example, selling SQQQ while net long is risk-increasing.

**Static test.** Only `orders/manager.py` calls `submit_order`.

**Scenario tests** (`tests/trading/`, against real PostgreSQL):
- API timeout, both after and before the broker accepted the order;
- duplicate submission, both a replayed plan and a broker-side duplicate;
- partial fill;
- rejection;
- insufficient buying power;
- restart mid-trade;
- an existing unknown order;
- an unexpected position (reconciliation halt and acknowledgement);
- price spike and large gap;
- the 1,500 / 1,000 / 500 example (no order);
- shadow mode never submits;
- database outage: nothing is transmitted.
