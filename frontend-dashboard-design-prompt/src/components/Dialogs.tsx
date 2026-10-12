// ── Modal dialogs: params editor + dangerous-action confirm ───────────────

import { useEffect, useRef, useState } from "react";
import { ShieldAlert, Terminal } from "lucide-react";
import { useFocusTrap } from "../lib/hooks";

export function ParamsDialog({
  open,
  title,
  body,
  initial = "{}",
  submitLabel = "Call",
  onSubmit,
  onClose,
}: {
  open: boolean;
  title: string;
  body?: string;
  initial?: string;
  submitLabel?: string;
  onSubmit: (params: Record<string, unknown>) => void;
  onClose: () => void;
}) {
  const [text, setText] = useState(initial);
  const ref = useRef<HTMLTextAreaElement>(null);
  const trapRef = useFocusTrap<HTMLDivElement>(open);

  useEffect(() => {
    if (open) {
      setText(initial);
      window.setTimeout(() => ref.current?.focus(), 60);
    }
  }, [open, initial]);
  useEscape(open, onClose);

  if (!open) return null;
  const submit = () => {
    let parsed: unknown = {};
    try {
      parsed = text.trim() ? JSON.parse(text) : {};
    } catch {
      ref.current?.focus();
      return;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      ref.current?.focus();
      return;
    }
    onSubmit(parsed as Record<string, unknown>);
  };

  return (
    <>
      <div className="scrim is-open" onClick={onClose} style={{ zIndex: 98 }} />
      <div
        ref={trapRef}
        className="dialog glass-strong"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="dialog__title">
          <Terminal style={{ color: "var(--kv-cyan)" }} /> {title}
        </div>
        {body ? <div className="dialog__body">{body}</div> : null}
        <label className="label">
          params · JSON
          <textarea
            ref={ref}
            className="field mono"
            style={{ fontSize: 12.5, minHeight: 110, color: "var(--kv-text-2)" }}
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
              e.stopPropagation();
            }}
          />
        </label>
        <div className="dialog__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn btn--primary" onClick={submit}>
            {submitLabel}
          </button>
        </div>
      </div>
    </>
  );
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "Go ahead",
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  body: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  useEscape(open, onClose);
  const trapRef = useFocusTrap<HTMLDivElement>(open);
  if (!open) return null;
  return (
    <>
      <div className="scrim is-open" onClick={onClose} style={{ zIndex: 98 }} />
      <div
        ref={trapRef}
        className="dialog glass-strong"
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="dialog__title" style={{ color: "var(--kv-bad)" }}>
          <ShieldAlert /> {title}
        </div>
        <div className="dialog__body">{body}</div>
        <div className="dialog__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose} autoFocus>
            Cancel
          </button>
          <button type="button" className="btn btn--danger" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </>
  );
}

function useEscape(open: boolean, onClose: () => void) {
  useEffect(() => {
    if (!open) return;
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", on, true);
    return () => window.removeEventListener("keydown", on, true);
  }, [open, onClose]);
}
