// ── Kollektiv mission control — app root ───────────────────────────────────

import { useEffect } from "react";
import { AppProvider, useApp } from "./context";
import { ToastProvider, useToast } from "./components/Toasts";
import { Sidebar, Topbar, TabBar, Footer } from "./components/Chrome";
import { ProjectDrawer } from "./components/Drawer";
import { Palette } from "./components/Palette";
import { Onboarding } from "./components/Onboarding";
import { useHashRoute } from "./lib/hooks";
import Overview from "./pages/Overview";
import Projects from "./pages/Projects";
import Connectors from "./pages/Connectors";
import Agents from "./pages/Agents";

function isTypingTarget(e: KeyboardEvent): boolean {
  const el = e.target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

function Shell() {
  const [route, navigate] = useHashRoute();
  const { paletteOpen, setPalette, toggleTheme, bump, menuOpen, setMenu } = useApp();
  const toast = useToast();

  // global keyboard:
  //   ⌘K / Ctrl-K  command palette        ⌘1–4 / Ctrl-1–4  section navigation
  //   t            toggle theme           r                refresh all data
  //   esc          close menus & overlays
  useEffect(() => {
    const NAV_KEYS: Record<string, Parameters<typeof navigate>[0]> = {
      "1": "overview",
      "2": "projects",
      "3": "connectors",
      "4": "agents",
    };
    const on = (e: KeyboardEvent) => {
      const chord = e.metaKey || e.ctrlKey;
      if (chord && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette(!paletteOpen);
        return;
      }
      if (chord && !e.shiftKey && !e.altKey && NAV_KEYS[e.key]) {
        e.preventDefault();
        navigate(NAV_KEYS[e.key]);
        return;
      }
      if (isTypingTarget(e)) return;
      if (chord || e.altKey) return;

      const k = e.key.toLowerCase();
      if (k === "t") {
        toggleTheme();
      } else if (k === "r") {
        bump();
        toast.info("Refreshing", "Re-querying every endpoint.");
      } else if (k === "escape" && menuOpen) {
        setMenu(false);
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [paletteOpen, setPalette, toggleTheme, bump, navigate, toast, menuOpen, setMenu]);

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <div className="aurora" aria-hidden />
      <div className="aurora aurora--b" aria-hidden />
      <div className="grain" aria-hidden />

      <div className="app">
        <Sidebar route={route} navigate={navigate} />
        <div className="main">
          <Topbar route={route} />
          <main className="content" id="main" tabIndex={-1} key={route} aria-label={route}>
            {route === "overview" && <Overview navigate={navigate} />}
            {route === "projects" && <Projects />}
            {route === "connectors" && <Connectors />}
            {route === "agents" && <Agents />}
            <Footer />
          </main>
        </div>
      </div>

      <TabBar route={route} navigate={navigate} />
      <ProjectDrawer />
      <Palette navigate={navigate} />
      <Onboarding />
    </>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <AppProvider>
        <Shell />
      </AppProvider>
    </ToastProvider>
  );
}
