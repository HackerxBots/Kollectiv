// ── Overview: warnings, six subsystem stat cards, projects at a glance ─────

import { Activity, ArrowRight, Bot, Brain, FolderGit2, Gauge, GitBranch, HardDrive, Info, TriangleAlert, Unplug } from "lucide-react";
import { useApp } from "../context";
import { useHealth } from "../components/Chrome";
import { useData, usePoll, useTilt, fmtAgo } from "../lib/hooks";
import { EmptyState, Progress, SectionTitle, Skeleton, Spark, StatusPill } from "../components/ui";
import type { ReactNode } from "react";

function StatCard({
  tint,
  icon,
  label,
  value,
  sub,
  onClick,
  go,
}: {
  tint: string;
  icon: ReactNode;
  label: string;
  value: ReactNode;
  sub: ReactNode;
  onClick?: () => void;
  go?: boolean;
}) {
  const ref = useTilt<HTMLButtonElement>();
  return (
    <button
      ref={ref}
      type="button"
      className="stat-card"
      style={{ "--tint": tint } as React.CSSProperties}
      onClick={onClick}
      disabled={!onClick}
      aria-label={`${label}: ${typeof value === "string" ? value : label}`}
    >
      <Spark tint={tint}>{icon}</Spark>
      {go ? <span className="stat-card__go"><ArrowRight size={15} /></span> : null}
      <div>
        <div className="stat-card__label">{label}</div>
        <div className="stat-card__value" style={{ color: "var(--kv-text)" }}>{value}</div>
      </div>
      <div className="stat-card__sub">{sub}</div>
    </button>
  );
}

/** Highlight every env var named in a warning as a mono chip. */
function WarningLine({ text }: { text: string }) {
  const parts = text.split(/(\b[A-Z][A-Z0-9_]{6,}\b)/g);
  return (
    <span>
      {parts.map((p, i) =>
        /^[A-Z][A-Z0-9_]{6,}$/.test(p) ? (
          <code className="env-chip" key={i}>{p}</code>
        ) : (
          <span key={i}>{p + (i < parts.length - 1 ? "" : "")}</span>
        ),
      )}
    </span>
  );
}

function StatSkeleton() {
  return (
    <div className="stat-card" aria-hidden>
      <Skeleton w={42} h={42} r={14} />
      <Skeleton w="55%" h={11} />
      <Skeleton w="80%" h={26} />
      <Skeleton w="65%" h={12} />
    </div>
  );
}

