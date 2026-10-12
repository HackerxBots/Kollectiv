// ── App chrome: sidebar, topbar, mobile tab bar, footer ────────────────────

import { useEffect, useState } from "react";
import clsx from "clsx";
import {
  ArrowUpRight,
  Bot,
  Command,
  FolderGit2,
  GitBranch,
  LayoutDashboard,
  Link2,
  Loader2,
  Menu,
  Moon,
  RotateCw,
  Scale,
  Server,
  ShieldCheck,
  Sun,
  Unplug,
  X,
} from "lucide-react";
import { useApp } from "../context";
import { shortHost } from "../lib/api";
import { useData, usePoll, MOD, isMac, type Route } from "../lib/hooks";
import { useToast } from "./Toasts";

export const NAV: { route: Route; label: string; short: string; icon: typeof Bot; accent: string; hotkey: string }[] = [
  { route: "overview", label: "Overview", short: "Home", icon: LayoutDashboard, accent: "var(--kv-cyan)", hotkey: `${MOD}1` },
  { route: "projects", label: "Projects", short: "Projects", icon: FolderGit2, accent: "var(--kv-lime)", hotkey: `${MOD}2` },
  { route: "connectors", label: "Connectors", short: "Connectors", icon: Unplug, accent: "var(--kv-sky)", hotkey: `${MOD}3` },
  { route: "agents", label: "Agents & storage", short: "Agents", icon: Bot, accent: "var(--kv-grape)", hotkey: `${MOD}4` },
];

export function useHealth() {
  const { client, refreshKey } = useApp();
  const r = useData(() => (client ? client.health() : Promise.resolve(null)), [client, refreshKey]);
  usePoll(r.reload, 12000);
  return r;
}

export function SysDot({ tone, pulse }: { tone: "ok" | "warn" | "bad" | "info"; pulse?: boolean }) {
  return <span className={clsx("dot", `dot--${tone}`, pulse && "pulse")} aria-hidden />;
}

