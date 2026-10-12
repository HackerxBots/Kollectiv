// ── Small shared primitives ─────────────────────────────────────────────────

import type { ReactNode } from "react";
import clsx from "clsx";

export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

export function SectionTitle({
  children,
  right,
  icon,
}: {
  children: ReactNode;
  right?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="section-head">
      <h2 className="section-title">
        {icon ? <span className="section-title__icon">{icon}</span> : null}
        {children}
      </h2>
      {right}
    </div>
  );
}

export function Pill({
  tone = "neutral",
  children,
  tip,
  className,
}: {
  tone?: Tone;
  children: ReactNode;
  tip?: string;
  className?: string;
}) {
  return (
    <span
      className={clsx("pill", `pill--${tone}`, className)}
      {...(tip ? { "data-tip": tip, tabIndex: 0 } : {})}
    >
      <span className="pill__dot" aria-hidden />
      {children}
    </span>
  );
}

const STATUS_TONE: Record<string, { tone: Tone; label: string }> = {
  done: { tone: "ok", label: "done" },
  running: { tone: "info", label: "running" },
  in_progress: { tone: "info", label: "in progress" },
  queued: { tone: "warn", label: "queued" },
  planned: { tone: "info", label: "planned" },
  pending: { tone: "neutral", label: "pending" },
  failed: { tone: "bad", label: "failed" },
  planned_only: { tone: "neutral", label: "planned" },
};

export function StatusPill({ status }: { status: string }) {
  const s = STATUS_TONE[status] ?? { tone: "neutral" as Tone, label: status.replace(/_/g, " ") };
  return (
    <Pill tone={s.tone} className={status === "running" || status === "in_progress" ? "is-live" : undefined}>
      {s.label}
    </Pill>
  );
}

export function Progress({
  value,
  tint,
  label,
}: {
  value: number;
  tint?: string;
  label?: string;
}) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div
      className="prog"
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label ?? "progress"}
      style={tint ? ({ "--tint": tint } as React.CSSProperties) : undefined}
    >
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kcap">{children}</kbd>;
}

export function Spark({
  tint,
  children,
}: {
  tint: string;
  children: ReactNode;
}) {
  return (
    <span className="spark" style={{ "--tint": tint } as React.CSSProperties}>
      {children}
    </span>
  );
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-tile" aria-hidden />
      <div className="empty-title">{title}</div>
      <p className="empty-hint">{hint}</p>
      {action}
    </div>
  );
}

export function Skeleton({ w, h = 14, r = 10 }: { w?: string | number; h?: number; r?: number }) {
  return (
    <span
      className="skeleton"
      style={{ width: w ?? "100%", height: h, borderRadius: r }}
      aria-hidden
    />
  );
}
