"use client";

import { JsonView, LoadState, PageHeader, Section, StatusBadge } from "@/components/ui";
import { text } from "@/lib/format";
import { useApi } from "@/lib/session";

export default function ConfigurationPage() {
  const c = useApi("/api/v1/configuration");
  const d = c.data;
  const lock = d?.live_trading_lock ?? {};
  return (
    <>
      <PageHeader title="Configuration">
        The reviewed configuration files (YAML), with secrets redacted. Operator changes made on the
        Settings, Strategies and Trading Control pages are layered on top and listed there; the
        mode banner shows the effective mode.
      </PageHeader>
      <LoadState loading={c.loading && !d} error={c.error} />
      <Section title="Trading mode lock">
        <dl className="kv">
          <dt>Mode</dt>
          <dd>{text(lock.mode)}</dd>
          <dt>Uses real money</dt>
          <dd>
            <StatusBadge status={lock.uses_real_money ? "critical" : "good"} label={lock.uses_real_money ? "YES" : "no"} />
          </dd>
          <dt>Live trading enabled in config</dt>
          <dd>
            <StatusBadge
              status={lock.live_trading_enabled_in_config ? "critical" : "good"}
              label={lock.live_trading_enabled_in_config ? "YES" : "no"}
            />
          </dd>
          <dt>Changeable from the API/dashboard</dt>
          <dd>{lock.changeable_via_api ? "yes" : "no"}</dd>
          <dt>How to change</dt>
          <dd>{text(lock.how_to_change)}</dd>
          <dt>Config version</dt>
          <dd>
            <code>{d?.config_version ?? "—"}</code>
          </dd>
        </dl>
      </Section>
      {d && d.warnings.length > 0 && (
        <Section title="Configuration warnings">
          <ul>
            {d.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="Resolved settings">
        {d && Object.entries(d.settings).map(([k, v]) => <JsonView key={k} label={k} value={v} />)}
      </Section>
    </>
  );
}
