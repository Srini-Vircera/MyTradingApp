import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Checklist, TradingModePanel } from "@/components/TradingMode";
import { mockFetch, withSession } from "./helpers";

const CONTROL = {
  mode: "shadow",
  environment: "production",
  mode_explanations: {
    shadow: "Strategies, signals and orders are calculated and recorded, but no orders are sent to a broker.",
    paper: "Orders may be submitted only to the verified Alpaca paper-trading account and use simulated funds.",
  },
  automation: { desired: "stopped" },
  broker: { verification: null },
  confirmations: { paper_mode: "Switch to PAPER trading", shadow_mode: "Switch to SHADOW mode" },
  paper_switch: {
    allowed: false,
    problems: ["the Alpaca paper account has not been verified yet"],
    checks: [],
    verified_at: null,
  },
};

describe("Trading Mode selector", () => {
  it("offers Shadow and Paper only, with explanations; live is locked", () => {
    withSession(<TradingModePanel c={CONTROL} onChanged={vi.fn()} />);
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => (r as HTMLInputElement).value)).toEqual(["shadow", "paper"]);
    expect(screen.getByText(/no orders are sent to a broker/)).toBeTruthy();
    expect(screen.getByText(/use simulated funds/)).toBeTruthy();
    expect(screen.getByText(/Live Trading: LOCKED \/ NOT AVAILABLE/)).toBeTruthy();
    expect(screen.queryByRole("radio", { name: /live/i })).toBeNull();
  });

  it("explains why PAPER is not available and keeps the switch disabled", async () => {
    mockFetch(() => ({ json: {} }));
    withSession(<TradingModePanel c={CONTROL} onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("radio", { name: /Paper/ }));
    expect(screen.getByText("PAPER TRADING — SIMULATED FUNDS")).toBeTruthy();
    expect(screen.getByText(/has not been verified yet/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Switch to PAPER trading…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Verify Alpaca paper account (read-only)" })).toBeDisabled();
  });

  it("switches only with the exact phrase, and never starts automation", async () => {
    const calls = mockFetch(() => ({ json: { ok: true, message: "Trading mode is now PAPER", detail: {} } }));
    const allowed = {
      ...CONTROL,
      paper_switch: {
        allowed: true,
        problems: [],
        checks: [{ name: "paper_account", label: "Account is a paper account", passed: true, detail: "paper" }],
        verified_at: "2026-09-27T12:00:00Z",
      },
    };
    const onChanged = vi.fn();
    withSession(<TradingModePanel c={allowed} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("radio", { name: /Paper/ }));
    await userEvent.click(screen.getByRole("button", { name: "Switch to PAPER trading…" }));
    const submit = screen.getByRole("button", { name: "Switch to PAPER" });
    await userEvent.type(screen.getByLabelText("Your name (operator)"), "ann");
    await userEvent.type(screen.getByLabelText("Reason"), "paper run");
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "switch to paper trading" } });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "Switch to PAPER trading" } });
    await userEvent.click(submit);
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(calls.map((c) => [c.method, new URL(c.url).pathname])).toEqual([["POST", "/api/v1/trading/mode"]]);
    expect(calls[0]!.body).toMatchObject({ target: "paper", confirm: "Switch to PAPER trading" });
  });
});

describe("Checklist", () => {
  it("marks each item and explains what to do", () => {
    withSession(
      <Checklist
        label="ready"
        items={[
          { key: "mode", label: "Trading mode: PAPER", ok: true, detail: "PAPER" },
          { key: "master_gate", label: "Scheduler master gate", ok: false, detail: "OFF", fix: "Set AQ_SCHEDULER_ENABLED=true" },
        ]}
      />,
    );
    expect(screen.getByText("✓")).toBeTruthy();
    expect(screen.getByText("✗")).toBeTruthy();
    expect(screen.getByText(/Set AQ_SCHEDULER_ENABLED=true/)).toBeTruthy();
  });
});
