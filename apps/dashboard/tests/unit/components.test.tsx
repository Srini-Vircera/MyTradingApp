import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AllocationChart, BandHistory, Heatmap, LineChart, ReturnBars } from "@/components/charts";
import { KillSwitchPanel, StopTradingButton } from "@/components/KillSwitch";
import { ModeBanner } from "@/components/ModeBanner";
import { TokenGate } from "@/components/TokenGate";
import { SessionProvider } from "@/lib/session";
import { BANNER, mockFetch, TOKEN, withSession } from "./helpers";

const ENGAGED = {
  engaged: true,
  reason: "manual stop",
  actor: "operator:ann",
  changed_at: "2026-09-24T19:00:00Z",
  fail_safe: false,
};

describe("ModeBanner", () => {
  it("shows PAPER prominently", () => {
    render(<ModeBanner banner={BANNER} />);
    expect(screen.getByTestId("mode-banner")).toHaveClass("mode-paper");
    expect(screen.getByText("PAPER TRADING")).toBeTruthy();
  });

  it("shows LIVE / real money and unknown modes as warnings", () => {
    const { rerender } = render(<ModeBanner banner={{ ...BANNER, mode: "live", uses_real_money: true }} />);
    expect(screen.getByTestId("mode-banner")).toHaveClass("mode-live");
    expect(screen.getByText(/REAL MONEY/)).toBeTruthy();
    rerender(<ModeBanner banner={null} />);
    expect(screen.getByText("MODE UNKNOWN")).toBeTruthy();
  });
});

describe("STOP AUTOMATED TRADING", () => {
  async function openDialog() {
    const onChanged = vi.fn();
    withSession(<StopTradingButton onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "STOP AUTOMATED TRADING" }));
    return onChanged;
  }

  it("requires a name, a reason and the exact phrase before it can be sent", async () => {
    const calls = mockFetch(() => ({ json: ENGAGED }));
    await openDialog();
    const submit = screen.getByRole("button", { name: "Engage kill switch" });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Your name (operator)"), "ann");
    await userEvent.type(screen.getByLabelText("Reason"), "manual stop");
    const confirm = screen.getByLabelText(/to confirm/);
    for (const wrong of ["stop automated trading", "STOP AUTOMATED TRADING ", "STOP"]) {
      fireEvent.change(confirm, { target: { value: wrong } });
      expect(submit).toBeDisabled();
    }
    fireEvent.change(confirm, { target: { value: "STOP AUTOMATED TRADING" } });
    expect(submit).toBeEnabled();
    expect(calls).toHaveLength(0); // nothing is sent before confirmation
  });

  it("engages via the API with the operator's name and reason", async () => {
    const calls = mockFetch(() => ({ json: ENGAGED }));
    const onChanged = await openDialog();
    await userEvent.type(screen.getByLabelText("Your name (operator)"), "ann");
    await userEvent.type(screen.getByLabelText("Reason"), "manual stop");
    await userEvent.type(screen.getByLabelText(/to confirm/), "STOP AUTOMATED TRADING");
    await userEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(ENGAGED));
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({
      method: "POST",
      url: "http://localhost:8000/api/v1/kill-switch/engage",
      body: { actor: "ann", reason: "manual stop", confirm: "STOP AUTOMATED TRADING" },
    });
    expect(calls[0]!.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the dialog open and shows the API's refusal", async () => {
    mockFetch(() => ({ status: 400, json: { detail: "type the confirmation phrase exactly" } }));
    const onChanged = await openDialog();
    await userEvent.type(screen.getByLabelText("Your name (operator)"), "ann");
    await userEvent.type(screen.getByLabelText("Reason"), "manual stop");
    await userEvent.type(screen.getByLabelText(/to confirm/), "STOP AUTOMATED TRADING");
    await userEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("type the confirmation phrase exactly");
    expect(onChanged).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("cancels with Escape without sending anything", async () => {
    const calls = mockFetch(() => ({ json: ENGAGED }));
    await openDialog();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls).toHaveLength(0);
  });
});

describe("kill-switch release", () => {
  it("is offered only while engaged and needs its own phrase", async () => {
    const calls = mockFetch(() => ({ json: { ...ENGAGED, engaged: false } }));
    const onChanged = vi.fn();
    const { rerender } = withSession(<KillSwitchPanel status={{ ...ENGAGED, engaged: false }} onChanged={onChanged} />);
    expect(screen.queryByRole("button", { name: /Re-enable/ })).toBeNull();
    rerender(
      <SessionProvider>
        <KillSwitchPanel status={ENGAGED} onChanged={onChanged} />
      </SessionProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: /Re-enable/ }));
    await userEvent.type(screen.getByLabelText("Your name (operator)"), "ann");
    await userEvent.type(screen.getByLabelText("Reason"), "checked reconciliation");
    await userEvent.type(screen.getByLabelText(/to confirm/), "STOP AUTOMATED TRADING");
    expect(screen.getByRole("button", { name: "Release kill switch" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "RE-ENABLE TRADING" } });
    await userEvent.click(screen.getByRole("button", { name: "Release kill switch" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(new URL(calls[0]!.url).pathname).toBe("/api/v1/kill-switch/release");
  });

  it("shows an unknown kill-switch state as unsafe", () => {
    withSession(<KillSwitchPanel status={null} onChanged={vi.fn()} />);
    expect(screen.getByText("Kill switch: unknown")).toHaveClass("critical");
  });
});

describe("TokenGate", () => {
  it("stores the typed token for this tab only", async () => {
    render(
      <SessionProvider>
        <TokenGate />
      </SessionProvider>,
    );
    const input = screen.getByLabelText("Operator token");
    expect(input).toHaveAttribute("type", "password");
    await userEvent.type(input, "my-operator-token");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(window.sessionStorage.getItem("aq.operator-token")).toBe("my-operator-token");
    expect(window.localStorage.length).toBe(0);
  });
});

describe("charts", () => {
  const fmt = (v: number) => v.toFixed(2);

  it("render an empty state instead of failing on missing data", () => {
    render(
      <>
        <LineChart title="Equity" points={[]} format={fmt} />
        <ReturnBars title="Returns" points={[{ x: "d", y: Number.NaN }]} format={fmt} />
        <AllocationChart title="Alloc" points={[]} />
        <BandHistory title="Bands" items={[]} />
        <Heatmap title="Heat" cells={[]} />
      </>,
    );
    expect(screen.getAllByText("No data yet.")).toHaveLength(5);
  });

  it("render single points and flat series without NaN geometry", () => {
    const { container } = render(
      <>
        <LineChart title="Equity" points={[{ x: "2026-09-01", y: 100 }]} format={fmt} />
        <LineChart title="Flat" points={[{ x: "a", y: 1 }, { x: "b", y: 1 }]} format={fmt} area />
        <AllocationChart title="Alloc" points={[{ x: "t", weights: { TQQQ: 0.5 } }]} />
      </>,
    );
    expect(container.innerHTML).not.toContain("NaN");
  });

  it("offer a data-table view", async () => {
    render(
      <AllocationChart
        title="Alloc"
        points={[
          { x: "t1", weights: { TQQQ: 0.6, SQQQ: 0 } },
          { x: "t2", weights: { QQQ: 0.3, XYZ: 0.1 } },
        ]}
      />,
    );
    expect(screen.getByText("Other")).toBeTruthy(); // unknown symbols fold into "Other"
    await userEvent.click(screen.getByRole("button", { name: "Show table" }));
    const table = screen.getByRole("table");
    expect(table).toHaveTextContent("60.0%");
    expect(table).toHaveTextContent("40.0%"); // t1 cash remainder
  });
});
