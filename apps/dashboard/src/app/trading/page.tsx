"use client";

import { useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { KillSwitchPanel } from "@/components/KillSwitch";
import { useShell } from "@/components/Shell";
import { Checklist, TradingModePanel, type CheckItem } from "@/components/TradingMode";
import { ActionResult } from "@/components/control";
import { DataTable, LoadState, PageHeader, Section, Stat } from "@/components/ui";
import { mutate, type Row } from "@/lib/api";
import { text, when } from "@/lib/format";
import { useApi, useMutation } from "@/lib/session";

export default function TradingPage() {
  const c = useApi("/api/v1/trading/control", undefined, 5_000);
  const { killSwitch, setKillSwitch } = useShell();
  const [dialog, setDialog] = useState<"start" | "stop" | null>(null);
  const m = useMutation();
  const d = c.data as Row | null;
  const automation = (d?.automation ?? {}) as Row;
  const broker = (d?.broker ?? {}) as Row;
  const ks = (d?.kill_switch ?? {}) as Row;
  const confirmations = (d?.confirmations ?? {}) as Record<string, string>;
  const mode = String(d?.mode ?? "unknown");
  const paper = mode === "paper";
  const Mode = paper ? "Paper" : "Shadow";
  const eligible = (d?.eligible_strategies as string[] | undefined) ?? [];
  const readiness = (d?.start_readiness ?? {}) as Row;
  const items = (readiness.items as CheckItem[] | undefined) ?? [];
  const ready = Boolean(readiness.ready);
  const running = automation.desired === "running" || automation.state === "running";
  const recon = d?.reconciliation as Row | null;
  const preflight = d?.latest_preflight as Row | null;

  return (
    <>
      <PageHeader title="Trading Control">
        Trading mode, automation, the kill switch and the deployment master gate are separate
        controls. Every cycle still runs the kill-switch, data-freshness, broker, reconciliation,
        risk and pre-flight checks; nothing here can bypass them.
      </PageHeader>
      <LoadState loading={c.loading && !d} error={c.error} />
      {d && (
        <>
          <section className={`card ${paper ? "simulated" : ""}`} aria-label="Money at risk">
            <h2>{text(d.mode_label)}</h2>
            <p>
              {paper
                ? "Orders can go only to the verified Alpaca PAPER account (simulated funds), and only while automation runs and the kill switch is released."
                : "Shadow mode records what it would do; it never sends an order to a broker."}{" "}
              <strong>Real money: {d.real_money_possible ? "possible" : "not possible in this deployment"}.</strong>{" "}
              Live Trading: {text(d.live_trading)}.
            </p>
          </section>
          <div className="status-row" aria-label="Trading status">
            <Stat label="Mode" value={mode.toUpperCase()} sub={paper ? "simulated funds" : "no orders sent"} />
            <Stat
              label="Automation"
              value={text(automation.label)}
              sub={`operator switch: ${text(automation.desired)}`}
            />
            <Stat
              label="Kill switch"
              value={killSwitch ? (killSwitch.engaged ? "ENGAGED" : "RELEASED") : text(ks.label)}
            />
            <Stat label="Broker" value={text(broker.status)} sub={text(broker.endpoint_host)} />
            <Stat
              label="Strategies"
              value={String(d.eligible_count ?? eligible.length)}
              sub={`eligible for ${mode.toUpperCase()}`}
            />
            <Stat
              label="Master gate"
              value={automation.master_gate ? "ON" : "OFF"}
              sub="AQ_SCHEDULER_ENABLED (deployment)"
            />
          </div>
          {!automation.available && (
            <p className="alert warning" role="status">
              <strong>{text(automation.unavailable_reason)}</strong> It is a deployment setting on the worker service
              (Railway); the dashboard cannot change it.
            </p>
          )}

          <Section title="Trading Mode">
            <TradingModePanel c={d} onChanged={c.reload} />
          </Section>

          <Section title={`Automation — ${Mode} trading`}>
            <p>
              {text(automation.detail)}
              {automation.next_wake ? ` · next step ${when(automation.next_wake)}` : ""}
            </p>
            {Boolean(automation.restart_needed) && (
              <p className="alert warning">The configuration changed since automation started: stop and start it to apply.</p>
            )}
            <h3>Readiness to start {Mode} trading</h3>
            <Checklist label={`Readiness to start ${mode} trading`} items={items} />
            {!ready && !running && (
              <p className="alert warning" role="status">
                Start is available once every item above is ✓. Each ✗ says what to do.
              </p>
            )}
            <div className="form-actions">
              <button type="button" className="btn primary" disabled={!ready || running} onClick={() => setDialog("start")}>
                Start {Mode} Trading…
              </button>
              <button type="button" className="btn critical" onClick={() => setDialog("stop")}>
                Stop {Mode} Trading
              </button>
            </div>
            <p className="muted small">
              Stop is always available: from the moment it is accepted, no new order is
              transmitted; the worker stops the scheduler within ~15 seconds.
            </p>
            <ActionResult error={m.error} message={m.message} />
          </Section>

          <div className="grid-2">
            <Section title="Broker">
              <dl className="kv">
                <dt>Status</dt>
                <dd>{text(broker.status)}</dd>
                <dt>Account</dt>
                <dd>{text(broker.identity)}</dd>
                <dt>Endpoint</dt>
                <dd>{text(broker.endpoint_host)}</dd>
                <dt>Credentials on the worker</dt>
                <dd>{broker.credentials_configured === null ? "unknown" : broker.credentials_configured ? "configured" : "not configured"}</dd>
                <dt>Last verification</dt>
                <dd>{when((broker.verification as Row | null)?.verified_at)}</dd>
              </dl>
            </Section>
            <Section title="Checks">
              <dl className="kv">
                <dt>Latest reconciliation</dt>
                <dd>{recon ? `${recon.passed ? "matched" : "DIFFERENCES FOUND"} (${when(recon.at)})` : "none yet"}</dd>
                <dt>Latest cycle pre-flight</dt>
                <dd>{preflight ? `${preflight.passed ? "passed" : "FAILED"} (${when(preflight.checked_at)})` : "none yet"}</dd>
              </dl>
              <DataTable
                caption="Required market data"
                rows={(d.data_freshness as Row[] | undefined) ?? []}
                empty="No market data stored yet."
                columns={[
                  { key: "symbol", label: "Symbol" },
                  { key: "source", label: "Source" },
                  { key: "last", label: "Last bar" },
                  { key: "fresh", label: "Fresh" },
                ]}
              />
            </Section>
          </div>
          <Section title="Kill switch">
            <KillSwitchPanel status={killSwitch} onChanged={setKillSwitch} />
          </Section>
        </>
      )}
      {dialog === "start" && (
        <ConfirmDialog
          title={`Start ${Mode} Trading`}
          description={
            paper
              ? "The worker runs the trading cycle against the Alpaca PAPER account (simulated funds) after re-checking the mode, the paper verification and the kill switch."
              : "The worker runs the trading cycle in shadow mode: decisions are recorded, no orders are sent."
          }
          phrase={paper ? confirmations.start_paper ?? "" : confirmations.start_shadow ?? ""}
          submitLabel={`Start ${Mode} Trading`}
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setDialog(null)}
          onSubmit={async (v) => {
            const out = await m.run((t) => mutate(t, "/api/v1/trading/scheduler/start", v));
            if (out) {
              setDialog(null);
              c.reload();
            }
          }}
        >
          {paper && (
            <p className="alert warning">
              <strong>PAPER TRADING — SIMULATED FUNDS.</strong>
            </p>
          )}
        </ConfirmDialog>
      )}
      {dialog === "stop" && (
        <ConfirmDialog
          title={`Stop ${Mode} Trading`}
          description="No new order is transmitted from the moment this is accepted; a step in progress finishes without sending more orders. To block new risk immediately, also engage the kill switch."
          phrase=""
          submitLabel={`Stop ${Mode} Trading`}
          tone="critical"
          busy={m.busy}
          error={m.error}
          onCancel={() => setDialog(null)}
          onSubmit={async (v) => {
            const out = await m.run((t) => mutate(t, "/api/v1/trading/scheduler/stop", { actor: v.actor, reason: v.reason }));
            if (out) {
              setDialog(null);
              c.reload();
            }
          }}
        />
      )}
    </>
  );
}
