// ── Command palette: fuzzy filter over commands and projects ───────────────

import { useEffect, useMemo, useRef, useState } from "react";
import {
  CornerDownLeft,
  FilePlus2,
  FolderGit2,
  Gauge,
  GitFork,
  LayoutDashboard,
  MoonStar,
  RefreshCw,
  Search,
  Sparkles,
  Unplug,
  Bot,
} from "lucide-react";
import { useApp } from "../context";
import { useToast } from "./Toasts";
import { Kbd, StatusPill } from "./ui";
import { useFocusTrap, MOD, type Route } from "../lib/hooks";

interface Item {
  id: string;
  group: "Commands" | "Projects";
  icon: React.ReactNode;
  title: string;
  hint?: string;
  search: string;
  run: () => void;
  right?: React.ReactNode;
}

function fuzzy(query: string, text: string): number | null {
  if (!query) return 1;
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  let ti = 0;
  let score = 0;
  let streak = 0;
  for (const qc of q) {
    const i = t.indexOf(qc, ti);
    if (i === -1) return null;
    streak = i === ti ? streak + 1 : 0;
    score += 1 + streak * 2 + (i === 0 || " /-_.".includes(t[i - 1]) ? 3 : 0);
    ti = i + 1;
  }
  return score - t.length * 0.01;
}

export function Palette({ navigate }: { navigate: (r: Route) => void }) {
  const {
    paletteOpen, setPalette, client, bump, toggleTheme,
    openOnboarding, openDrawer, theme,
  } = useApp();
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const [projects, setProjects] = useState<{ project_id: string; name: string; status: string }[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const trapRef = useFocusTrap<HTMLDivElement>(paletteOpen);

  useEffect(() => {
    if (!paletteOpen) return;
    setQuery("");
    setSel(0);
    window.setTimeout(() => inputRef.current?.focus(), 40);
    if (client) {
      client.listProjects().then((r) => setProjects(r.projects)).catch(() => undefined);
    }
  }, [paletteOpen, client]);

  const close = () => setPalette(false);

  const items = useMemo<Item[]>(() => {
    const nav: Item[] = [
      {
        id: "go-overview", group: "Commands", icon: <LayoutDashboard />,
        title: "Go to Overview", hint: `${MOD}1`, search: "go overview home dashboard",
        run: () => navigate("overview"),
      },
      {
        id: "go-projects", group: "Commands", icon: <FolderGit2 />,
        title: "Go to Projects", hint: `${MOD}2`, search: "go projects",
        run: () => navigate("projects"),
      },
      {
        id: "go-connectors", group: "Commands", icon: <Unplug />,
        title: "Go to Connectors", hint: `${MOD}3`, search: "go connectors services",
        run: () => navigate("connectors"),
      },
      {
        id: "go-agents", group: "Commands", icon: <Bot />,
        title: "Go to Agents & storage", hint: `${MOD}4`, search: "go agents storage pool",
        run: () => navigate("agents"),
      },
      {
        id: "new-project", group: "Commands", icon: <FilePlus2 />,
        title: "Create a new project", search: "create new project plan run",
        run: () => {
          navigate("projects");
          window.setTimeout(() => document.getElementById("kv-project-name")?.focus(), 120);
        },
      },
      {
        id: "theme", group: "Commands", icon: <MoonStar />,
        title: theme === "dark" ? "Switch to light theme" : "Switch to dark theme",
        hint: "t", search: "toggle theme dark light",
        run: toggleTheme,
      },
      {
        id: "refresh", group: "Commands", icon: <RefreshCw />,
        title: "Refresh all data", hint: "r", search: "refresh reload refetch",
        run: () => { bump(); toast.info("Refreshing", "Re-querying every endpoint."); },
      },
      {
        id: "sync", group: "Commands", icon: <GitFork />,
        title: "Sync work to GitHub", search: "github sync push pull",
        run: () => {
          if (!client) return;
          client
            .sync()
            .then((r) => toast.ok("GitHub sync complete", `${r.pushed ?? 0} pushed · ${r.pulled ?? 0} pulled`))
            .catch((e) => toast.bad("Sync failed", e instanceof Error ? e.message : String(e)));
        },
      },
      {
        id: "onboarding", group: "Commands", icon: <Sparkles />,
        title: "Show me around", search: "onboarding tour help welcome",
        run: openOnboarding,
      },
    ];
    const proj: Item[] = projects.map((p) => ({
      id: `p-${p.project_id}`,
      group: "Projects" as const,
      icon: <Gauge />,
      title: p.name,
      search: `${p.name} ${p.project_id} ${p.status}`,
      run: () => openDrawer(p.project_id),
      right: <StatusPill status={p.status} />,
    }));
    return [...nav, ...proj];
  }, [navigate, theme, toggleTheme, bump, toast, client, openOnboarding, openDrawer, projects]);

  const filtered = useMemo(() => {
    const scored = items
      .map((it) => ({ it, s: fuzzy(query, it.search) }))
      .filter((x) => x.s !== null)
      .sort((a, b) => (b.s ?? 0) - (a.s ?? 0));
    return scored.map((x) => x.it);
  }, [items, query]);

  useEffect(() => setSel(0), [query]);
  useEffect(() => {
    const rows = listRef.current?.querySelectorAll(".palette__row");
    rows?.[sel]?.scrollIntoView?.({ block: "nearest" });
  }, [sel]);

  if (!paletteOpen) return null;

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(filtered.length - 1, s + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const it = filtered[sel];
      if (it) { close(); it.run(); }
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  };

  let groupSeen = "";

  return (
    <>
      <div className="scrim is-open" onClick={close} style={{ zIndex: 85 }} />
      <div
        ref={trapRef}
        className="palette glass-strong is-open"
        role="dialog"
        aria-modal="true"
        aria-label="command palette"
        onKeyDown={onKey}
      >
        <div className="palette__field">
          <Search size={17} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Type a command or a project name…"
            aria-label="filter commands"
            spellCheck={false}
          />
          <Kbd>esc</Kbd>
        </div>
        <div className="palette__list" ref={listRef} role="listbox" aria-label="results">
          {filtered.length === 0 ? (
            <div className="palette__empty">Nothing matches “{query}” — commands and project names are searched.</div>
          ) : (
            filtered.map((it, i) => {
              const header =
                it.group !== groupSeen ? ((groupSeen = it.group), (<div key={`g-${it.group}`} className="palette__group">{it.group}</div>)) : null;
              return (
                <div key={it.id}>
                  {header}
                  <button
                    type="button"
                    role="option"
                    aria-selected={i === sel}
                    className={`palette__row ${i === sel ? "is-sel" : ""}`}
                    onMouseEnter={() => setSel(i)}
                    onClick={() => { close(); it.run(); }}
                  >
                    {it.icon}
                    <span>{it.title}</span>
                    {it.right ? <span style={{ marginLeft: "auto" }}>{it.right}</span> : null}
                    {!it.right && it.hint ? <span className="kbd-hint"><Kbd>{it.hint}</Kbd></span> : null}
                    {!it.right && !it.hint ? <CornerDownLeft size={13} style={{ marginLeft: "auto", opacity: i === sel ? 0.8 : 0 }} /> : null}
                  </button>
                </div>
              );
            })
          )}
        </div>
        <div className="palette__foot">
          <span>↑↓ move</span>
          <span>↵ run</span>
          <span>esc close</span>
          <span style={{ marginLeft: "auto" }}>fuzzy over commands + projects</span>
        </div>
      </div>
    </>
  );
}
