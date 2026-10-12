// ── Agents & storage: pool table + drive accounts, GitHub sync ─────────────

import { useState } from "react";
import { Bot, GitFork, HardDrive, Loader2 } from "lucide-react";
import { useApp } from "../context";
import { useToast } from "../components/Toasts";
import { useData, usePoll } from "../lib/hooks";
import { EmptyState, Pill, Progress, SectionTitle, Skeleton } from "../components/ui";

export default function Agents() {
  const { client, refreshKey } = useApp();
  const toast = useToast();
  const [syncing, setSyncing] = useState(false);

  const agents = useData(
    () => (client ? client.agentsStatus() : Promise.resolve(null)),
    [client, refreshKey],
  );
  const storage = useData(
    () => (client ? client.storageStatus() : Promise.resolve(null)),
    [client, refreshKey],
  );
  usePoll(agents.reload, 6000);

  const sync = async () => {
    if (!client || syncing) return;
    setSyncing(true);
    try {
      const res = await client.sync();
      const bits = [
        res.pushed != null ? `${res.pushed} pushed` : null,
        res.pulled != null ? `${res.pulled} pulled` : null,
        res.conflicts != null ? `${res.conflicts} conflicts` : null,
      ]
        .filter(Boolean)
        .join(" · ");
      toast.ok("GitHub sync complete", bits || res.detail || "nothing to move.");
    } catch (e) {
      toast.bad("Sync failed", e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  return (
    <>
      <div>
        <SectionTitle
          icon={<Bot size={13} />}
          right={
            <button
              type="button"
              className="btn btn--sm btn--peach"
              onClick={() => void sync()}
              disabled={syncing}
              data-tip="POST /sync — reconcile work with GitHub"
            >
              {syncing ? <Loader2 size={13} className="spin-anim" /> : <GitFork size={13} />}
              Sync to GitHub
            </button>
          }
        >
          Agent pool
        </SectionTitle>
        <div className="glass table-wrap" style={{ marginTop: 14 }}>
          {agents.loading ? (
            <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 12 }}>
              <Skeleton h={44} r={14} /> <Skeleton h={44} r={14} /> <Skeleton h={44} r={14} />
            </div>
          ) : agents.data && agents.data.agents.length > 0 ? (
            <div className="table-scroll">
              <table className="kv">
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>State</th>
                    <th>Done</th>
                    <th>Failed</th>
                    <th>Last error</th>
                  </tr>
                </thead>
                <tbody>
                  {agents.data.agents.map((a) => (
                    <tr key={a.account_id} style={{ "--rowtint": "var(--kv-grape)" } as React.CSSProperties}>
                      <td data-label="Agent">
                        <div className="row-title mono" style={{ color: "var(--kv-text)" }}>{a.label}</div>
                        <div className="row-sub mono">{a.account_id}</div>
                      </td>
                      <td data-label="State">
                        <Pill tone={a.busy ? "info" : "neutral"} className={a.busy ? "is-live" : undefined}>
                          {a.busy ? "busy" : "idle"}
                        </Pill>
                      </td>
                      <td data-label="Done"><span className="num" style={{ color: "var(--kv-ok)" }}>{a.tasks_done}</span></td>
                      <td data-label="Failed">
                        <span className="num" style={{ color: a.tasks_failed ? "var(--kv-bad)" : undefined }}>
                          {a.tasks_failed}
                        </span>
                      </td>
                      <td data-label="Last error" style={{ maxWidth: 340 }}>
                        {a.last_error ? (
                          <span className="mono" style={{ fontSize: 11.5, color: "var(--kv-bad)" }}>{a.last_error}</span>
                        ) : (
                          <span style={{ color: "var(--kv-text-3)", fontSize: 12 }}>clean</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="Pool is empty" hint="Agents register when the orchestrator starts — check the server logs if this is unexpected." />
          )}
        </div>
      </div>

      <div>
        <SectionTitle icon={<HardDrive size={13} />}>Storage accounts</SectionTitle>
        <div className="glass table-wrap" style={{ marginTop: 14 }}>
          {storage.loading ? (
            <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 12 }}>
              <Skeleton h={44} r={14} /> <Skeleton h={44} r={14} /> <Skeleton h={44} r={14} />
            </div>
          ) : storage.data && storage.data.per_account.length > 0 ? (
            <div className="table-scroll">
              <table className="kv">
                <thead>
                  <tr>
                    <th>Account</th>
                    <th>Health</th>
                    <th>Usage</th>
                    <th>Free</th>
                    <th>Bucket</th>
                  </tr>
                </thead>
                <tbody>
                  {storage.data.per_account.map((s) => {
                    const total = s.used_gb + s.free_gb;
                    const pct = (s.used_gb / Math.max(1, total)) * 100;
                    const tint = pct > 90 ? "var(--kv-bad)" : pct > 75 ? "var(--kv-warn)" : "var(--kv-lime)";
                    return (
                      <tr key={s.bucket} style={{ "--rowtint": "var(--kv-lime)" } as React.CSSProperties}>
                        <td data-label="Account"><span className="row-title">{s.label}</span></td>
                        <td data-label="Health">
                          <Pill tone={s.healthy ? "ok" : "bad"}>{s.healthy ? "healthy" : "fault"}</Pill>
                        </td>
                        <td data-label="Usage">
                          <div className="usage-bar">
                            <Progress value={pct} tint={tint} label={`${s.label} usage`} />
                            <span className="num">{s.used_gb.toFixed(1)} / {total.toFixed(0)} GB</span>
                          </div>
                        </td>
                        <td data-label="Free"><span className="num">{s.free_gb.toFixed(1)} GB</span></td>
                        <td data-label="Bucket"><span className="mono">{s.bucket}</span></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="No storage accounts" hint="Point KOLLEKTIV_STORAGE_ACCOUNTS at one or more remotes and restart." />
          )}
        </div>
        {storage.data ? (
          <p className="card__hint" style={{ margin: "10px 6px 0" }}>
            Pool total: <b className="num" style={{ color: "var(--kv-text-2)" }}>{storage.data.used_gb.toFixed(1)} / {storage.data.total_gb} GB</b> used across{" "}
            {storage.data.accounts} accounts — writes always land on the healthiest one.
          </p>
        ) : null}
      </div>
    </>
  );
}
