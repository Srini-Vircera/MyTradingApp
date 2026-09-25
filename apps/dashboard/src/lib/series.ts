/** Pure transforms from API rows to chart series (unit-tested). */
import type { Row } from "./api";
import type { AllocationPoint, HeatCell, Point } from "@/components/charts";
import { num, weights } from "./format";

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** Daily performance rows (oldest first) -> equity, drawdown (<= 0) and daily return series. */
export function performanceSeries(rows: Row[]): {
  equity: Point[];
  drawdown: Point[];
  returns: Point[];
} {
  const equity: Point[] = [];
  const drawdown: Point[] = [];
  const returns: Point[] = [];
  for (const r of rows) {
    const x = str(r.session_date);
    const e = num(r.equity);
    const d = num(r.drawdown);
    const ret = num(r.daily_return);
    if (e !== null) equity.push({ x, y: e });
    // stored as a positive fraction below the peak; plotted downwards from zero
    if (d !== null) drawdown.push({ x, y: -Math.abs(d) });
    if (ret !== null) returns.push({ x, y: ret });
  }
  return { equity, drawdown, returns };
}

/** Risk decisions (newest first from the API) -> allocation over time, oldest first. */
export function allocationSeries(riskHistory: Row[]): AllocationPoint[] {
  return [...riskHistory]
    .reverse()
    .map((r) => ({ x: str(r.created_at), weights: weights(r.approved_weights_json) }));
}

export function bandSeries(riskHistory: Row[]): { x: string; band: string; blocked: boolean }[] {
  return [...riskHistory].reverse().map((r) => ({
    x: str(r.created_at),
    band: str(r.drawdown_band) || "unknown",
    blocked: r.blocked_risk_increasing === true,
  }));
}

export function riskDrawdownSeries(riskHistory: Row[]): Point[] {
  return [...riskHistory]
    .reverse()
    .flatMap((r) => {
      const d = num(r.drawdown);
      return d === null ? [] : [{ x: str(r.created_at), y: -Math.abs(d) }];
    });
}

/** Strategy signals -> heatmap cells (strategy x signal time, normalised score). */
export function signalCells(signals: Row[]): HeatCell[] {
  return signals.flatMap((s) => {
    const v = num(s.normalized_score);
    if (v === null) return [];
    return [
      {
        row: str(s.strategy_id),
        col: str(s.timestamp),
        value: v,
        detail: `${str(s.direction)}, confidence ${num(s.confidence)?.toFixed(2) ?? "—"}`,
      },
    ];
  });
}

export interface PositionCompare {
  symbol: string;
  broker: number | null;
  expected: number | null;
  market_value: number | null;
  matches: boolean;
}

/** Broker vs expected positions by symbol. */
export function comparePositions(broker: Row[], expected: Row[]): PositionCompare[] {
  const b = new Map(broker.map((r) => [str(r.symbol), r]));
  const e = new Map(expected.map((r) => [str(r.symbol), r]));
  return [...new Set([...b.keys(), ...e.keys()])].sort().map((symbol) => {
    const bq = num(b.get(symbol)?.quantity);
    const eq = num(e.get(symbol)?.quantity);
    return {
      symbol,
      broker: bq,
      expected: eq,
      market_value: num(b.get(symbol)?.market_value),
      matches: (bq ?? 0) === (eq ?? 0),
    };
  });
}
