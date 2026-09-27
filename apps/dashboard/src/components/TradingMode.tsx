"use client";

/**
 * Trading Mode (SHADOW / PAPER) selector and readiness checklists.
 *
 * Mode is not automation: choosing PAPER never starts the scheduler, releases the
 * kill switch or promotes a strategy. Live trading is not an option here.
 */
import Link from "next/link";
import { useState } from "react";
import { mutate, type Row } from "@/lib/api";
import { when } from "@/lib/format";
import { useMutation } from "@/lib/session";
import { ConfirmDialog } from "./ConfirmDialog";
import { ActionResult, OperatorField, useOperator } from "./control";
import { StatusBadge } from "./ui";

export interface CheckItem {
  key?: string;
  name?: string;
  label: string;
  ok?: boolean;
  passed?: boolean;
  detail?: string;
  fix?: string;
}

/** ✓ / ✗ list; every failed item says what is wrong and what to do. */
export function Checklist({ items, label }: { items: CheckItem[]; label: string }) {
  if (items.length === 0) return <p className="muted">No checks recorded yet.</p>;
  return (
    <ul className="checklist" aria-label={label}>
      {items.map((i, n) => {
        const ok = Boolean(i.ok ?? i.passed);
        return (
          <li key={i.key ?? i.name ?? n} data-ok={ok}>
            <span aria-hidden="true" className={ok ? "tick" : "cross"}>
              {ok ? "✓" : "✗"}
            </span>
            <div>
              <span className="sr-only">{ok ? "passed: " : "not met: "}</span>
              <strong>{i.label}</strong>
              {i.detail ? <span className="muted"> — {i.detail}</span> : null}
              {!ok && i.fix ? <div className="small">→ {i.fix}</div> : null}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

export function ModeBadge({ mode }: { mode: string }) {
  return mode === "paper" ? (
    <StatusBadge status="warning" label="PAPER — SIMULATED FUNDS" />
  ) : (
    <StatusBadge status="neutral" label="SHADOW — NO ORDERS SENT" />
  );
}

const DEFAULT_TEXT: Record<string, string> = {
  shadow: "Strategies, signals and orders are calculated and recorded, but no orders are sent to a broker.",
  paper: "Orders may be submitted only to the verified Alpaca paper-trading account and use simulated funds.",
};

/**
 * The Trading Mode control. ``c`` is the /trading/control view (refreshed by the caller).
 */
export function TradingModePanel({ c, onChanged }: { c: Row; onChanged: () => void }) {
  const mode = String(c.mode ?? "shadow");
  const explain = { ...DEFAULT_TEXT, ...((c.mode_explanations ?? {}) as Record<string, string>) };
  const sw = (c.paper_switch ?? {}) as Row;
  const confirmations = (c.confirmations ?? {}) as Record<string, string>;
  const verification = ((c.broker as Row | undefined)?.verification ?? null) as Row | null;
  const [selected, setSelected] = useState<string>(mode);
  const [dialog, setDialog] = useState<"paper" | "shadow" | null>(null);
  const [actor, setActor] = useOperator();
  const m = useMutation();
  const choosing = selected !== mode;
  const automationRunning = (c.automation as Row | undefined)?.desired === "running";

  async function verify() {
    const out = await m.run((t) => mutate(t, "/api/v1/jobs/broker/verify", { actor: actor.trim(), params: {} }));
    if (out) onChanged();
  }

  return (
    <div className="mode-panel">
      <p>
        Active trading mode: <ModeBadge mode={mode} />
      </p>
      <fieldset className="plain mode-options">
        <legend>Trading Mode</legend>
        {["shadow", "paper"].map((v) => (
          <label key={v} className={`mode-option ${selected === v ? "selected" : ""}`}>
            <input type="radio" name="trading-mode" value={v} checked={selected === v} onChange={() => setSelected(v)} />
            <span>
              <strong>{v === "shadow" ? "Shadow" : "Paper"}</strong>
              {v === mode ? <span className="tag real"> ACTIVE</span> : null}
              <span className="muted small"> — {explain[v]}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <p className="small">
        <strong>Live Trading: LOCKED / NOT AVAILABLE.</strong> Live trading is not part of this selector; see{" "}
        <Link href="/live-readiness/">Live Readiness</Link>.
      </p>
      <p className="muted small">
        Changing the mode never starts automation, releases the kill switch or approves a strategy.
      </p>

      {choosing && selected === "paper" && (
        <div className="card simulated">
          <h3>PAPER TRADING — SIMULATED FUNDS</h3>
          <p className="small">
            Before switching, the worker verifies the Alpaca paper account (read-only; credentials
            never leave the server): broker is Alpaca, the endpoint is the paper environment,
            credentials exist and authenticate, the account is a paper account, the broker and
            database are reachable and the kill-switch state is known.
          </p>
          <Checklist label="Paper account verification" items={((sw.checks as CheckItem[] | undefined) ?? [])} />
          <p className="muted small">Last verification: {when(sw.verified_at ?? verification?.verified_at)}</p>
          {!sw.allowed && (
            <div className="alert warning" role="status">
              <strong>Cannot switch to PAPER yet:</strong>
              <ul className="compact">
                {((sw.problems as string[] | undefined) ?? []).map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="form-grid">
            <OperatorField value={actor} onChange={setActor} />
          </div>
          <div className="form-actions">
            <button type="button" className="btn" disabled={actor.trim().length < 2 || m.busy} onClick={() => void verify()}>
              Verify Alpaca paper account (read-only)
            </button>
            <button type="button" className="btn primary" disabled={!sw.allowed} onClick={() => setDialog("paper")}>
              Switch to PAPER trading…
            </button>
            <button type="button" className="btn ghost" onClick={() => setSelected(mode)}>
              Cancel
            </button>
          </div>
          <ActionResult error={m.error} message={m.message} />
        </div>
      )}

      {choosing && selected === "shadow" && (
        <div className="card">
          <h3>SHADOW MODE — NO ORDERS SENT</h3>
          <p className="small">
            {automationRunning
              ? "Automation is running: it is stopped first, and from that moment no paper order can be transmitted."
              : "No orders will be sent to the broker."}{" "}
            Switching back to shadow never needs the broker.
          </p>
          <div className="form-actions">
            <button type="button" className="btn primary" onClick={() => setDialog("shadow")}>
              Switch to SHADOW mode…
            </button>
            <button type="button" className="btn ghost" onClick={() => setSelected(mode)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {dialog && (
        <ConfirmDialog
          title={dialog === "paper" ? "Switch to PAPER trading" : "Switch to SHADOW mode"}
          description={
            dialog === "paper"
              ? "Selects PAPER mode. Automation stays STOPPED, the kill switch is unchanged and no strategy is approved; start paper trading separately on Trading Control."
              : "Selects SHADOW mode. If automation is running it is stopped first."
          }
          phrase={dialog === "paper" ? confirmations.paper_mode ?? "" : confirmations.shadow_mode ?? ""}
          submitLabel={dialog === "paper" ? "Switch to PAPER" : "Switch to SHADOW"}
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setDialog(null)}
          onSubmit={async (v) => {
            const out = await m.run((t) => mutate(t, "/api/v1/trading/mode", { ...v, target: dialog }));
            if (out) {
              setDialog(null);
              setSelected(dialog);
              onChanged();
            }
          }}
        >
          {dialog === "paper" && (
            <p className="alert warning">
              <strong>PAPER TRADING — SIMULATED FUNDS.</strong> Orders can only go to the verified Alpaca paper
              account. No real money is involved.
            </p>
          )}
        </ConfirmDialog>
      )}
    </div>
  );
}
