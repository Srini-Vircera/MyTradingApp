# Research robustness

Milestone 6. Code: `src/adaptive_quant/quant/research/` and `src/adaptive_quant/governance/research.py`.
Configuration: `config/base.yaml → research`.

> **Every number this produces is hypothetical.** Research evaluates backtests. It never
> adopts parameters, never edits `strategies.yaml`, and the only lifecycle change software
> can make is `research → validated` (and back). Paper, shadow and live still need a named
> human with a written justification.

## Running

```bash
aq research run                                  # every enabled, non-benchmark strategy
aq research run --strategy st_ema_cross --strategy it_ma_stack
aq research run --workers 4 --scheme anchored    # parallel trials; anchored walk-forward
aq research run --synthetic                      # labelled synthetic history (cannot validate)
aq research trials                               # trial registry summary (the DSR "N")
aq research status                               # latest evidence and automated lifecycle changes
```

**Output directory.** Each run writes to `var/research/<timestamp>-<ids>/`:

| File | Contents |
|---|---|
| `report.html` | Scorecard, multiple-testing tables, and per-strategy heatmaps, walk-forward, regimes and Monte Carlo |
| `research.json` | Every statistic, per strategy and across strategies |
| `scorecard.csv` | Ranked candidates with each gate's result |
| `trials.csv` | Every trial of the run, including invalid parameter combinations |
| `walkforward.csv` | Each fold's windows, chosen parameters, and IS / validation / OOS Sharpe |
| `montecarlo.csv` | Percentile tables, adverse direction, rounded |

**Persistent evidence** (append-only; never edit it):
- `research.registry_path`: every trial ever run;
- `research.governance_ledger`: research records and automated transitions.

**Cost.** One trial is one full M5 backtest, about 4 s for 15 years of daily data. Twenty
strategies × ~15 grid points, plus Monte Carlo re-runs, take tens of minutes single-threaded;
use `--workers`.

## Pipeline

For each candidate:

1. **Trials.**
   - One backtest per `param_grid` point: the Cartesian product, refused above `max_grid_points` rather than silently subsampled.
   - Invalid combinations (e.g. `fast >= slow`) are logged and not run.
   - Every trial uses the same period, costs and execution model, so their returns are comparable.
2. **Parameter robustness.**
   - The grid of full-period Sharpe ratios is scored for robustness.
   - The **plateau centre**, the point with the highest neighbourhood median, is the research choice. The raw best point never is.
3. **Walk-forward** selection and stitched out-of-sample returns.
4. **Statistics**:
   - Deflated Sharpe;
   - PBO over the strategy's grid;
   - bootstrap confidence intervals;
   - a p-value for Sharpe > 0 on the OOS returns;
   - regime consistency.
5. **Monte Carlo** path risk.

Across candidates:
- Benjamini–Hochberg FDR;
- White's Reality Check and Hansen's SPA of **every trial** against QQQ buy-and-hold;
- a global PBO over all trials;
- the scorecard and gates;
- governance records.

## Parameter robustness

Neighbours of a grid point differ by at most one grid step in every dimension. At the best point `x*`:

```
plateau    = median(neighbour Sharpe) / Sharpe(x*)        clipped to [0, 1]
dispersion = std(Sharpe of x* and neighbours) / |Sharpe(x*)|
negative   = share of neighbours with Sharpe <= 0
score      = plateau × (1 − negative) / (1 + dispersion)  ∈ [0, 1]
```

- **Flagged `potentially_overfit`:** a score below `overfit_threshold` (0.5), a non-positive best Sharpe, or a one-point grid (robustness unknown).
- **Tests.** The fixtures show that a sharp-peak surface is flagged and a plateau is not. Where an edge spike and a broad plateau compete, the plateau centre is chosen.

## Walk-forward

Windows are in sessions (years × 252):

| Scheme | Train | Validate | Test |
|---|---|---|---|
| `rolling` (default) | 8 y, sliding | 2 y | 1 y, step 1 y |
| `anchored` | from the first session | 2 y | 1 y |

**Selection for a fold** uses only rows before the end of its validation window. The slice is cut before the selector runs.
1. Rank grid points by neighbourhood-median train Sharpe (plateau-aware).
2. Keep the `top_k`.
3. Choose the best validation Sharpe among them.

**Stitching.** The test windows never overlap (`step ≥ test`). Their returns are stitched into one OOS series.

**Reported:**
- per-fold IS, validation and OOS Sharpe;
- the stitched OOS Sharpe;
- the OOS/IS decay ratio;
- parameter changes between folds.

**Charts keep IS and OOS apart:**
- "OUT-OF-SAMPLE: stitched walk-forward equity", with QQQ over the same dates;
- a separate "IN-SAMPLE (hindsight)" chart of the plateau-centre parameters.

**Test.** Replacing all data from fold *k*'s test window onward with extreme values leaves every choice up to fold *k* unchanged.

