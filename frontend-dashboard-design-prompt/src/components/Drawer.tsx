// ── Project drawer: live SSE state, tasks, artifacts, history, handoff ─────

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  Copy,
  FileText,
  ListChecks,
  Loader2,
  Package,
  Play,
  Radio,
  RefreshCw,
  X,
} from "lucide-react";
import { useApp, type DrawerView } from "../context";
import { useToast } from "./Toasts";
import { fmtClock, useFocusTrap } from "../lib/hooks";
import { Progress, StatusPill } from "./ui";
import type { Handoff, ProjectStatus } from "../lib/types";
import { SysDot } from "./Chrome";

export function ProjectDrawer() {
  const { client, drawer, closeDrawer } = useApp();
  const toast = useToast();
  const [data, setData] = useState<ProjectStatus | null>(null);
  const [streamOk, setStreamOk] = useState<"connecting" | "live" | "down">("connecting");
  const [view, setView] = useState<DrawerView>("status");
  const [handoff, setHandoff] = useState<Handoff | null>(null);
  const [handoffBusy, setHandoffBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState<"run" | "replan" | null>(null);
  const prevHistory = useRef<Set<string>>(new Set());
  const closeRef = useRef<HTMLButtonElement>(null);
  const id = drawer?.id ?? null;
  const open = drawer !== null;
  const trapRef = useFocusTrap<HTMLElement>(open);

  // move focus into the drawer when it opens
  useEffect(() => {
    if (open) {
      const previous = document.activeElement as HTMLElement | null;
      window.setTimeout(() => closeRef.current?.focus(), 80);
      return () => previous?.focus?.();
    }
  }, [open]);

  useEffect(() => {
    if (drawer) setView(drawer.view);
  }, [drawer]);

  // subscribe to the SSE stream while open
  useEffect(() => {
    if (!client || !id) return;
    setData(null);
    setHandoff(null);
    setStreamOk("connecting");
    prevHistory.current = new Set();
    let alive = true;
    client
      .projectStatus(id)
      .then((s) => alive && setData(s))
      .catch(() => undefined);
    const unsub = client.stream(
      id,
      (s) => {
        if (!alive) return;
        setData(s);
        setStreamOk("live");
      },
      () => alive && setStreamOk("down"),
    );
    return () => {
      alive = false;
      unsub();
    };
  }, [client, id]);

  // esc closes
  useEffect(() => {
    if (!open) return;
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closeDrawer();
      }
    };
    window.addEventListener("keydown", on, true);
    return () => window.removeEventListener("keydown", on, true);
  }, [open, closeDrawer]);

  const done = data?.tasks.filter((t) => t.status === "done").length ?? 0;
  const inProgress = data?.tasks.filter((t) => t.status === "in_progress").length ?? 0;
  const failed = data?.tasks.filter((t) => t.status === "failed").length ?? 0;
  const waiting =
    (data?.tasks.filter((t) => t.status === "pending" || t.status === "queued").length ?? 0);
  const total = data?.tasks.length ?? 0;

  const loadHandoff = useCallback(async () => {
    if (!client || !id) return;
    setHandoffBusy(true);
    try {
      setHandoff(await client.handoff(id));
      setView("handoff");
    } catch (e) {
      toast.bad("Handoff failed", e instanceof Error ? e.message : String(e));
    } finally {
      setHandoffBusy(false);
    }
  }, [client, id, toast]);

  useEffect(() => {
    if (view === "handoff" && !handoff && !handoffBusy && client && id) void loadHandoff();
  }, [view, handoff, handoffBusy, client, id, loadHandoff]);

  const run = async () => {
    if (!client || !id) return;
    setActionBusy("run");
    try {
      const res = await client.runProject(id);
      toast.ok("Run dispatched", `${res.tasks_dispatched} tasks queued.`);
    } catch (e) {
      toast.bad("Run failed", e instanceof Error ? e.message : String(e));
    } finally {
      setActionBusy(null);
    }
  };

  const replan = async () => {
    if (!client || !id) return;
    setActionBusy("replan");
    try {
      const res = await client.replanProject(id);
      toast.ok(`Re-plan revision ${res.revision}`, `${res.new_tasks.length} new tasks added.`);
    } catch (e) {
      toast.bad("Re-plan failed", e instanceof Error ? e.message : String(e));
    } finally {
      setActionBusy(null);
    }
  };

  const copyFile = async (path: string) => {
    if (!client || !id) return;
    try {
      const { url } = await client.fileUrl(id, path);
      await navigator.clipboard.writeText(url);
      toast.ok("Download URL copied", path);
    } catch {
      toast.bad("Copy failed", "The server did not return a URL for this file.");
    }
  };

  // mark freshly-arrived history rows for the entrance animation
  const history = data?.history ?? [];
  const seen = prevHistory.current;
  const freshKeys = new Set<string>();
  if (seen.size > 0) {
    for (const h of history) {
      const k = `${h.at}${h.event}`;
      if (!seen.has(k)) freshKeys.add(k);
    }
  }
  seen.clear();
  for (const h of history) seen.add(`${h.at}${h.event}`);

  return (
    <>
      <div
        className={`scrim ${open ? "is-open" : ""}`}
        onClick={closeDrawer}
        aria-hidden={!open}
      />
      <aside
        ref={trapRef}
        className={`drawer glass-strong ${open ? "is-open" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={data?.project_name ?? "project"}
        aria-hidden={!open}
      >
        <div className="drawer__head">
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="drawer__title">{data?.project_name ?? "Loading…"}</div>
            <div className="drawer__sub">
              <button
                type="button"
                className="file-chip"
                onClick={() => {
                  if (!id) return;
                  void navigator.clipboard
                    .writeText(id)
                    .then(() => toast.ok("Project id copied", id))
                    .catch(() => toast.bad("Copy failed", "Clipboard is unavailable."));
                }}
                data-tip="Copy project id"
                aria-label={id ? `copy project id ${id}` : "copy project id"}
              >
                <Copy /> {id}
              </button>
              {data ? <StatusPill status={data.status} /> : null}
              <span className="stream-dot">
                <SysDot
                  tone={streamOk === "live" ? "ok" : streamOk === "down" ? "bad" : "info"}
                  pulse={streamOk === "live"}
                />
                {streamOk === "live" ? "stream live" : streamOk === "down" ? "stream lost" : "connecting"}
              </span>
            </div>
          </div>
          <button
            type="button"
            className="icon-btn"
            ref={closeRef}
            onClick={closeDrawer}
            aria-label="close drawer (esc)"
          >
            <X />
          </button>
        </div>

        <div className="drawer__body">
          {view === "handoff" ? (
            handoff ? (
              <HandoffView handoff={handoff} />
            ) : (
              <div style={{ display: "flex", justifyContent: "center", padding: 60 }}>
                <Loader2 className="spin-anim" style={{ color: "var(--kv-grape)" }} />
              </div>
            )
          ) : (
            <>
              {data?.description ? (
                <p className="drawer__meta">{data.description}</p>
              ) : (
                <p className="drawer__meta" style={{ color: "var(--kv-text-3)" }}>
                  {data ? "No description recorded for this project." : "Rehydrating state…"}
                </p>
              )}

              <div>
                <div className="drawer__sub-title" style={{ marginBottom: 8 }}>
                  <Activity size={12} /> Progress
                </div>
                <div className="prog-cell" style={{ width: "100%" }}>
                  <Progress
                    value={(done / Math.max(1, total)) * 100}
                    tint={data?.status === "done" ? "var(--kv-ok)" : "var(--kv-grape)"}
                    label="project progress"
                  />
                  <span className="num">{done}/{total}</span>
                </div>
              </div>

              <div className="mini-stats">
                <div className="mini-stat mini-stat--ok"><b>{done}</b><span>done</span></div>
                <div className="mini-stat mini-stat--info"><b>{inProgress}</b><span>in flight</span></div>
                <div className="mini-stat mini-stat--warn"><b>{waiting}</b><span>waiting</span></div>
                <div className="mini-stat mini-stat--bad"><b>{failed}</b><span>failed</span></div>
              </div>

              <div>
                <div className="drawer__sub-title" style={{ marginBottom: 8 }}>
                  <ListChecks size={12} /> Tasks
                  {data?.last_commit ? (
                    <span className="mono" style={{ marginLeft: "auto", fontSize: 11 }}>
                      last commit {data.last_commit}
                    </span>
                  ) : null}
                </div>
                <div className="table-scroll" style={{
                  border: "1px solid var(--kv-stroke)",
                  borderRadius: "var(--kv-radius-sm)",
                  background: "color-mix(in srgb, var(--kv-fill) 55%, transparent)",
                }}>
                  <table className="kv">
                    <thead>
                      <tr>
                        <th>Task</th>
                        <th>Agent</th>
                        <th>Status</th>
                        <th>Score</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(data?.tasks ?? []).map((t) => (
                        <tr key={t.id} style={{ "--rowtint": "var(--kv-grape)" } as React.CSSProperties}>
                          <td data-label="Task" style={{ maxWidth: 240 }}>
                            <div style={{ fontSize: 12.5, color: "var(--kv-text)" }}>{t.title}</div>
                            <div className="mono" style={{ fontSize: 10.5, color: "var(--kv-text-3)" }}>{t.id}</div>
                            {t.error ? (
                              <div className="mono" style={{ fontSize: 10.5, color: "var(--kv-bad)", marginTop: 3 }}>{t.error}</div>
                            ) : null}
                          </td>
                          <td data-label="Agent">
                            {t.assigned_agent ? (
                              <span className="mono" style={{ fontSize: 11.5 }}>{t.assigned_agent}</span>
                            ) : (
                              <span style={{ color: "var(--kv-text-3)", fontSize: 12 }}>—</span>
                            )}
                          </td>
                          <td data-label="Status"><StatusPill status={t.status} /></td>
                          <td data-label="Score">
                            {t.score != null ? (
                              <span className="num" style={{ color: t.score >= 80 ? "var(--kv-ok)" : t.score >= 60 ? "var(--kv-warn)" : "var(--kv-bad)" }}>
                                {t.score}
                              </span>
                            ) : (
                              <span style={{ color: "var(--kv-text-3)" }}>—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              <div>
                <div className="drawer__sub-title" style={{ marginBottom: 8 }}>
                  <Package size={12} /> Artifacts
                </div>
                {data && data.files.length > 0 ? (
                  <div className="file-chips">
                    {data.files.map((f) => (
                      <button
                        key={f}
                        type="button"
                        className="file-chip"
                        onClick={() => void copyFile(f)}
                        data-tip="Copy a presigned download URL"
                      >
                        <Copy /> {f}
                      </button>
                    ))}
                  </div>
                ) : (
                  <p style={{ fontSize: 12.5, color: "var(--kv-text-3)" }}>
                    Nothing written yet — artifacts land here as tasks finish.
                  </p>
                )}
              </div>

              <div>
                <div className="drawer__sub-title" style={{ marginBottom: 8 }}>
                  <Radio size={12} /> Event history
                </div>
                <div className="log" aria-live="polite">
                  {history.length > 0 ? (
                    history.map((h) => {
                      const k = `${h.at}${h.event}`;
                      return (
                        <div key={k} className={`log__row ${freshKeys.has(k) ? "kv-new" : ""}`}>
                          <span className="log__time">{fmtClock(h.at)}</span>
                          <span>
                            {h.event}
                            {h.detail ? <span className="log__detail">{h.detail}</span> : null}
                          </span>
                        </div>
                      );
                    })
                  ) : (
                    <div className="log__row" style={{ color: "var(--kv-text-3)" }}>No events recorded yet.</div>
                  )}
                </div>
              </div>
            </>
          )}
        </div>

        <div className="drawer__actions">
          <button
            type="button"
            className="btn btn--sm btn--primary"
            style={{ "--accent": "var(--kv-grape)" } as React.CSSProperties}
            onClick={() => void run()}
            disabled={actionBusy !== null}
          >
            {actionBusy === "run" ? <Loader2 size={13} className="spin-anim" /> : <Play size={13} />} Run
          </button>
          <button
            type="button"
            className="btn btn--sm"
            style={{ "--accent": "var(--kv-pink)" } as React.CSSProperties}
            onClick={() => void replan()}
            disabled={actionBusy !== null}
          >
            {actionBusy === "replan" ? <Loader2 size={13} className="spin-anim" /> : <RefreshCw size={13} />} Re-plan
          </button>
          {view === "handoff" ? (
            <button type="button" className="btn btn--sm btn--ghost" onClick={() => setView("status")}>
              <X size={13} /> Back to state
            </button>
          ) : (
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => void loadHandoff()}
              disabled={handoffBusy}
            >
              {handoffBusy ? <Loader2 size={13} className="spin-anim" /> : <FileText size={13} />} Handoff
            </button>
          )}
        </div>
      </aside>
    </>
  );
}

// ── tiny markdown renderer for the handoff briefing ─────────────────────────
function HandoffView({ handoff }: { handoff: Handoff }) {
  const inline = (text: string) => {
    const parts: React.ReactNode[] = [];
    let rest = text;
    let i = 0;
    while (rest.length) {
      const code = rest.match(/`([^`]+)`/);
      const bold = rest.match(/\*\*([^*]+)\*\*/);
      let idx = -1;
      let kind: "code" | "bold" | null = null;
      if (code?.index != null && (idx === -1 || code.index < idx)) { idx = code.index; kind = "code"; }
      if (bold?.index != null && (idx === -1 || bold.index < idx)) { idx = bold.index; kind = "bold"; }
      if (idx === -1 || kind === null) { parts.push(rest); break; }
      if (idx > 0) parts.push(rest.slice(0, idx));
      const m = (kind === "code" ? code : bold)!;
      parts.push(
        kind === "code" ? <code key={i++}>{m[1]}</code> : <strong key={i++}>{m[1]}</strong>,
      );
      rest = rest.slice(idx + m[0].length);
    }
    return parts;
  };

  const lines = handoff.markdown.split("\n");
  const nodes: React.ReactNode[] = [];
  let ul: string[] = [];
  const flush = (key: string) => {
    if (ul.length) {
      nodes.push(
        <ul key={key}>{ul.map((li, j) => <li key={j}>{inline(li)}</li>)}</ul>,
      );
      ul = [];
    }
  };
  lines.forEach((line, i) => {
    const t = line.trim();
    if (t.startsWith("### ")) { flush(`u${i}`); nodes.push(<h3 key={i}>{inline(t.slice(4))}</h3>); }
    else if (t.startsWith("## ")) { flush(`u${i}`); nodes.push(<h2 key={i}>{inline(t.slice(3))}</h2>); }
    else if (t.startsWith("- ")) { ul.push(t.slice(2)); }
    else if (t.length) { flush(`u${i}`); nodes.push(<p key={i}>{inline(t)}</p>); }
  });
  flush("end");

  return (
    <div className="md">
      <div className="prog-cell" style={{ marginBottom: 14 }}>
        <Progress value={handoff.progress} tint="var(--kv-cyan)" label="handoff progress" />
        <span className="num">{handoff.progress}%</span>
      </div>
      {nodes}
    </div>
  );
}
