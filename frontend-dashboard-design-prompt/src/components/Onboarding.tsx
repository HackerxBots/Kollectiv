// ── Onboarding: three steps, one card, confetti finish ─────────────────────

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Bot,
  CircleX,
  Command,
  FolderGit2,
  GitBranch,
  History,
  LayoutDashboard,
  MoonStar,
  RotateCw,
  ShieldCheck,
  Sparkles,
  Unplug,
} from "lucide-react";
import { useApp, markOnboardedDone } from "../context";
import { useToast } from "./Toasts";
import { Spark } from "./ui";
import { shortHost } from "../lib/api";
import { MOD } from "../lib/hooks";

const STEPS = ["Welcome", "Your API", "Keyboard first"] as const;

export function Onboarding({ onDone }: { onDone?: () => void }) {
  const { onboardingOpen, closeOnboarding, base, mode, connect } = useApp();
  const toast = useToast();
  const [step, setStep] = useState(0);
  const [url, setUrl] = useState("");
  const [confetti, setConfetti] = useState<{ tx: number; ty: number; rot: number; h: number }[] | null>(null);

  useEffect(() => {
    if (onboardingOpen) {
      setStep(0);
      setUrl(mode === "demo" ? "" : base);
    }
  }, [onboardingOpen, base, mode]);

  const finish = useCallback(() => {
    closeOnboarding();
    onDone?.();
    if (url.trim() && url.trim() !== base) void connect(url.trim());
    setConfetti(
      Array.from({ length: 34 }, (_, i) => {
        const angle = (i / 34) * Math.PI * 2 + Math.random() * 0.5;
        const dist = 130 + Math.random() * 190;
        return {
          tx: Math.cos(angle) * dist,
          ty: Math.sin(angle) * dist * 0.8 + 90,
          rot: Math.random() * 720 - 360,
          h: [265, 190, 150, 80, 330, 45][i % 6],
        };
      }),
    );
    window.setTimeout(() => setConfetti(null), 1100);
    toast.ok("You are set", "Next: create your first project and dispatch a run.");
  }, [closeOnboarding, onDone, url, base, connect, toast]);

  const next = useCallback(() => {
    if (step < STEPS.length - 1) setStep((s) => s + 1);
    else finish();
  }, [step, finish]);

  const skip = useCallback(() => {
    markOnboardedDone();
    closeOnboarding();
    onDone?.();
  }, [closeOnboarding, onDone]);

  useEffect(() => {
    if (!onboardingOpen) return;
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        skip();
      } else if (e.key === "Enter" && (e.target as HTMLElement)?.tagName !== "TEXTAREA") {
        const btn = (e.target as HTMLElement)?.closest("button");
        if (!btn) {
          e.preventDefault();
          next();
        }
      }
    };
    window.addEventListener("keydown", on, true);
    return () => window.removeEventListener("keydown", on, true);
  }, [onboardingOpen, next, skip]);

  const keys = useMemo(
    () => [
      { icon: <Command size={13} />, k: `${MOD} K`, d: "command palette" },
      { icon: <LayoutDashboard size={13} />, k: `${MOD} 1`, d: "overview" },
      { icon: <FolderGit2 size={13} />, k: `${MOD} 2`, d: "projects" },
      { icon: <Unplug size={13} />, k: `${MOD} 3`, d: "connectors" },
      { icon: <Bot size={13} />, k: `${MOD} 4`, d: "agents & storage" },
      { icon: <MoonStar size={13} />, k: "T", d: "toggle theme" },
      { icon: <RotateCw size={13} />, k: "R", d: "refresh all data" },
      { icon: <CircleX size={13} />, k: "Esc", d: "close drawer & dialogs" },
    ],
    [],
  );

  if (!onboardingOpen && !confetti) return null;

  return (
    <>
      {confetti ? (
        <div className="confetti-wrap" aria-hidden>
          {confetti.map((c, i) => (
            <span
              key={i}
              className="confetti"
              style={{
                "--tx": `${c.tx.toFixed(0)}px`,
                "--ty": `${c.ty.toFixed(0)}px`,
                "--rot": `${c.rot.toFixed(0)}deg`,
                "--h": c.h,
              } as React.CSSProperties}
            />
          ))}
        </div>
      ) : null}

      {onboardingOpen ? (
        <div className="onboard-scrim" role="presentation" onClick={skip}>
          <div
            className="onboard glass-strong"
            role="dialog"
            aria-modal="true"
            aria-label={`onboarding step ${step + 1} of ${STEPS.length}: ${STEPS[step]}`}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="onboard-tile" aria-hidden />

            {step === 0 && (
              <div className="onboard__step" key="s0">
                <h2 className="onboard__title">
                  <span className="grad-text">Your dev team, orchestrated.</span>
                </h2>
                <p className="onboard__text">
                  Kollektiv turns a description into a plan, and a plan into shipped work —
                  by agents you host, on infrastructure you own.
                </p>
                <div className="onboard__bullets">
                  <div className="onboard-bullet" style={{ "--tint": "var(--kv-cyan)" } as React.CSSProperties}>
                    <Spark tint="var(--kv-cyan)"><GitBranch /></Spark>
                    <div><b>Plan, then work in parallel</b><span>The brain splits a project into tasks; the pool executes them side by side.</span></div>
                  </div>
                  <div className="onboard-bullet" style={{ "--tint": "var(--kv-lime)" } as React.CSSProperties}>
                    <Spark tint="var(--kv-lime)"><History /></Spark>
                    <div><b>Nothing lost between sessions</b><span>State lives in object storage. Close the tab, resume with a handoff briefing.</span></div>
                  </div>
                  <div className="onboard-bullet" style={{ "--tint": "var(--kv-sky)" } as React.CSSProperties}>
                    <Spark tint="var(--kv-sky)"><Sparkles /></Spark>
                    <div><b>Your tools stay yours</b><span>Connectors call the services you already use — with your keys, from your server.</span></div>
                  </div>
                </div>
              </div>
            )}

            {step === 1 && (
              <div className="onboard__step" key="s1" style={{ width: "100%" }}>
                <h2 className="onboard__title">
                  <span className="grad-text">Point it at your API.</span>
                </h2>
                <p className="onboard__text">
                  The only requests this page makes go to the address below — nothing else,
                  ever. Leave it empty to keep exploring the demo backend.
                </p>
                <label className="label" style={{ width: "100%", textAlign: "left" }}>
                  Kollektiv API base URL
                  <input
                    className="field"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="http://localhost:8420"
                    inputMode="url"
                    spellCheck={false}
                    aria-label="Kollektiv API base URL"
                  />
                </label>
                <p className="card__hint" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <ShieldCheck size={14} style={{ color: "var(--kv-ok)", flex: "none" }} />
                  {mode === "demo"
                    ? "Right now you are looking at the simulated backend — connect to go live."
                    : "Currently connected to " + shortHost(base) + "."}
                </p>
              </div>
            )}

            {step === 2 && (
              <div className="onboard__step" key="s2" style={{ width: "100%" }}>
                <h2 className="onboard__title">
                  <span className="grad-text">Keyboard first.</span>
                </h2>
                <p className="onboard__text">Everything on this panel is reachable without the mouse.</p>
                <div className="keys-grid">
                  {keys.map((x) => (
                    <div key={x.k} className="key-row">
                      <span className="key-row__label">
                        <span className="key-row__icon" aria-hidden>{x.icon}</span>
                        {x.d}
                      </span>
                      <kbd className="kcap">{x.k}</kbd>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="onboard__footer">
              <button type="button" className="btn btn--sm btn--ghost" onClick={skip}>
                Skip
              </button>
              <div className="dots" aria-hidden>
                {STEPS.map((_, i) => (
                  <i key={i} className={i === step ? "is-active" : ""} />
                ))}
              </div>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => setStep((s) => Math.max(0, s - 1))}
                disabled={step === 0}
                aria-label="back"
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button type="button" className="btn btn--sm btn--primary" onClick={next} autoFocus>
                {step === STEPS.length - 1 ? "Start building" : "Next"} <ArrowRight size={14} />
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
