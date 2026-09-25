"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";

export interface ConfirmValues {
  actor: string;
  reason: string;
  confirm: string;
}

interface Props {
  title: string;
  description: string;
  phrase: string;
  submitLabel: string;
  tone: "critical" | "primary";
  busy?: boolean;
  error?: string | null;
  onSubmit: (v: ConfirmValues) => void;
  onCancel: () => void;
}

/**
 * A deliberate confirmation: a named operator, a reason, and the exact phrase
 * typed by hand (paste is allowed, but the text must match exactly). The API
 * checks the phrase again.
 */
export function ConfirmDialog(p: Props) {
  const [actor, setActor] = useState("");
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState("");
  const id = useId();
  const first = useRef<HTMLInputElement>(null);
  const cancel = useRef(p.onCancel);
  cancel.current = p.onCancel;

  useEffect(() => {
    first.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") cancel.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const ready =
    actor.trim().length >= 2 && reason.trim().length >= 5 && confirm === p.phrase && !p.busy;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready) p.onSubmit({ actor: actor.trim(), reason: reason.trim(), confirm });
  }

  return (
    <div className="overlay">
      <form
        className="card dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        onSubmit={submit}
      >
        <h2 id={`${id}-title`}>{p.title}</h2>
        <p>{p.description}</p>
        <label htmlFor={`${id}-actor`}>Your name (operator)</label>
        <input
          ref={first}
          id={`${id}-actor`}
          value={actor}
          maxLength={128}
          autoComplete="off"
          onChange={(e) => setActor(e.target.value)}
        />
        <label htmlFor={`${id}-reason`}>Reason</label>
        <textarea
          id={`${id}-reason`}
          value={reason}
          maxLength={1000}
          rows={3}
          onChange={(e) => setReason(e.target.value)}
        />
        <label htmlFor={`${id}-confirm`}>
          Type <code>{p.phrase}</code> to confirm
        </label>
        <input
          id={`${id}-confirm`}
          value={confirm}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setConfirm(e.target.value)}
        />
        {p.error && (
          <p role="alert" className="alert critical">
            {p.error}
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={p.onCancel}>
            Cancel
          </button>
          <button type="submit" className={`btn ${p.tone}`} disabled={!ready}>
            {p.busy ? "Sending…" : p.submitLabel}
          </button>
        </div>
      </form>
    </div>
  );
}
