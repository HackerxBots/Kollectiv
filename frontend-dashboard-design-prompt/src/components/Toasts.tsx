// ── Toasts: bottom-right, tone-coloured, auto-dismiss ~5s ──────────────────

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { CheckCircle2, Info, TriangleAlert, XCircle } from "lucide-react";
import type { Tone } from "./ui";

export interface ToastItem {
  id: number;
  tone: Tone;
  title: string;
  msg?: string;
}

interface ToastCtxT {
  push: (tone: Tone, title: string, msg?: string) => void;
  ok: (title: string, msg?: string) => void;
  warn: (title: string, msg?: string) => void;
  bad: (title: string, msg?: string) => void;
  info: (title: string, msg?: string) => void;
}

const ToastCtx = createContext<ToastCtxT | null>(null);

export function useToast(): ToastCtxT {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast outside provider");
  return ctx;
}

const ICONS: Record<Tone, ReactNode> = {
  ok: <CheckCircle2 size={17} />,
  warn: <TriangleAlert size={17} />,
  bad: <XCircle size={17} />,
  info: <Info size={17} />,
  neutral: <Info size={17} />,
};

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const [leaving, setLeaving] = useState<Set<number>>(new Set());

  const dismiss = useCallback((id: number) => {
    // play the exit animation, then actually remove the toast
    setLeaving((s) => new Set(s).add(id));
    window.setTimeout(() => {
      setItems((xs) => xs.filter((t) => t.id !== id));
      setLeaving((s) => {
        const n = new Set(s);
        n.delete(id);
        return n;
      });
    }, 240);
  }, []);

  const push = useCallback(
    (tone: Tone, title: string, msg?: string) => {
      const id = nextId++;
      setItems((xs) => [...xs.slice(-4), { id, tone, title, msg }]);
      window.setTimeout(() => dismiss(id), 5200);
    },
    [dismiss],
  );

  const value = useMemo<ToastCtxT>(
    () => ({
      push,
      ok: (t, m) => push("ok", t, m),
      warn: (t, m) => push("warn", t, m),
      bad: (t, m) => push("bad", t, m),
      info: (t, m) => push("info", t, m),
    }),
    [push],
  );

  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="toasts" aria-live="polite" aria-label="notifications">
        {items.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`toast toast--${t.tone} ${leaving.has(t.id) ? "is-leaving" : ""}`}
            onClick={() => dismiss(t.id)}
            aria-label={`dismiss notification: ${t.title}`}
          >
            <span className="toast__icon">{ICONS[t.tone]}</span>
            <span className="toast__body">
              <span className="toast__title">{t.title}</span>
              {t.msg ? <span className="toast__msg">{t.msg}</span> : null}
            </span>
          </button>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
