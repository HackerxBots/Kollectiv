// ── Kollektiv API client ────────────────────────────────────────────────────
// Base URL precedence: ?api= query param → localStorage → same origin.
// Optional auth: Authorization: Bearer <localStorage "kollektiv.token">.

import type {
  AgentsResponse,
  CallBody,
  CallResponse,
  ConnectorsResponse,
  CreateProjectBody,
  CreateProjectResponse,
  Handoff,
  Health,
  ProbeResponse,
  ProjectStatus,
  ProjectsResponse,
  ReplanResponse,
  RunResponse,
  StorageStatus,
  SyncResponse,
} from "./types";
import { createMockClient } from "./mock";

export interface ApiClient {
  mode: "live" | "demo";
  base: string;
  health(): Promise<Health>;
  listProjects(): Promise<ProjectsResponse>;
  createProject(body: CreateProjectBody): Promise<CreateProjectResponse>;
  runProject(id: string): Promise<RunResponse>;
  replanProject(id: string): Promise<ReplanResponse>;
  projectStatus(id: string): Promise<ProjectStatus>;
  handoff(id: string): Promise<Handoff>;
  fileUrl(id: string, path: string): Promise<{ url: string }>;
  listConnectors(): Promise<ConnectorsResponse>;
  callConnector(name: string, body: CallBody): Promise<CallResponse>;
  probeConnector(name: string): Promise<ProbeResponse>;
  agentsStatus(): Promise<AgentsResponse>;
  storageStatus(): Promise<StorageStatus>;
  sync(): Promise<SyncResponse>;
  /** Subscribe to the SSE state stream; returns an unsubscribe function. */
  stream(
    id: string,
    onState: (s: ProjectStatus) => void,
    onError?: () => void,
  ): () => void;
}

const LS_API = "kollektiv.api";
const LS_TOKEN = "kollektiv.token";

/** Trim trailing slashes so joins don't double up. */
export function cleanBase(raw: string): string {
  return raw.trim().replace(/\/+$/, "");
}

export function resolveApiBase(): string {
  const q = new URLSearchParams(window.location.search);
  const fromQuery = q.get("api");
  if (fromQuery) {
    const b = cleanBase(fromQuery);
    try {
      localStorage.setItem(LS_API, b);
    } catch {
      /* private mode */
    }
    return b;
  }
  try {
    const stored = localStorage.getItem(LS_API);
    if (stored) return cleanBase(stored);
  } catch {
    /* ignore */
  }
  return window.location.origin;
}

export function saveApiBase(base: string) {
  try {
    localStorage.setItem(LS_API, cleanBase(base));
  } catch {
    /* ignore */
  }
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.status = status;
  }
}

async function fetchJson<T>(
  base: string,
  path: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  const ctrl = new AbortController();
  const t = window.setTimeout(() => ctrl.abort(), init?.timeoutMs ?? 12000);
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init?.body ? { "Content-Type": "application/json" } : {}),
  };
  try {
    const token = localStorage.getItem(LS_TOKEN);
    if (token) headers.Authorization = `Bearer ${token}`;
  } catch {
    /* ignore */
  }
  try {
    const res = await fetch(`${base}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
    });
    const text = await res.text();
    let body: unknown = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = { raw: text };
    }
    if (!res.ok) {
      const msg =
        (body as { error?: string; detail?: string } | null)?.error ??
        (body as { detail?: string } | null)?.detail ??
        `${res.status} ${res.statusText}`;
      throw new ApiError(String(msg), res.status);
    }
    return body as T;
  } finally {
    window.clearTimeout(t);
  }
}

export function makeLiveClient(base: string): ApiClient {
  const b = cleanBase(base);
  return {
    mode: "live",
    base: b,
    health: () => fetchJson<Health>(b, "/health", { timeoutMs: 4500 }),
    listProjects: () => fetchJson<ProjectsResponse>(b, "/projects"),
    createProject: (body) =>
      fetchJson<CreateProjectResponse>(b, "/projects", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    runProject: (id) =>
      fetchJson<RunResponse>(b, `/projects/${encodeURIComponent(id)}/run`, {
        method: "POST",
        body: "{}",
      }),
    replanProject: (id) =>
      fetchJson<ReplanResponse>(b, `/projects/${encodeURIComponent(id)}/replan`, {
        method: "POST",
        body: "{}",
      }),
    projectStatus: (id) =>
      fetchJson<ProjectStatus>(b, `/projects/${encodeURIComponent(id)}/status`),
    handoff: (id) => fetchJson<Handoff>(b, `/projects/${encodeURIComponent(id)}/handoff`),
    fileUrl: (id, path) =>
      fetchJson<{ url: string }>(
        b,
        `/projects/${encodeURIComponent(id)}/files/${encodeURIComponent(path)}/url`,
      ),
    listConnectors: () => fetchJson<ConnectorsResponse>(b, "/connectors"),
    callConnector: (name, body) =>
      fetchJson<CallResponse>(b, `/connectors/${encodeURIComponent(name)}/call`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    probeConnector: (name) =>
      fetchJson<ProbeResponse>(b, `/connectors/${encodeURIComponent(name)}/probe`, {
        method: "POST",
        body: "{}",
      }),
    agentsStatus: () => fetchJson<AgentsResponse>(b, "/agents/status"),
    storageStatus: () => fetchJson<StorageStatus>(b, "/storage/status"),
    sync: () => fetchJson<SyncResponse>(b, "/sync", { method: "POST", body: "{}" }),
    stream(id, onState, onError) {
      const es = new EventSource(`${b}/projects/${encodeURIComponent(id)}/events/stream`);
      es.addEventListener("state", (ev) => {
        try {
          onState(JSON.parse((ev as MessageEvent).data) as ProjectStatus);
        } catch {
          /* malformed frame — ignore */
        }
      });
      es.onerror = () => onError?.();
      return () => es.close();
    },
  };
}

/** Try the real API; fall back is decided by the caller. */
export async function ping(base: string): Promise<Health> {
  return fetchJson<Health>(cleanBase(base), "/health", { timeoutMs: 4500 });
}

export function makeClient(base: string, mode: "live" | "demo"): ApiClient {
  return mode === "live" ? makeLiveClient(base) : createMockClient(base);
}

/** Short, displayable version of a base URL for the host chip. */
export function shortHost(base: string): string {
  if (base === "demo") return "simulated backend";
  try {
    const u = new URL(base);
    return u.host + (u.pathname !== "/" ? u.pathname : "");
  } catch {
    return base;
  }
}
