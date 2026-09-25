"use client";

import { useState, type FormEvent } from "react";
import { apiOrigin } from "@/lib/api";
import { useSession } from "@/lib/session";

/** Asks for the operator API token. It is kept for this browser tab only. */
export function TokenGate() {
  const { signIn, notice } = useSession();
  const [value, setValue] = useState("");

  function submit(e: FormEvent) {
    e.preventDefault();
    const t = value.trim();
    if (t) signIn(t);
  }

  return (
    <main className="gate">
      <form onSubmit={submit} className="card gate-card" aria-labelledby="gate-title">
        <h1 id="gate-title">Adaptive Quant — operator dashboard</h1>
        <p className="muted">
          Enter the operator API token (<code>AQ_API_TOKEN</code>) for {apiOrigin()}. It is kept
          in this browser tab only and cleared when the tab closes.
        </p>
        {notice && (
          <p role="alert" className="alert critical">
            {notice}
          </p>
        )}
        <label htmlFor="token">Operator token</label>
        <input
          id="token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button type="submit" className="btn primary" disabled={!value.trim()}>
          Continue
        </button>
      </form>
    </main>
  );
}
