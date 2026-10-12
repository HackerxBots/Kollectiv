// ── Connectors: readiness, action select, Call (params + confirm), Probe ───

import { useState } from "react";
import {
  BookOpen,
  Bug,
  Container,
  GitBranch,
  Loader2,
  MessageSquare,
  Play,
  Radar,
  Rocket,
  Unplug,
} from "lucide-react";
import { useApp } from "../context";
import { useToast } from "../components/Toasts";
import { ConfirmDialog, ParamsDialog } from "../components/Dialogs";
import { useData, useMedia } from "../lib/hooks";
import { EmptyState, Pill, SectionTitle, Skeleton, Spark } from "../components/ui";
import type { Connector } from "../lib/types";

const SERVICE_ICONS: Record<string, React.ReactNode> = {
  github: <GitBranch size={17} />,
  slack: <MessageSquare size={17} />,
  notion: <BookOpen size={17} />,
  sentry: <Bug size={17} />,
  vercel: <Rocket size={17} />,
  docker: <Container size={17} />,
};

const SERVICE_TINTS: Record<string, string> = {
  github: "var(--kv-peach)",
  slack: "var(--kv-grape)",
  notion: "var(--kv-text-2)",
  sentry: "var(--kv-pink)",
  vercel: "var(--kv-sky)",
  docker: "var(--kv-cyan)",
};

