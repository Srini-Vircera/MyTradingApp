import { describe, expect, it } from "vitest";
import { describeMode } from "@/lib/mode";
import { money, num, pct, weights } from "@/lib/format";
import {
  allocationSeries,
  bandSeries,
  comparePositions,
  performanceSeries,
  signalCells,
} from "@/lib/series";
import { clearToken, loadToken, saveToken } from "@/lib/token";
import { BANNER } from "./helpers";

describe("mode banner logic", () => {
  it("labels paper, shadow and backtest modes", () => {
    expect(describeMode(BANNER).label).toBe("PAPER TRADING");
    expect(describeMode({ ...BANNER, mode: "shadow" }).tone).toBe("shadow");
    expect(describeMode({ ...BANNER, mode: "backtest" }).tone).toBe("neutral");
  });

  it("flags real money loudly", () => {
    const v = describeMode({ ...BANNER, mode: "live", uses_real_money: true });
    expect(v.tone).toBe("live");
    expect(v.label).toContain("REAL MONEY");
    expect(describeMode({ ...BANNER, uses_real_money: true }).tone).toBe("live");
  });

  it("treats a missing or unrecognised mode as a warning, never as safe", () => {
    expect(describeMode(null).tone).toBe("unknown");
    expect(describeMode({ ...BANNER, mode: "turbo" }).tone).toBe("unknown");
  });

  it("warns when live trading is enabled in the configuration", () => {
    expect(describeMode({ ...BANNER, live_trading_enabled: true }).detail).toContain("WARNING");
  });
});

describe("formatting", () => {
  it("handles decimal strings, nulls and garbage", () => {
    expect(num("1500.25")).toBe(1500.25);
    expect(num("abc")).toBeNull();
    expect(num(Number.NaN)).toBeNull();
    expect(pct("0.0123")).toBe("1.23%");
    expect(pct(null)).toBe("—");
    expect(money("100000")).toBe("$100,000.00");
    expect(weights({ TQQQ: "0.5", QQQ: 0.25, bad: "x" })).toEqual({ TQQQ: 0.5, QQQ: 0.25 });
  });
});

describe("series", () => {
  it("builds performance series with drawdown plotted below zero", () => {
    const s = performanceSeries([
      { session_date: "2026-09-01", equity: "100000", drawdown: 0, daily_return: 0 },
      { session_date: "2026-09-02", equity: "99000", drawdown: 0.01, daily_return: -0.01 },
    ]);
    expect(s.equity.map((p) => p.y)).toEqual([100000, 99000]);
    expect(s.drawdown[1]!.y).toBeCloseTo(-0.01);
    expect(s.returns[1]!.y).toBe(-0.01);
  });

  it("orders risk history oldest first", () => {
    const hist = [
      { created_at: "2026-09-02", approved_weights_json: { QQQ: "0.4" }, drawdown_band: "caution", blocked_risk_increasing: false },
      { created_at: "2026-09-01", approved_weights_json: { TQQQ: "0.5" }, drawdown_band: "normal", blocked_risk_increasing: true },
    ];
    expect(allocationSeries(hist).map((p) => p.x)).toEqual(["2026-09-01", "2026-09-02"]);
    expect(allocationSeries(hist)[0]!.weights).toEqual({ TQQQ: 0.5 });
    expect(bandSeries(hist)[0]).toEqual({ x: "2026-09-01", band: "normal", blocked: true });
  });

  it("builds heatmap cells and skips unusable scores", () => {
    const cells = signalCells([
      { strategy_id: "trend", timestamp: "t1", normalized_score: 0.6, direction: "long", confidence: 0.8 },
      { strategy_id: "mr", timestamp: "t1", normalized_score: null },
    ]);
    expect(cells).toHaveLength(1);
    expect(cells[0]).toMatchObject({ row: "trend", col: "t1", value: 0.6 });
  });

  it("compares broker and expected positions", () => {
    const rows = comparePositions(
      [{ symbol: "TQQQ", quantity: "10", market_value: "500" }],
      [
        { symbol: "TQQQ", quantity: "10" },
        { symbol: "QQQ", quantity: "2" },
      ],
    );
    expect(rows).toEqual([
      { symbol: "QQQ", broker: null, expected: 2, market_value: null, matches: false },
      { symbol: "TQQQ", broker: 10, expected: 10, market_value: 500, matches: true },
    ]);
  });
});

describe("token storage", () => {
  it("keeps the token in sessionStorage only", () => {
    saveToken("secret-token-value");
    expect(loadToken()).toBe("secret-token-value");
    expect(window.sessionStorage.length).toBe(1);
    expect(window.localStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    clearToken();
    expect(loadToken()).toBeNull();
  });
});
