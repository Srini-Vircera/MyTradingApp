"use client";

import { useState } from "react";
import {
  ApiError,
  ENGAGE_CONFIRMATION,
  RELEASE_CONFIRMATION,
  killSwitch,
  type KillSwitchView,
} from "@/lib/api";
import { when } from "@/lib/format";
import { useSession } from "@/lib/session";
import { ConfirmDialog, type ConfirmValues } from "./ConfirmDialog";

interface Props {
  status: KillSwitchView | null;
  onChanged: (v: KillSwitchView) => void;
}

function useAction(action: "engage" | "release", onChanged: (v: KillSwitchView) => void) {
  const { token, signOut } = useSession();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(v: ConfirmValues) {
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      onChanged(await killSwitch(token, action, v));
      setOpen(false);
    } catch (exc) {
      const err = exc instanceof ApiError ? exc : null;
      if (err?.unauthorized) signOut("The API rejected the token. Enter it again.");
      setError(
        err
          ? `${err.detail}${err.hint ? ` — ${err.hint}` : ""}`
          : "The request failed. Use the CLI (aq kill-switch engage) if the API is unreachable.",
      );
    } finally {
      setBusy(false);
    }
  }
  return {
    open,
    busy,
    error,
    submit,
    show: () => {
      setError(null);
      setOpen(true);
    },
    hide: () => setOpen(false),
  };
}

/** Kill-switch state pill; engaged or unknown is the safe state. */
export function KillSwitchState({ status }: { status: KillSwitchView | null }) {
  if (!status) {
    return <span className="pill critical">Kill switch: unknown</span>;
  }
  return status.engaged ? (
    <span className="pill critical" title={status.reason}>
      ■ Trading stopped (kill switch engaged{status.fail_safe ? ", fail-safe" : ""})
    </span>
  ) : (
    <span className="pill good">● Automated trading allowed</span>
  );
}

/** The STOP AUTOMATED TRADING button, always available in the header. */
export function StopTradingButton({ onChanged }: Pick<Props, "onChanged">) {
  const a = useAction("engage", onChanged);
  return (
    <>
      <button type="button" className="btn critical stop" onClick={a.show}>
        STOP AUTOMATED TRADING
      </button>
      {a.open && (
        <ConfirmDialog
          title="Stop automated trading"
          description="Engages the kill switch: no new risk-increasing orders are sent until an operator re-enables trading. Existing positions are not closed."
          phrase={ENGAGE_CONFIRMATION}
          submitLabel="Engage kill switch"
          tone="critical"
          busy={a.busy}
          error={a.error}
          onSubmit={(v) => void a.submit(v)}
          onCancel={a.hide}
        />
      )}
    </>
  );
}

/** Kill-switch details plus the (deliberately less prominent) release flow. */
export function KillSwitchPanel({ status, onChanged }: Props) {
  const a = useAction("release", onChanged);
  return (
    <div>
      <KillSwitchState status={status} />
      {status && (
        <dl className="kv">
          <dt>Reason</dt>
          <dd>{status.reason || "—"}</dd>
          <dt>Changed by</dt>
          <dd>{status.actor ?? "—"}</dd>
          <dt>Changed at</dt>
          <dd>{when(status.changed_at)}</dd>
        </dl>
      )}
      {status?.engaged && (
        <button type="button" className="btn" onClick={a.show}>
          Re-enable trading…
        </button>
      )}
      {a.open && (
        <ConfirmDialog
          title="Re-enable automated trading"
          description="Releases the kill switch. Check the reason it was engaged, the latest reconciliation and system health first. Paper/live mode itself is not changed here."
          phrase={RELEASE_CONFIRMATION}
          submitLabel="Release kill switch"
          tone="primary"
          busy={a.busy}
          error={a.error}
          onSubmit={(v) => void a.submit(v)}
          onCancel={a.hide}
        />
      )}
    </div>
  );
}
