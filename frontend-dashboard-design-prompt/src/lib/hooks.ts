// ── Shared hooks & tiny helpers ─────────────────────────────────────────────

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Data fetching with three deliberate behaviours:
 *  1. skeleton only for the first load — later reloads mutate in place, so a
 *     background poll never flashes the page;
 *  2. a failed refresh keeps the last good data on screen and only records
 *     the error;
 *  3. `reload()` manual retry.
 */
export function useData<T>(
  fn: () => Promise<T>,
  deps: readonly unknown[],
): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({
    data: null,
    error: null,
    loading: true,
  });
  const [nonce, setNonce] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const hasData = useRef(false);

  useEffect(() => {
    let alive = true;
    if (!hasData.current) {
      setState((s) => ({ ...s, loading: true, error: null }));
    } else {
      setState((s) => ({ ...s, error: null }));
    }
    fnRef
      .current()
      .then((data) => {
        if (!alive) return;
        hasData.current = true;
        setState({ data, error: null, loading: false });
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setState((s) => ({
          data: s.data,
          error: e instanceof Error ? e.message : String(e),
          loading: false,
        }));
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { ...state, reload };
}

/** Soft background poll — visible-tab only, never flashes loading states. */
export function usePoll(reload: () => void, ms: number, active = true) {
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, ms);
    return () => window.clearInterval(t);
  }, [reload, ms, active]);
}

export type Route = "overview" | "projects" | "connectors" | "agents";
const ROUTES: Route[] = ["overview", "projects", "connectors", "agents"];

function parseHash(): Route {
  const h = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  return (ROUTES as string[]).includes(h) ? (h as Route) : "overview";
}

export function useHashRoute(): [Route, (r: Route) => void] {
  const [route, setRoute] = useState<Route>(parseHash);
  useEffect(() => {
    const on = () => setRoute(parseHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const navigate = useCallback((r: Route) => {
    if (parseHash() !== r) window.location.hash = `/${r}`;
    else setRoute(r);
  }, []);
  return [route, navigate];
}

export function useMedia(query: string): boolean {
  const [m, setM] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setM(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return m;
}

/**
 * Minimal focus trap: keeps Tab cycling among focusable elements of `ref`.
 * Focus is not stolen — callers decide what to focus on open.
 */
export function useFocusTrap<T extends HTMLElement>(active: boolean) {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || !active) return;
    const SELECTOR =
      'a[href], button:not(:disabled), textarea, input, select, [tabindex]:not([tabindex="-1"])';
    const on = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const items = Array.from(el.querySelectorAll<HTMLElement>(SELECTOR)).filter(
        (n) => n.offsetParent !== null,
      );
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (current === first || !el.contains(current))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (current === last || !el.contains(current))) {
        e.preventDefault();
        first.focus();
      }
    };
    el.addEventListener("keydown", on);
    return () => el.removeEventListener("keydown", on);
  }, [active]);
  return ref;
}

/** Card tilt — pointer-driven rotateX/Y + spotlight, reset on leave.
 *  Disabled for coarse pointers and reduced-motion. */
export function useTilt<T extends HTMLElement = HTMLDivElement>(max = 7) {
  const ref = useRef<T | null>(null);
  const frame = useRef(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (window.matchMedia("(pointer: coarse)").matches) return;

    const onMove = (e: PointerEvent) => {
      cancelAnimationFrame(frame.current);
      frame.current = requestAnimationFrame(() => {
        const r = el.getBoundingClientRect();
        const px = (e.clientX - r.left) / r.width - 0.5;
        const py = (e.clientY - r.top) / r.height - 0.5;
        el.style.setProperty("--kv-ry", `${(px * max).toFixed(2)}deg`);
        el.style.setProperty("--kv-rx", `${(-py * max).toFixed(2)}deg`);
        el.style.setProperty("--kv-mx", `${((px + 0.5) * 100).toFixed(1)}%`);
        el.style.setProperty("--kv-my", `${((py + 0.5) * 100).toFixed(1)}%`);
        el.classList.add("is-tilting");
      });
    };
    const onOut = () => {
      cancelAnimationFrame(frame.current);
      el.classList.remove("is-tilting");
      el.style.setProperty("--kv-rx", "0deg");
      el.style.setProperty("--kv-ry", "0deg");
    };
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerout", onOut);
    return () => {
      cancelAnimationFrame(frame.current);
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerout", onOut);
    };
  }, [max]);

  return ref;
}

export function fmtAgo(iso: string): string {
  const s = Math.max(1, Math.round((Date.now() - +new Date(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export function fmtClock(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(
    d.getSeconds(),
  ).padStart(2, "0")}`;
}

export const isMac =
  typeof navigator !== "undefined" && /mac|iphone|ipad/i.test(navigator.platform);

/** Display string for the primary modifier: ⌘ on macOS, Ctrl elsewhere. */
export const MOD = isMac ? "⌘" : "Ctrl";
