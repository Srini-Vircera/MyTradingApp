import type { Banner } from "@/lib/api";
import { describeMode } from "@/lib/mode";

/** The always-visible trading-mode banner. Unknown or unexpected modes are shown as warnings. */
export function ModeBanner({ banner }: { banner: Banner | null | undefined }) {
  const m = describeMode(banner);
  return (
    <div className={`mode-banner mode-${m.tone}`} role="status" aria-live="polite" data-testid="mode-banner">
      <strong className="mode-label">{m.label}</strong>
      <span className="mode-detail">{m.detail}</span>
      {banner && <span className="mode-config">config {banner.config_version.slice(0, 12)}</span>}
    </div>
  );
}
