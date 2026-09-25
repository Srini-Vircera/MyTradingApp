import type { Banner } from "./api";

export type ModeTone = "live" | "paper" | "shadow" | "neutral" | "unknown";

export interface ModeView {
  tone: ModeTone;
  label: string;
  detail: string;
}

/** How the trading mode is presented. Anything unexpected is shown as a warning. */
export function describeMode(banner: Banner | null | undefined): ModeView {
  if (!banner) {
    return {
      tone: "unknown",
      label: "MODE UNKNOWN",
      detail: "The trading mode could not be read from the API.",
    };
  }
  const env = banner.live_trading_enabled
    ? `${banner.environment} (WARNING: live trading is enabled in the configuration)`
    : banner.environment;
  if (banner.uses_real_money || banner.mode === "live") {
    return {
      tone: "live",
      label: "LIVE TRADING — REAL MONEY",
      detail: `Environment ${env}. Orders use real money.`,
    };
  }
  switch (banner.mode) {
    case "paper":
      return {
        tone: "paper",
        label: "PAPER TRADING",
        detail: `Environment ${env}. Broker paper account — no real money.`,
      };
    case "shadow":
      return {
        tone: "shadow",
        label: "SHADOW MODE",
        detail: `Environment ${env}. Orders are computed and recorded, never sent.`,
      };
    case "backtest":
      return {
        tone: "neutral",
        label: "BACKTEST ONLY",
        detail: `Environment ${env}. No broker connection.`,
      };
    default:
      return {
        tone: "unknown",
        label: `UNRECOGNISED MODE: ${banner.mode.toUpperCase()}`,
        detail: `Environment ${env}. Treat as unsafe until confirmed.`,
      };
  }
}

export type Status = "good" | "warning" | "serious" | "critical" | "neutral";

const BANDS: Record<string, Status> = {
  normal: "good",
  caution: "warning",
  defensive: "serious",
  emergency: "critical",
};

export function bandStatus(band: unknown): Status {
  return typeof band === "string" ? (BANDS[band] ?? "neutral") : "neutral";
}