export default function Connectors() {
  const { client, refreshKey } = useApp();
  const { data, loading } = useData(
    () => (client ? client.listConnectors() : Promise.resolve(null)),
    [client, refreshKey],
  );

  return (
    <div>
      <SectionTitle icon={<Unplug size={13} />}>Services Kollektiv can reach</SectionTitle>
      <div className="glass table-wrap" style={{ marginTop: 14 }}>
        {loading ? (
          <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 12 }}>
            <Skeleton h={52} r={14} /> <Skeleton h={52} r={14} /> <Skeleton h={52} r={14} />
          </div>
        ) : data && data.connectors.length > 0 ? (
          <div className="table-scroll">
            <table className="kv">
              <thead>
                <tr>
                  <th>Service</th>
                  <th>Description</th>
                  <th>Readiness</th>
                  <th>Action</th>
                  <th>Probe</th>
                  <th style={{ textAlign: "right" }}>Invoke</th>
                </tr>
              </thead>
              <tbody>
                {data.connectors.map((c) => (
                  <ConnectorRow key={c.name} connector={c} />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title="No connectors yet"
            hint="Define connectors in the server config — they will appear here with their actions."
          />
        )}
      </div>
    </div>
  );
}

function ConnectorRow({ connector: c }: { connector: Connector }) {
  const { client } = useApp();
  const toast = useToast();
  const compact = useMedia("(max-width: 900px)");
  const allActions = [...c.actions, ...c.dangerous_actions];
  const [action, setAction] = useState(allActions[0] ?? "");
  const [paramsOpen, setParamsOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pendingParams, setPendingParams] = useState<Record<string, unknown>>({});
  const [calling, setCalling] = useState(false);
  const [probe, setProbe] = useState<"busy" | { ok: boolean; text: string } | null>(null);

  const isDangerous = c.dangerous_actions.includes(action);

  const invoke = async (params: Record<string, unknown>, confirm: boolean) => {
    if (!client) return;
    setCalling(true);
    try {
      const res = await client.callConnector(c.name, { action, params, confirm });
      if (res.error) {
        toast.bad(`${c.name} · ${action}`, String(res.error));
      } else {
        toast.ok(`${c.name} · ${action}`, String(res.detail ?? "completed"));
      }
    } catch (e) {
      toast.bad(`${c.name} call failed`, e instanceof Error ? e.message : String(e));
    } finally {
      setCalling(false);
    }
  };

  const onCallClick = () => {
    if (!c.configured) {
      toast.warn(`${c.name} not configured`, `Set ${c.detail ?? "its env vars"} to enable it.`);
      return;
    }
    setParamsOpen(true);
  };

  const onParamsSubmit = (params: Record<string, unknown>) => {
    setParamsOpen(false);
    if (isDangerous) {
      setPendingParams(params);
      setConfirmOpen(true);
    } else {
      void invoke(params, false);
    }
  };

  const doProbe = async () => {
    if (!client) return;
    setProbe("busy");
    try {
      const res = await client.probeConnector(c.name);
      setProbe(
        res.ok
          ? { ok: true, text: `${res.seconds.toFixed(2)}s · ok` }
          : { ok: false, text: `${res.seconds.toFixed(2)}s · ${res.error ?? "failed"}` },
      );
    } catch (e) {
      setProbe({ ok: false, text: e instanceof Error ? e.message : "failed" });
    }
  };

  return (
    <tr style={{ "--rowtint": "var(--kv-sky)" } as React.CSSProperties}>
      <td data-label="Service">
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Spark tint={SERVICE_TINTS[c.name] ?? "var(--kv-sky)"}>
            {SERVICE_ICONS[c.name] ?? <Unplug size={17} />}
          </Spark>
          <div>
            <div className="row-title mono" style={{ color: "var(--kv-text)" }}>{c.name}</div>
            <span className="badge-cat" style={{ marginTop: 3, display: "inline-block" }}>{c.category}</span>
          </div>
        </div>
      </td>
      {!compact && (
        <td data-label="Description" style={{ maxWidth: 300 }}>
          <span style={{ fontSize: 12.5 }}>{c.description}</span>
        </td>
      )}
      <td data-label="Readiness">
        {c.configured ? (
          <Pill tone="ok">ready</Pill>
        ) : (
          <Pill tone="warn" tip={`Missing setting: ${c.detail ?? "unknown"}`}>
            missing config
          </Pill>
        )}
      </td>
      <td data-label="Action" style={{ minWidth: 170 }}>
        <select
          className="field field--sm"
          value={action}
          onChange={(e) => setAction(e.target.value)}
          aria-label={`${c.name} action`}
        >
          {c.actions.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
          {c.dangerous_actions.map((a) => (
            <option key={a} value={a}>! {a} (dangerous)</option>
          ))}
        </select>
      </td>
      <td data-label="Probe">
        {probe === "busy" ? (
          <Loader2 size={15} className="spin-anim" style={{ color: "var(--kv-sky)" }} />
        ) : probe ? (
          <span className={`probe-result ${probe.ok ? "ok" : "err"}`} data-tip={probe.ok ? "round trip measured" : probe.text}>
            {probe.text}
          </span>
        ) : (
          <span style={{ color: "var(--kv-text-3)", fontSize: 12 }}>—</span>
        )}
      </td>
      <td data-label="">
        <div className="row-actions">
          <button
            type="button"
            className="btn btn--sm btn--sky"
            onClick={onCallClick}
            disabled={calling}
            data-tip={c.configured ? "Run the selected action" : "Not configured yet"}
          >
            {calling ? <Loader2 size={13} className="spin-anim" /> : <Play size={13} />} Call
          </button>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => void doProbe()}
            disabled={probe === "busy"}
            data-tip="Time one harmless action"
          >
            <Radar size={13} /> Probe
          </button>
        </div>

        <ParamsDialog
          open={paramsOpen}
          title={`${c.name} · ${action}`}
          body={
            isDangerous
              ? "Marked dangerous on the server — parameters first, confirmation after."
              : "Arguments are sent as the action's params object."
          }
          onSubmit={onParamsSubmit}
          onClose={() => setParamsOpen(false)}
        />
        <ConfirmDialog
          open={confirmOpen}
          title="Dangerous action"
          body={`“${action}” can delete data or cancel live work on ${c.name}. The call will be sent with confirm: true.`}
          confirmLabel="Send it"
          onConfirm={() => {
            setConfirmOpen(false);
            void invoke(pendingParams, true);
          }}
          onClose={() => setConfirmOpen(false)}
        />
      </td>
    </tr>
  );
}