export default function Overview({ navigate }: { navigate: (r: "overview" | "projects" | "connectors" | "agents") => void }) {
  const { client, mode, bump } = useApp();
  const { data: health, loading, error } = useHealth();
  const { data: projects, reload: reloadProjects } = useData(
    () => (client ? client.listProjects() : Promise.resolve(null)),
    [client],
  );
  usePoll(reloadProjects, 8000);
  const { data: storage } = useData(
    () => (client ? client.storageStatus() : Promise.resolve(null)),
    [client],
  );

  const sub = health?.subsystems;
  const warnings = health?.warnings ?? [];

  return (
    <>
      {mode === "demo" && (
        <div className="banner banner--info" role="status">
          <span className="banner__icon"><Info /></span>
          <div>
            <div className="banner__title">Showing demo data</div>
            <div className="banner__list">
              <span>
                The API at your base URL did not answer, so this session runs on a simulated
                backend. Actions do nothing real. Point the top bar at your Kollektiv server
                and press <b>Connect</b>.
              </span>
            </div>
          </div>
        </div>
      )}

      {!loading && warnings.length > 0 && (
        <div className="banner banner--warn" role="alert">
          <span className="banner__icon"><TriangleAlert /></span>
          <div>
            <div className="banner__title">{warnings.length} warning{warnings.length > 1 ? "s" : ""} from /health</div>
            <div className="banner__list">
              {warnings.map((w) => (
                <WarningLine key={w} text={w} />
              ))}
            </div>
          </div>
        </div>
      )}

      {error && !health && mode === "live" && (
        <div className="banner banner--warn" role="alert">
          <span className="banner__icon"><TriangleAlert /></span>
          <div>
            <div className="banner__title">/health failed</div>
            <div className="banner__list"><span>{error}</span></div>
          </div>
        </div>
      )}

      <div>
        <SectionTitle icon={<Gauge size={13} />}>Subsystem status</SectionTitle>
        <div className="stat-grid stat-grid--six" style={{ marginTop: 14 }}>
          {loading || !sub ? (
            Array.from({ length: 6 }, (_, i) => <StatSkeleton key={i} />)
          ) : (
            <>
              <StatCard
                tint="var(--kv-cyan)"
                icon={<Activity />}
                label="API"
                value={<>{health?.status ?? "—"} <small>{health?.environment}</small></>}
                sub={mode === "live" ? "serving live data" : "simulated demo data"}
                onClick={() => bump()}
                go
              />
              <StatCard
                tint="var(--kv-lime)"
                icon={<HardDrive />}
                label="Storage"
                value={
                  storage ? (
                    <>{storage.used_gb.toFixed(1)}<small> / {storage.total_gb} GB</small></>
                  ) : (
                    "…"
                  )
                }
                sub={
                  storage ? (
                    <span style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      <Progress
                        value={(storage.used_gb / Math.max(1, storage.total_gb)) * 100}
                        tint={"var(--kv-lime)"}
                        label="storage usage"
                      />
                      <span><span className="mono">{sub.storage.backend}</span> · {sub.storage.accounts} accounts · {sub.storage.healthy ? "healthy" : "degraded"}</span>
                    </span>
                  ) : (
                    <>backend <span className="mono">{sub.storage.backend}</span> · {sub.storage.accounts} accounts</>
                  )
                }
                onClick={() => navigate("agents")}
                go
              />
              <StatCard
                tint="var(--kv-grape)"
                icon={<Bot />}
                label="Agents"
                value={<>{sub.agents.busy}<small> / {sub.agents.agents} busy</small></>}
                sub={sub.agents.busy === 0 ? "pool idle — ready for a run" : "work in flight right now"}
                onClick={() => navigate("agents")}
                go
              />
              <StatCard
                tint="var(--kv-pink)"
                icon={<Brain />}
                label="Brain"
                value={<>{sub.brain.calls}<small> calls</small></>}
                sub={
                  sub.brain.configured ? (
                    <>provider <span className="mono">{sub.brain.provider}</span> · {sub.brain.failures} failed</>
                  ) : (
                    <span className="mono">KOLLEKTIV_BRAIN_PROVIDER unset</span>
                  )
                }
                onClick={() => navigate("projects")}
                go
              />
              <StatCard
                tint="var(--kv-peach)"
                icon={<GitBranch />}
                label="GitHub"
                value={
                  sub.github.configured && sub.github.repo ? (
                    <span className="mono" style={{ fontSize: 16 }}>{sub.github.repo}</span>
                  ) : "not linked"
                }
                sub={sub.github.configured ? "sync endpoint available" : "set the repo slug to enable sync"}
                onClick={() => navigate("agents")}
                go
              />
              <StatCard
                tint="var(--kv-sky)"
                icon={<Unplug />}
                label="Connectors"
                value={<>{sub.connectors.configured.length}<small> / {sub.connectors.count} ready</small></>}
                sub={`${sub.connectors.actions} actions exposed across services`}
                onClick={() => navigate("connectors")}
                go
              />
            </>
          )}
        </div>
      </div>

      <div>
        <SectionTitle
          icon={<FolderGit2 size={13} />}
          right={
            <button type="button" className="btn btn--sm btn--ghost" onClick={() => navigate("projects")}>
              All projects <ArrowRight size={13} />
            </button>
          }
        >
          Projects at a glance
        </SectionTitle>
        <div className="glass table-wrap" style={{ marginTop: 14 }}>
          {projects && projects.count > 0 ? (
            <div className="table-scroll">
              <table className="kv">
                <thead>
                  <tr>
                    <th>Project</th>
                    <th>Status</th>
                    <th>Progress</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {projects.projects.slice(0, 4).map((p) => (
                    <GlanceRow key={p.project_id} id={p.project_id} name={p.name} status={p.status} created={p.created_at} />
                  ))}
                </tbody>
              </table>
            </div>
          ) : projects ? (
            <EmptyState
              title="No projects yet"
              hint="Describe what to build on the Projects page and press Create — the planner will break it into tasks."
              action={
                <button type="button" className="btn btn--sm btn--primary" onClick={() => navigate("projects")}>
                  Create a project <ArrowRight size={13} />
                </button>
              }
            />
          ) : (
            <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 10 }}>
              <Skeleton h={16} /> <Skeleton h={16} /> <Skeleton h={16} />
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function GlanceRow({ id, name, status, created }: { id: string; name: string; status: string; created: string }) {
  const { client, openDrawer } = useApp();
  const { data } = useData(
    () => (client ? client.projectStatus(id) : Promise.resolve(null)),
    [client, id],
  );
  const done = data?.tasks.filter((t) => t.status === "done").length ?? 0;
  const total = data?.tasks.length ?? 0;
  return (
    <tr style={{ "--rowtint": "var(--kv-cyan)" } as React.CSSProperties}>
      <td data-label="Project">
        <button
          type="button"
          className="row-title"
          style={{ color: "inherit", textAlign: "left" }}
          onClick={() => openDrawer(id)}
          data-tip="Open the live drawer"
        >
          <span className="grad-text">{name}</span>
        </button>
        <div className="row-sub mono">{id}</div>
      </td>
      <td data-label="Status"><StatusPill status={status} /></td>
      <td data-label="Progress">
        {data ? (
          <div className="prog-cell">
            <Progress value={(done / Math.max(1, total)) * 100} tint={status === "done" ? "var(--kv-ok)" : "var(--kv-cyan)"} label={`${name} progress`} />
            <span>{done}/{total}</span>
          </div>
        ) : (
          <Skeleton w={110} h={8} r={99} />
        )}
      </td>
      <td data-label="Created">{fmtAgo(created)}</td>
    </tr>
  );
}





