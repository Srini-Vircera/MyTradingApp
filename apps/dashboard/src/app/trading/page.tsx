"use client";

import { useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { KillSwitchPanel } from "@/components/KillSwitch";
import { useShell } from "@/components/Shell";
import { ActionResult, OperatorField, useOperator } from "@/components/control";
import { DataTable, LoadState, PageHeader, Section, Stat, StatusBadge } from "@/components/ui";
import { mutate, type Row } from "@/lib/api";
import { text, when } from "@/lib/format";
import { useApi, useMutation } from "@/lib/session";

type Dialog = "start" | "stop" | "paper" | "shadow" | null;

export default function TradingPage() {
  const c = useApi("/api/v1/trading/control", undefined, 10_000);
  const { killSwitch, setKillSwitch } = useShell();
  const [dialog, setDialog] = useState<Dialog>(null);
  const [actor, setActor] = useOperator();
  const m = useMutation();
  const d = c.data as Row | null;
  const sched = (d?.scheduler ?? {}) as Row;
  const broker = (d?.broker ?? {}) as Row;
  const verification = (broker.verification ?? null) as Row | null;
  const confirmations = (d?.confirmations ?? {}) as Record<string, string>;
  const mode = String(d?.mode ?? "unknown");
  const paper = mode === "paper";
  const eligible = (d?.eligible_strategies as string[] | undefined) ?? [];
  const recon = d?.reconciliation as Row | null;
  const preflight = d?.latest_preflight as Row | null;

  async function verify() {
    const out = await m.run((t) => mutate(t, "/api/v1/jobs/broker/verify", { actor: actor.trim(), params: {} }));
    if (out) c.reload();
  }

  return (
    <>
      <PageHeader title="Trading Control">
        Start or stop the automated shadow/paper trading cycle. Every cycle still runs the kill
        switch, data-freshness, broker, reconciliation, risk and pre-flight checks; nothing here
        can bypass them.
      </PageHeader>
      <LoadState loading={c.loading && !d} error={c.error} />
      {d && (
        <>
          <section className={`card ${paper ? "simulated" : ""}`} aria-label="Money at risk">
            <h2>{paper ? "PAPER TRADING — SIMULATED FUNDS" : "SHADOW MODE — NO ORDERS ARE SENT"}</h2>
            <p>
              {paper
                ? "Orders go to the Alpaca PAPER account (simulated money) once the scheduler runs and the kill switch is released."
                : "Shadow mode records what it would do; it never sends an order to a broker."}{" "}
              <strong>Real money: {d.real_money_possible ? "possible" : "not possible in this deployment"}.</strong>
            </p>
            <p className="muted small">{text(d.real_money_note)}</p>
          </section>
          <div className="stats">
            <Stat label="Environment" value={text(d.environment)} />
            <Stat label="Mode" value={mode} />
            <Stat
              label="Scheduler"
              value={text(sched.state)}
              sub={`operator switch: ${text(sched.desired)}`}
            />
            <Stat
              label="Master gate (AQ_SCHEDULER_ENABLED)"
              value={sched.master_gate ? "ON" : "OFF"}
              sub="deployment setting, not changeable here"
            />
            <Stat label="Kill switch" value={killSwitch ? (killSwitch.engaged ? "ENGAGED" : "released") : "unknown"} />
            <Stat
              label="Worker"
              value={(d.worker as Row | null)?.online ? "online" : "offline"}
              sub={when((d.worker as Row | null)?.last_seen)}
            />
          </div>
          <Section title="Scheduler">
            <p>{text(sched.detail)}</p>
            {Boolean(sched.restart_needed) && (
              <p className="alert warning">The configuration changed since the scheduler started: stop and start it to apply.</p>
            )}
            {!sched.master_gate && (
              <p className="alert warning">
                The deployment master gate is OFF: the scheduler cannot run whatever is chosen here.
                Turning it on is a deliberate deployment change on the worker service.
              </p>
            )}
            <p className="muted small">
              Strategies that can trade in {mode} mode: {eligible.join(", ") || "none (advance one to paper in the Strategy Manager)"}
              {sched.next_wake ? ` · next step ${when(sched.next_wake)}` : ""}
            </p>
            <div className="form-actions">
              <button type="button" className="btn primary" disabled={sched.desired === "running"} onClick={() => setDialog("start")}>
                Start {mode} trading…
              </button>
              <button type="button" className="btn critical" onClick={() => setDialog("stop")}>
                Stop scheduler
              </button>
            </div>
          </Section>
          <div className="grid-2">
            <Section title="Broker">
              <dl className="kv">
                <dt>Account</dt>
                <dd>{text(broker.identity)}</dd>
                <dt>Endpoint</dt>
                <dd>
                  {text(broker.endpoint_host)} {broker.paper_endpoint ? <StatusBadge status="good" label="paper" /> : <StatusBadge status="critical" label="not paper" />}
                </dd>
                <dt>Credentials on the worker</dt>
                <dd>{broker.credentials_configured ? "configured" : "not configured"}</dd>
                <dt>Last verification</dt>
                <dd>
                  {verification
                    ? `${verification.ok ? (verification.is_paper ? "paper account confirmed" : "NOT a paper account") : `failed: ${text(verification.error)}`} (${when(verification.verified_at)})`
                    : "never"}
                </dd>
              </dl>
              <div className="form-grid">
                <OperatorField value={actor} onChange={setActor} />
              </div>
              <div className="form-actions">
                <button type="button" className="btn" disabled={actor.trim().length < 2 || m.busy} onClick={() => void verify()}>
                  Verify broker connection (read-only)
                </button>
              </div>
              <p className="muted small">{text(d.paper_mode_ready)}</p>
            </Section>
            <Section title="Mode">
              <p>
                Current mode: <strong>{mode}</strong>. Paper mode needs a successful paper-account
                verification from the last hour and a stopped scheduler. Live mode is not available.
              </p>
              <div className="form-actions">
                {mode !== "paper" && (
                  <button type="button" className="btn" onClick={() => setDialog("paper")}>
                    Use paper trading (simulated funds)…
                  </button>
                )}
                {mode !== "shadow" && (
                  <button type="button" className="btn" onClick={() => setDialog("shadow")}>
                    Return to shadow mode…
                  </button>
                )}
              </div>
            </Section>
          </div>
          <div className="grid-2">
            <Section title="Data freshness">
              <DataTable
                rows={(d.data_freshness as Row[] | undefined) ?? []}
                empty="No market data stored yet."
                columns={[
                  { key: "symbol", label: "Symbol" },
                  { key: "source", label: "Source" },
                  { key: "last", label: "Last bar" },
                  { key: "fresh", label: "Fresh" },
                  { key: "freshness", label: "Detail" },
                ]}
              />
            </Section>
            <Section title="Checks">
              <dl className="kv">
                <dt>Latest reconciliation</dt>
                <dd>{recon ? `${recon.passed ? "matched" : "DIFFERENCES FOUND"} (${when(recon.at)})` : "none yet"}</dd>
                <dt>Latest pre-flight</dt>
                <dd>{preflight ? `${preflight.passed ? "passed" : "FAILED"} (${when(preflight.checked_at)})` : "none yet"}</dd>
              </dl>
            </Section>
          </div>
          <KillSwitchPanel status={killSwitch} onChanged={setKillSwitch} />
          <ActionResult error={m.error} message={m.message} />
        </>
      )}
      {dialog === "start" && (
        <ConfirmDialog
          title={`Start ${mode} trading`}
          description={
            paper
              ? "The worker will run the trading cycle against the Alpaca PAPER account (simulated funds) after its own checks. The kill switch must also be released before any order is sent."
              : "The worker will run the trading cycle in shadow mode: decisions are recorded, no orders are sent."
          }
          phrase={paper ? confirmations.start_paper ?? "" : confirmations.start_shadow ?? ""}
          submitLabel={`Start ${mode}`}
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
        />
      )}
      {dialog === "stop" && (
        <ConfirmDialog
          title="Stop the scheduler"
          description="No further cycle steps start. A step already running finishes safely. To block new risk immediately, also engage the kill switch."
          phrase=""
          submitLabel="Stop scheduler"
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
      {(dialog === "paper" || dialog === "shadow") && (
        <ConfirmDialog
          title={dialog === "paper" ? "Use paper trading (simulated funds)" : "Return to shadow mode"}
          description={
            dialog === "paper"
              ? "Paper mode sends orders to the Alpaca PAPER account — simulated money, never a real-money account. It applies after the scheduler is started."
              : "Shadow mode records decisions without sending orders."
          }
          phrase={dialog === "paper" ? confirmations.paper_mode ?? "" : confirmations.shadow_mode ?? ""}
          submitLabel={dialog === "paper" ? "Use paper trading" : "Use shadow mode"}
          tone="primary"
          busy={m.busy}
          error={m.error}
          onCancel={() => setDialog(null)}
          onSubmit={async (v) => {
            const out = await m.run((t) => mutate(t, "/api/v1/trading/mode", { ...v, target: dialog }));
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
