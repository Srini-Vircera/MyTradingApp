# Safety

## Trading modes and the live-trading lock

The platform ships in **shadow** (development) and **paper** (paper, production)
modes. Every shipped configuration is tested to never be live
(`tests/unit/test_config.py::TestShippedConfiguration`).

Live trading is refused at startup unless **all six** of these are true
(`config/loader.py::enforce_trading_mode_policy`):

1. The `production` environment is selected (`AQ_ENV=production` / `--env production`).
2. `trading.mode: live` in `config/production.yaml`.
3. `trading.live_trading.enabled: true`.
4. `trading.live_trading.acknowledgement` is exactly
   `I understand that live mode trades real money`.
5. Environment variable `AQ_LIVE_TRADING_CONFIRM=yes-trade-real-money` in the deployment.
6. A real (non-simulated) broker provider; the broker adapter itself must report `is_live`, and a live adapter paired with a non-live mode (or vice-versa) is refused.

No code path writes any of these values. If any is missing the process stops
with a list of *every* missing item.

### Before you even consider live trading (checklist)

- [ ] ≥ 3 months of paper trading with the exact configuration, no unexplained reconciliation failures.
- [ ] Paper-vs-backtest report shows signals/targets match and slippage within modelled bounds.
- [ ] Every enabled strategy is `LIVE_APPROVED` by a human with written justification.
- [ ] Full test suite green; `aq config validate --env production` passes.
- [ ] Kill switch tested from the dashboard and CLI.
- [ ] Alerts verified end-to-end (email received).
- [ ] Start with a small allocation; understand that historical results are hypothetical.

## Strategy governance

- **Live mode:** only strategies in lifecycle `live_approved` can produce signals there (`StrategyCatalog.eligible`).
- **Approval in config:** declaring `live_approved` requires an `approval` block with the approver, date and a written justification.
- **Paper and shadow modes:** these require lifecycle ≥ `paper`.
- **Shipped config:** it has no strategy eligible outside research.
- **Failures:** a strategy that fails, isn't warmed up, or produces an out-of-range output blocks trading (`signal_failure`).

## Kill switch

```bash
aq kill-switch status
aq kill-switch engage  --actor "your name" --reason "why"
aq kill-switch release --actor "your name" --reason "why" --confirm "RE-ENABLE TRADING"
```

* Engaged ⇒ no new risk-increasing orders; risk-reducing orders stay allowed
  (`trading.kill_switch.allow_risk_reducing_orders`).
* **Fresh installs start engaged.** A missing, corrupt or unreadable state file
  also counts as engaged (fail closed).
* **Where the state lives:** `trading.kill_switch.store: file` (one host) or
  `database` (the deployment: the API and the worker share the
  `kill_switch_state` row). With the database store an unreachable database
  also counts as engaged, and changes are audited in `kill_switch_events`.
* Automated components (actor `system:*`) can engage but never release.
* Every change is appended to `var/state/kill_switch_audit.jsonl` (file store) or
  `kill_switch_events` (database store); nothing is deleted.

## Deployment guard

Every container runs `aq deploy check` before it starts and refuses to run if:
- the mode is live, or live trading is enabled in the configuration;
- `AQ_LIVE_TRADING_CONFIRM` is set at all;
- the kill switch is not the shared database store;
- the broker endpoint is not Alpaca *paper*.

Only one scheduler can run at a time (a PostgreSQL advisory lock), so an
overlapping redeploy or an extra replica cannot trade twice.

## Refusal conditions (pre-trade gate)

The gate runs *every* check and reports all failures together. A check that
crashes counts as failed; no checks configured counts as failed.

| Reason | Blocks |
|---|---|
| market data stale / missing / invalid | all orders |
| broker unavailable | all orders |
| positions or equity unconfirmed | all orders |
| duplicate orders detected | all orders |
| uncertain open orders (incl. UNKNOWN) | all orders |
| signal generation failed | all orders |
| risk calculation failed | all orders |
| reconciliation failed | all orders |
| kill switch engaged | risk-increasing orders |
| drawdown emergency band / daily loss limit | risk-increasing orders |

## Credentials

Only via environment variables (`.env` locally; sealed service variables on
Railway, see [DEPLOYMENT_RAILWAY.md](DEPLOYMENT_RAILWAY.md)). Config files containing secret-looking keys are
rejected; logs redact secret-looking fields; `aq secrets` shows only whether
each credential is set.