export function Sidebar({
  route,
  navigate,
}: {
  route: Route;
  navigate: (r: Route) => void;
}) {
  const { mode, openOnboarding, theme, toggleTheme, menuOpen, setMenu } = useApp();
  const { data: health } = useHealth();
  const cx = health?.subsystems.connectors;

  return (
    <aside className={clsx("sidebar glass", menuOpen && "is-open")} aria-label="sidebar">
      <div className="sidebar__brand">
        <a
          className="logo-tile"
          href="#/overview"
          onClick={(e) => {
            e.preventDefault();
            navigate("overview");
          }}
          aria-label="Kollektiv home"
        >
          <img src="/favicon.svg" alt="" width="44" height="44" />
        </a>
        <div>
          <div className="brand-word">Kollektiv</div>
          <div className="brand-sub">mission control</div>
        </div>
        {menuOpen ? (
          <button
            type="button"
            className="icon-btn"
            style={{ marginLeft: "auto" }}
            onClick={() => setMenu(false)}
            aria-label="close menu"
          >
            <X />
          </button>
        ) : null}
      </div>

      <nav className="nav" aria-label="primary">
        {NAV.map((n) => (
          <button
            key={n.route}
            type="button"
            className={clsx("nav-item", route === n.route && "is-active")}
            style={{ "--accent": n.accent } as React.CSSProperties}
            onClick={() => {
              navigate(n.route);
              setMenu(false);
            }}
            aria-current={route === n.route ? "page" : undefined}
            aria-keyshortcuts={
              `${isMac ? "Meta" : "Control"}+${String(NAV.indexOf(n) + 1)}`
            }
            data-tip={`${n.label} · ${n.hotkey}`}
          >
            <n.icon />
            {n.label}
          </button>
        ))}
      </nav>

      <div className="sidebar__footer">
        <div className="sys-row">
          <Server size={13} className="sys-row__icon" aria-hidden />
          <SysDot tone={mode === "live" ? "ok" : mode === "demo" ? "warn" : "info"} pulse={mode === "connecting"} />
          API
          <b>{mode === "live" ? "live" : mode === "demo" ? "demo" : "…"}</b>
        </div>
        <div className="sys-row">
          <Unplug size={13} className="sys-row__icon" aria-hidden />
          <SysDot tone={cx && cx.configured.length > 0 ? "ok" : "warn"} />
          Connectors
          <b>{cx ? `${cx.configured.length}/${cx.count}` : "…"}</b>
        </div>
        <div className="sidebar__actions">
          <button type="button" className="btn btn--sm" onClick={openOnboarding}>
            Show me around
          </button>
          <button
            type="button"
            className="icon-btn"
            style={{ width: 32, height: 32 }}
            onClick={toggleTheme}
            aria-label={theme === "dark" ? "switch to light theme" : "switch to dark theme"}
            data-tip={theme === "dark" ? "Light theme (t)" : "Dark theme (t)"}
          >
            {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
          </button>
        </div>
      </div>
    </aside>
  );
}

export function Topbar({ route }: { route: Route }) {
  const { mode, base, connect, setPalette, setMenu, bump } = useApp();
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [spinning, setSpinning] = useState(false);
  const title = NAV.find((n) => n.route === route)?.label ?? "Overview";

  useEffect(() => {
    if (!url) setUrl(base === "demo" ? "" : base);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, mode]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    void connect(url.trim() ? url.trim() : undefined);
  };

  const refresh = () => {
    bump();
    setSpinning(true);
    window.setTimeout(() => setSpinning(false), 700);
    toast.info("Refreshing", "Re-querying every endpoint.");
  };

  return (
    <header className="topbar glass">
      <button
        type="button"
        className="icon-btn topbar__menu"
        onClick={() => setMenu(true)}
        aria-label="open menu"
      >
        <Menu />
      </button>
      <div className="topbar__title-wrap">
        <h1 className="topbar__title grad-text">{title}</h1>
      </div>
      <span className="host-chip" data-tip={mode === "demo" ? "simulated backend — connect a real API to go live" : base}>
        <SysDot tone={mode === "live" ? "ok" : mode === "demo" ? "warn" : "info"} pulse={mode === "connecting"} />
        <span>{mode === "demo" ? "demo · simulated backend" : shortHost(base)}</span>
      </span>
      <div className="topbar__spacer" />
      <form className="api-form" onSubmit={submit} role="search" aria-label="api connection">
        <input
          className="field field--sm"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="http://localhost:8420"
          aria-label="Kollektiv API base URL"
          inputMode="url"
          spellCheck={false}
        />
        <button
          type="submit"
          className="btn btn--sm"
          disabled={mode === "connecting"}
          data-tip="Point the dashboard at your Kollektiv server"
        >
          {mode === "connecting" ? <Loader2 className="spin-anim" size={14} /> : <Link2 size={14} />}
          Connect
        </button>
      </form>
      <button
        type="button"
        className="icon-btn"
        onClick={refresh}
        aria-label="refresh all data (r)"
        data-tip="Refresh · r"
      >
        <RotateCw style={spinning ? { animation: "kv-spin .7s linear" } : undefined} />
      </button>
      <button
        type="button"
        className="btn btn--sm"
        onClick={() => setPalette(true)}
        aria-label={`open command palette (${MOD}K)`}
        data-tip={`Command palette · ${MOD}K`}
      >
        <Command size={14} />
        <span className="kcap">{MOD}K</span>
      </button>
    </header>
  );
}

export function TabBar({ route, navigate }: { route: Route; navigate: (r: Route) => void }) {
  return (
    <nav className="tabbar glass-strong" aria-label="mobile">
      {NAV.map((n) => (
        <button
          key={n.route}
          type="button"
          className={clsx("tab-item", route === n.route && "is-active")}
          style={{ "--accent": n.accent } as React.CSSProperties}
          onClick={() => navigate(n.route)}
          aria-current={route === n.route ? "page" : undefined}
        >
          <n.icon />
          {n.short}
        </button>
      ))}
    </nav>
  );
}

export function Footer() {
  return (
    <footer className="site-footer">
      <span className="site-footer__item">
        <Scale size={13} aria-hidden /> MIT · Kollektiv project
      </span>
      <a
        href="https://github.com/kollektiv-dev/kollektiv"
        target="_blank"
        rel="noreferrer noopener"
      >
        <GitBranch size={13} aria-hidden /> Source on GitHub <ArrowUpRight size={11} />
      </a>
      <span className="site-footer__item" style={{ marginLeft: "auto" }}>
        <ShieldCheck size={13} aria-hidden /> No telemetry, no analytics, no tracking — this page talks only to your API.
      </span>
    </footer>
  );
}
