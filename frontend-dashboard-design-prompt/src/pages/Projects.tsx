// ── Projects: create form (plan / plan & run), live table ──────────────────

import { useState } from "react";
import { ExternalLink, FileText, Loader2, Play, Sparkles, Wand2 } from "lucide-react";
import { useApp } from "../context";
import { useToast } from "../components/Toasts";
import { useData, usePoll, fmtAgo } from "../lib/hooks";
import { EmptyState, Progress, SectionTitle, Skeleton, StatusPill } from "../components/ui";
import { FolderGit2 } from "lucide-react";

export default function Projects() {
  const { client, refreshKey, openDrawer } = useApp();
  const toast = useToast();
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [agents, setAgents] = useState(3);
  const [busy, setBusy] = useState<"plan" | "run" | null>(null);

  const { data, loading, reload } = useData(
    () => (client ? client.listProjects() : Promise.resolve(null)),
    [client, refreshKey],
  );
  // soft 8s poll keeps statuses fresh without ever flashing skeletons
  usePoll(reload, 8000);

  const create = async (andRun: boolean) => {
    if (!client || busy) return;
    if (!name.trim()) {
      toast.warn("Name the project first", "One short line is enough — the planner does the rest.");
      return;
    }
    setBusy(andRun ? "run" : "plan");
    try {
      const res = await client.createProject({
        name: name.trim(),
        description: desc.trim(),
        n_agents: agents,
      });
      const n = res.plan.tasks.length;
      toast.ok("Project created", `${n} tasks planned for “${name.trim()}”.`);
      setName("");
      setDesc("");
      if (andRun) {
        const run = await client.runProject(res.project.project_id);
        toast.ok("Run dispatched", `${run.tasks_dispatched} tasks queued across the pool.`);
      }
      reload();
      openDrawer(res.project.project_id);
    } catch (e) {
      toast.bad("Create failed", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <section className="card glass card--tint" style={{ "--tint": "var(--kv-lime)" } as React.CSSProperties}>
        <div>
          <div className="card__title">
            <Sparkles size={17} style={{ color: "var(--kv-lime)" }} />
            New project
          </div>
          <p className="card__hint" style={{ marginTop: 6 }}>
            Describe the outcome in plain words. The brain plans the tasks; the pool ships them.
          </p>
        </div>
        <form
          className="create-grid"
          onSubmit={(e) => {
            e.preventDefault();
            void create(false);
          }}
        >
          <label className="label">
            Name
            <input
              className="field"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Realtime billing reconciler"
              maxLength={80}
              id="kv-project-name"
            />
          </label>
          <label className="label">
            Agents
            <input
              className="field num-field"
              type="number"
              min={1}
              max={8}
              value={agents}
              onChange={(e) => setAgents(Math.max(1, Math.min(8, Number(e.target.value) || 1)))}
              aria-label="number of agents"
            />
          </label>
          <label className="label span-2">
            Description
            <textarea
              className="field"
              value={desc}
              onChange={(e) => setDesc(e.target.value)}
              placeholder="What should exist when the run finishes? Constraints, stack, anything that would go in a ticket."
              rows={3}
            />
          </label>
          <div className="create-actions span-2">
            <button
              type="submit"
              className="btn"
              disabled={busy !== null}
              style={{ "--accent": "var(--kv-lime)" } as React.CSSProperties}
            >
              {busy === "plan" ? <Loader2 className="spin-anim" size={15} /> : <Wand2 size={15} />}
              Plan only
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void create(true)}
              disabled={busy !== null}
              style={{ "--accent": "var(--kv-lime)" } as React.CSSProperties}
            >
              {busy === "run" ? <Loader2 className="spin-anim" size={15} /> : <Play size={15} />}
              Plan &amp; run
            </button>
          </div>
        </form>
      </section>

      <div>
        <SectionTitle icon={<FolderGit2 size={13} />}>All projects</SectionTitle>
        <div className="glass table-wrap" style={{ marginTop: 14 }}>
          {loading ? (
            <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 12 }}>
              <Skeleton h={18} /> <Skeleton h={18} /> <Skeleton h={18} /> <Skeleton h={18} />
            </div>
          ) : data && data.count > 0 ? (
            <div className="table-scroll">
              <table className="kv">
                <thead>
                  <tr>
                    <th>Project</th>
                    <th>Status</th>
                    <th>Tasks</th>
                    <th>Created</th>
                    <th style={{ textAlign: "right" }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {data.projects.map((p) => (
                    <ProjectRow
                      key={p.project_id}
                      id={p.project_id}
                      name={p.name}
                      status={p.status}
                      created={p.created_at}
                      onChanged={reload}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              title="No projects yet"
              hint="Describe what to build above and press Create — the planner will break it into parallel tasks."
            />
          )}
        </div>
      </div>
    </>
  );
}

function ProjectRow({
  id,
  name,
  status,
  created,
  onChanged,
}: {
  id: string;
  name: string;
  status: string;
  created: string;
  onChanged: () => void;
}) {
  const { client, openDrawer, refreshKey } = useApp();
  const toast = useToast();
  const [running, setRunning] = useState(false);
  const { data, reload } = useData(
    () => (client ? client.projectStatus(id) : Promise.resolve(null)),
    [client, id, refreshKey],
  );
  // running rows tick faster; finished rows quiet down
  usePoll(reload, 4000, status === "running");

  const done = data?.tasks.filter((t) => t.status === "done").length ?? 0;
  const total = data?.tasks.length ?? 0;

  const run = async () => {
    if (!client || running) return;
    setRunning(true);
    try {
      const res = await client.runProject(id);
      toast.ok(
        "Run dispatched",
        `${res.tasks_dispatched} tasks queued — watch the drawer for live progress.`,
      );
      reload();
      onChanged();
      openDrawer(id);
    } catch (e) {
      toast.bad("Run failed", e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <tr style={{ "--rowtint": "var(--kv-lime)" } as React.CSSProperties}>
      <td data-label="Project">
        <div className="row-title">{name}</div>
        <div className="row-sub mono">{id}</div>
      </td>
      <td data-label="Status">
        <StatusPill status={status} />
      </td>
      <td data-label="Tasks">
        {data ? (
          <div className="prog-cell">
            <Progress
              value={(done / Math.max(1, total)) * 100}
              tint={status === "done" ? "var(--kv-ok)" : "var(--kv-lime)"}
              label={`${name} progress`}
            />
            <span className="num">{done}/{total}</span>
          </div>
        ) : (
          <Skeleton w={110} h={8} r={99} />
        )}
      </td>
      <td data-label="Created">{fmtAgo(created)}</td>
      <td data-label="">
        <div className="row-actions">
          <button type="button" className="btn btn--sm btn--ghost" onClick={() => openDrawer(id)} data-tip="Open the live drawer">
            <ExternalLink size={13} /> Open
          </button>
          <button
            type="button"
            className="btn btn--sm"
            style={{ "--accent": "var(--kv-lime)" } as React.CSSProperties}
            onClick={() => void run()}
            disabled={running}
            data-tip="Dispatch the pool"
          >
            {running ? <Loader2 size={13} className="spin-anim" /> : <Play size={13} />} Run
          </button>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => openDrawer(id, "handoff")}
            data-tip="Resume briefing"
          >
            <FileText size={13} /> Handoff
          </button>
        </div>
      </td>
    </tr>
  );
}
