"use client";

/**
 * Per-run strategy parameters for the Backtests form, generated from each strategy's
 * parameter schema (types, bounds, choices). Values apply to that backtest only; the
 * configured/approved strategy is unchanged. The API and the strategy registry
 * validate everything again.
 */
import type { Row } from "@/lib/api";
import { text } from "@/lib/format";

export type ParamValues = Record<string, string>;
type Scalar = number | string | boolean;

const HIDDEN = new Set(["signal_symbol"]);

function schemaOf(s: Row): Row[] {
  return ((s.param_schema as Row[] | undefined) ?? []).filter((p) => !HIDDEN.has(String(p.name)));
}

function parse(p: Row, raw: string): { ok: boolean; value?: Scalar; error?: string } {
  const name = String(p.name);
  if (p.type === "bool") return { ok: raw === "true" || raw === "false", value: raw === "true" };
  if (p.type === "int" || p.type === "float") {
    const n = Number(raw);
    if (raw.trim() === "" || !Number.isFinite(n)) return { ok: false, error: `${name} must be a number` };
    if (p.type === "int" && !Number.isInteger(n)) return { ok: false, error: `${name} must be a whole number` };
    if (typeof p.minimum === "number" && n < p.minimum) return { ok: false, error: `${name} must be ≥ ${p.minimum}` };
    if (typeof p.maximum === "number" && n > p.maximum) return { ok: false, error: `${name} must be ≤ ${p.maximum}` };
    return { ok: true, value: n };
  }
  const choices = p.choices as unknown[] | null;
  if (choices && !choices.includes(raw)) return { ok: false, error: `${name} must be one of ${choices.join(", ")}` };
  return { ok: true, value: raw };
}

/** Changed values only (typed), plus every problem found. */
export function paramOverrides(s: Row, values: ParamValues): { overrides: Record<string, Scalar>; problems: string[] } {
  const current = (s.params ?? {}) as Row;
  const overrides: Record<string, Scalar> = {};
  const problems: string[] = [];
  const merged: Record<string, unknown> = { ...current };
  for (const p of schemaOf(s)) {
    const name = String(p.name);
    const raw = values[name];
    if (raw === undefined || raw === String(current[name])) continue;
    const out = parse(p, raw);
    if (!out.ok) {
      problems.push(`${s.strategy_id}: ${out.error ?? `invalid ${name}`}`);
      continue;
    }
    overrides[name] = out.value as Scalar;
    merged[name] = out.value;
  }
  if (s.implementation === "golden_death_cross") {
    const fast = Number(merged.fast_period);
    const slow = Number(merged.slow_period);
    if (!(slow > fast)) problems.push(`${s.strategy_id}: slow MA period must be greater than fast MA period`);
    if (merged.bearish_action === "sqqq" && !(Number(merged.max_short_exposure) > 0))
      problems.push(`${s.strategy_id}: bearish behaviour SQQQ needs an inverse exposure above 0 (requires TQQQ/SQQQ data)`);
    if (merged.bearish_action !== "sqqq" && Number(merged.max_short_exposure) > 0)
      problems.push(`${s.strategy_id}: inverse exposure is only used with bearish behaviour SQQQ`);
  }
  return { overrides, problems };
}

const LABELS: Record<string, string> = {
  fast_period: "Fast MA period",
  slow_period: "Slow MA period",
  ma_type: "MA type",
  bearish_action: "Bearish behaviour",
  reduced_exposure: "Reduced QQQ exposure (bearish, qqq_reduced)",
  max_long_exposure: "Bullish exposure (× QQQ; above 1 uses TQQQ)",
  max_short_exposure: "Inverse exposure (bearish, sqqq only)",
};

export function StrategyParamsEditor({
  strategy,
  values,
  onChange,
}: {
  strategy: Row;
  values: ParamValues;
  onChange: (v: ParamValues) => void;
}) {
  const sid = String(strategy.strategy_id);
  const current = (strategy.params ?? {}) as Row;
  const golden = strategy.implementation === "golden_death_cross";
  const get = (name: string) => values[name] ?? String(current[name] ?? "");
  const fast = get("fast_period");
  const slow = get("slow_period");
  const ma = get("ma_type");
  const classic = golden && fast === "50" && slow === "200" && ma === "SMA";
  return (
    <details className="params" open={golden}>
      <summary>
        Parameters: {text(strategy.title ?? sid)} <span className="muted small">({sid})</span>
      </summary>
      {golden && (
        <p className="small">
          50 SMA / 200 SMA = classic Golden Cross / Death Cross. Changing the values creates a
          moving-average crossover variant rather than the classic configuration.{" "}
          {classic ? <span className="tag real">CLASSIC 50/200 SMA</span> : <span className="tag synthetic">VARIANT</span>}
        </p>
      )}
      {strategy.summary ? <p className="muted small">{text(strategy.summary)}</p> : null}
      <div className="form-grid">
        {schemaOf(strategy).map((p) => {
          const name = String(p.name);
          const id = `${sid}-${name}`;
          const choices = p.choices as unknown[] | null;
          const set = (v: string) => onChange({ ...values, [name]: v });
          return (
            <div className="field" key={name}>
              <label htmlFor={id}>{LABELS[name] ?? name}</label>
              {choices || p.type === "bool" ? (
                <select id={id} value={get(name)} onChange={(e) => set(e.target.value)}>
                  {(choices ?? [true, false]).map((c) => (
                    <option key={String(c)} value={String(c)}>
                      {String(c)}
                    </option>
                  ))}
                </select>
              ) : (
                <input id={id} inputMode="decimal" value={get(name)} onChange={(e) => set(e.target.value)} />
              )}
              {p.description ? <div className="muted small">{text(p.description)}</div> : null}
            </div>
          );
        })}
      </div>
      <p className="muted small">These values apply to this backtest only; the configured strategy is not changed.</p>
    </details>
  );
}
