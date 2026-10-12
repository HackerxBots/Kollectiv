// ── App context: connection, theme, drawer, overlays ───────────────────────

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import {
  cleanBase,
  makeClient,
  ping,
  resolveApiBase,
  saveApiBase,
  type ApiClient,
} from "./lib/api";
import { useToast } from "./components/Toasts";

export type ConnMode = "connecting" | "live" | "demo";
export type Theme = "dark" | "light";
export type DrawerView = "status" | "handoff";

interface AppCtxT {
  client: ApiClient | null;
  mode: ConnMode;
  base: string;
  connect: (base?: string) => Promise<void>;
  refreshKey: number;
  bump: () => void;
  theme: Theme;
  toggleTheme: () => void;
  drawer: { id: string; view: DrawerView } | null;
  openDrawer: (id: string, view?: DrawerView) => void;
  closeDrawer: () => void;
  paletteOpen: boolean;
  setPalette: (v: boolean) => void;
  menuOpen: boolean;
  setMenu: (v: boolean) => void;
  onboardingOpen: boolean;
  openOnboarding: () => void;
  closeOnboarding: () => void;
}

const AppCtx = createContext<AppCtxT | null>(null);

export function useApp(): AppCtxT {
  const ctx = useContext(AppCtx);
  if (!ctx) throw new Error("useApp outside provider");
  return ctx;
}

const LS_THEME = "kollektiv.theme";
const LS_ONBOARDED = "kollektiv.onboarded";

function initialTheme(): Theme {
  try {
    const t = localStorage.getItem(LS_THEME);
    if (t === "light" || t === "dark") return t;
  } catch {
    /* ignore */
  }
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function AppProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [mode, setMode] = useState<ConnMode>("connecting");
  const [base, setBase] = useState<string>(() => resolveApiBase());
  const [client, setClient] = useState<ApiClient | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [drawer, setDrawer] = useState<AppCtxT["drawer"]>(null);
  const [paletteOpen, setPalette] = useState(false);
  const [menuOpen, setMenu] = useState(false);
  const [onboardingOpen, setOnboarding] = useState(false);
  const firstBoot = useRef(true);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem(LS_THEME, theme);
    } catch {
      /* ignore */
    }
  }, [theme]);

  const toggleTheme = useCallback(() => setTheme((t) => (t === "dark" ? "light" : "dark")), []);

  const connect = useCallback(
    async (target?: string) => {
      const b = cleanBase(target ?? resolveApiBase());
      setMode("connecting");
      setBase(b);
      try {
        await ping(b);
        saveApiBase(b);
        setClient(makeClient(b, "live"));
        setMode("live");
        setRefreshKey((k) => k + 1);
        toast.ok("Connected to Kollektiv", b.replace(/^https?:\/\//, ""));
      } catch {
        setClient(makeClient(b, "demo"));
        setMode("demo");
        setRefreshKey((k) => k + 1);
        toast.warn(
          "API unreachable — showing demo data",
          "Actions are simulated. Set the base URL above to connect.",
        );
      }
    },
    [toast],
  );

  // boot: try the resolved base, and show onboarding the first time round
  useEffect(() => {
    if (!firstBoot.current) return;
    firstBoot.current = false;
    void connect();
    try {
      if (!localStorage.getItem(LS_ONBOARDED)) setOnboarding(true);
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const bump = useCallback(() => setRefreshKey((k) => k + 1), []);

  const openDrawer = useCallback((id: string, view: DrawerView = "status") => {
    setDrawer({ id, view });
    setMenu(false);
  }, []);
  const closeDrawer = useCallback(() => setDrawer(null), []);
  const openOnboarding = useCallback(() => setOnboarding(true), []);
  const closeOnboarding = useCallback(() => {
    setOnboarding(false);
    try {
      localStorage.setItem(LS_ONBOARDED, "yes");
    } catch {
      /* ignore */
    }
  }, []);

  const value = useMemo<AppCtxT>(
    () => ({
      client,
      mode,
      base,
      connect,
      refreshKey,
      bump,
      theme,
      toggleTheme,
      drawer,
      openDrawer,
      closeDrawer,
      paletteOpen,
      setPalette,
      menuOpen,
      setMenu,
      onboardingOpen,
      openOnboarding,
      closeOnboarding,
    }),
    [
      client, mode, base, connect, refreshKey, bump, theme, toggleTheme,
      drawer, openDrawer, closeDrawer, paletteOpen, menuOpen,
      onboardingOpen, openOnboarding, closeOnboarding,
    ],
  );

  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}

export function markOnboardedDone() {
  try {
    localStorage.setItem(LS_ONBOARDED, "yes");
  } catch {
    /* ignore */
  }
}
