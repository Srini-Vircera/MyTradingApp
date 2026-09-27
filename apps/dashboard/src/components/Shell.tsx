"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import type { Banner, KillSwitchView } from "@/lib/api";
import { SessionProvider, useApi, useSession } from "@/lib/session";
import { KillSwitchState, StopTradingButton } from "./KillSwitch";
import { ModeBanner } from "./ModeBanner";
import { TokenGate } from "./TokenGate";

export const NAV = [
  ["/", "Overview"],
  ["/trading/", "Trading Control"],
  ["/portfolio/", "Portfolio"],
  ["/signals/", "Signals"],
  ["/risk/", "Risk"],
  ["/performance/", "Performance"],
  ["/orders/", "Orders"],
  ["/executions/", "Executions"],
  ["/data/", "Data"],
  ["/backtests/", "Backtests"],
  ["/research/", "Research"],
  ["/strategies/", "Strategies"],
  ["/jobs/", "Jobs"],
  ["/settings/", "Settings"],
  ["/system/", "System Health"],
  ["/configuration/", "Configuration"],
  ["/live-readiness/", "Live Readiness"],
] as const;

interface ShellState {
  banner: Banner | null;
  killSwitch: KillSwitchView | null;
  setKillSwitch: (v: KillSwitchView) => void;
}

const ShellContext = createContext<ShellState | null>(null);

export function useShell(): ShellState {
  const s = useContext(ShellContext);
  if (!s) throw new Error("useShell outside Shell");
  return s;
}

function Frame({ children }: { children: ReactNode }) {
  const { signOut } = useSession();
  const pathname = usePathname();
  // /configuration works without the database, so the mode banner is always available.
  const config = useApi("/api/v1/configuration", undefined, 60_000);
  const ks = useApi("/api/v1/kill-switch", undefined, 15_000);
  const [killSwitch, setKillSwitch] = useState<KillSwitchView | null>(null);
  useEffect(() => {
    if (ks.data) setKillSwitch(ks.data);
    else if (ks.error) setKillSwitch(null);
  }, [ks.data, ks.error]);
  const banner = config.data?.banner ?? null;

  return (
    <ShellContext.Provider value={{ banner, killSwitch, setKillSwitch }}>
      <header className="top">
        <ModeBanner banner={banner} />
        <div className="toolbar">
          <span className="brand">Adaptive Quant</span>
          <KillSwitchState status={killSwitch} />
          <span className="spacer" />
          <StopTradingButton onChanged={setKillSwitch} />
          <button type="button" className="btn ghost" onClick={() => signOut()}>
            Sign out
          </button>
        </div>
        <nav aria-label="Pages">
          {NAV.map(([href, label]) => (
            <Link
              key={href}
              href={href}
              aria-current={pathname === href || `${pathname}/` === href ? "page" : undefined}
            >
              {label}
            </Link>
          ))}
        </nav>
      </header>
      <main className="content">{children}</main>
      <footer className="notice">
        {banner?.notice ??
          "Paper/simulated results and backtests are hypothetical and are not a prediction of future returns."}{" "}
        This dashboard cannot place an order itself, enable live trading, approve a strategy for
        live trading or change deployment secrets.
      </footer>
    </ShellContext.Provider>
  );
}

function Gate({ children }: { children: ReactNode }) {
  const { token } = useSession();
  return token ? <Frame>{children}</Frame> : <TokenGate />;
}

export function Shell({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <Gate>{children}</Gate>
    </SessionProvider>
  );
}