**Approximation.** Each trial is one continuous causal backtest. A parameter switch at a fold boundary is not simulated as a trade: the switch costs one rebalance, which is not charged.

## Multiple-testing controls

| Control | Definition | Tested against |
|---|---|---|
| Deflated Sharpe (Bailey & López de Prado 2014) | PSR against SR0 = E[max SR of N zero-skill trials]. N = distinct grid trials recorded **on the same data across all runs**; V = variance of this run's trial Sharpes; skew and raw kurtosis of the returns | Paper's example: SR0 ≈ 0.1132, DSR ≈ 0.9004 |
| PBO via CSCV (Bailey, Borwein, López de Prado & Zhu 2017) | S contiguous blocks (default 16, i.e. 12,870 combinations); PBO = share of combinations whose in-sample winner ranks at or below the OOS median (logit ≤ 0). Also: OOS-vs-IS slope and P(OOS loss) | Two-trial hand case (PBO = 1); noise ≈ 0.5; skill ≈ 0; fast path equals a brute-force loop |
| Stationary bootstrap (Politis & Romano 1994) | Geometric blocks (mean 20) with wrap-around; percentile CIs for OOS Sharpe, CAGR and max drawdown | Continuation rate 1 − 1/L; CI width ≈ analytic |
| White's Reality Check / Hansen SPA | Differential = trial return − QQQ return; max of √n·mean vs its recentred bootstrap. SPA is studentised, with consistent recentring (poor models are not recentred) | Detects a real edge; not fooled by the luckiest of 20 null models; SPA ≤ RC with many poor models |
| Benjamini–Hochberg | Step-up adjusted p-values for "OOS Sharpe ≤ 0" across candidates | BH 1995 example: 4 of 15 rejected at q = 0.05 |

## Monte Carlo

The plateau-centre parameters, full period. Percentiles are **adverse**: p90 is the value 90% of simulations beat. Returns and drawdowns are rounded to 0.5 pp, Sharpe to 0.05, and equity to 3 significant figures.

| Dimension | How |
|---|---|
| Trade sequence | Round trips (P&L / equity at entry) resampled with replacement |
| Return blocks | Stationary block bootstrap of daily returns |
| Start date | Random later start, up to 33% of the sample, with ≥ 3 years left |
| Parameters | Every grid point in the plateau neighbourhood |
| Costs | Engine re-runs with spread, slippage, impact and commission × U(0.5, 3) (top-ranked candidates) |
| Signal delay | Engine re-runs with 0/1/2 extra sessions of delay (top-ranked candidates) |

## Scorecard and gates

**Score.** Each criterion is converted to a percentile rank across the candidates (a single candidate gets 0.5; undefined values rank last). The weighted mean is the score, with weights set in `research.scorecard_weights`:
- OOS Sharpe, Sortino, max drawdown and Calmar;
- robustness;
- regime/decade consistency;
- diversification (1 − mean |corr| of OOS returns with the other candidates);
- turnover (lower is better);
- execution feasibility (share of orders not cancelled or trimmed).

**CAGR cannot be configured as a criterion.**

**Gates.** All must pass for `validated`. An undefined value fails its gate.

| Gate | Default |
|---|---|
| Deflated Sharpe ≥ | 0.95 |
| PBO over the strategy's grid ≤ | 0.30 |
| Robustness ≥ threshold and not `potentially_overfit` | 0.5 |
| Stitched OOS Sharpe > | 0 |
| BH-adjusted p ≤ | 0.10 |
| OOS sessions ≥ | 504 |
| OOS Sharpe − QQQ OOS Sharpe ≥ | 0 |
| No synthetic sessions in the OOS period | on |

## Governance

- **Research records.** Each research run attaches a `ResearchRecord` to the strategy version (`version_id` = implementation, code version and parameter hash of the plateau centre). The record holds the gates, score, rank, key statistics and the report path, and is appended to the governance ledger.
- **Automated transitions.** `proposed_transition` can return only:
  - `research → validated`, when every gate passed;
  - `validated → research`, when a later run fails a gate.

  `lifecycle.transition` independently forbids any other automated promotion.
- **Human control.**
  - `strategies.yaml` is never written by software.
  - The lifecycle declared there remains the human-controlled switch for paper, shadow and live eligibility.
  - `validated` enables nothing by itself.

## Known limitations

- The robustness metric is the full-period Sharpe. Walk-forward selection uses train and validation Sharpe only.
- Fold-boundary parameter switches are not charged as trades (see above).
- The Deflated Sharpe N counts distinct grid trials on identical data. Trials on different data vintages or periods are counted separately.
- Regime buckets need ≥ 60 sessions to count towards consistency. Decades are calendar decades.
- Engine-based Monte Carlo (costs, delay) runs only for the `top_n` candidates, to bound runtime.
- All statistics inherit the M5 limitations: daily bars, modelled costs, the interim allocator (the M7 risk engine is not applied), and total-return prices.
- **Results on generated or synthetic data say nothing about real markets.** No real market data has been researched in the build environment.
