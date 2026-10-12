// ── Kollektiv API shapes (mirrors the documented endpoints) ────────────────

export interface Health {
  status: string;
  environment: string;
  subsystems: {
    storage: { backend: string; accounts: number; healthy: boolean };
    agents: { agents: number; busy: number };
    brain: {
      configured: boolean;
      provider: string | null;
      calls: number;
      failures: number;
    };
    github: { configured: boolean; repo: string | null };
    connectors: { count: number; configured: string[]; actions: number };
  };
  warnings: string[];
}

export interface ProjectSummary {
  project_id: string;
  name: string;
  status: string;
  created_at: string;
}

export interface ProjectsResponse {
  count: number;
  projects: ProjectSummary[];
}

export type TaskStatus = "pending" | "queued" | "in_progress" | "done" | "failed";

export interface TaskT {
  id: string;
  title: string;
  status: TaskStatus;
  assigned_agent: string | null;
  score: number | null;
  error: string | null;
}

export interface HistoryEntry {
  at: string;
  event: string;
  detail?: string;
}

export interface ProjectStatus {
  project_name: string;
  status: string;
  description?: string;
  tasks: TaskT[];
  files: string[];
  history: HistoryEntry[];
  last_commit: string | null;
  queued_tasks: number;
  pool: { agents: number; busy: number } & Record<string, unknown>;
}

export interface Handoff {
  progress: number;
  next_actions: string[];
  blockers: string[];
  markdown: string;
}

export interface CreateProjectBody {
  name: string;
  description: string;
  n_agents: number;
}

export interface CreateProjectResponse {
  project: ProjectSummary;
  plan: { tasks: TaskT[] };
}

export interface RunResponse {
  status: string;
  tasks_dispatched: number;
  completed: number;
  errors: string[];
}

export interface ReplanResponse {
  revision: number;
  new_tasks: TaskT[];
}

export interface Connector {
  name: string;
  category: string;
  description: string;
  configured: boolean;
  detail: string | null;
  actions: string[];
  dangerous_actions: string[];
}

export interface ConnectorsResponse {
  count: number;
  configured: string[];
  connectors: Connector[];
}

export interface CallBody {
  action: string;
  params: Record<string, unknown>;
  confirm: boolean;
}

export interface CallResponse {
  ok?: boolean;
  action?: string;
  detail?: string;
  error?: string;
  [k: string]: unknown;
}

export interface ProbeResponse {
  ok: boolean;
  seconds: number;
  action: string;
  detail?: string;
  error?: string;
}

export interface AgentInfo {
  account_id: string;
  label: string;
  busy: boolean;
  status: string;
  tasks_done: number;
  tasks_failed: number;
  last_error: string | null;
}

export interface AgentsResponse {
  agents: AgentInfo[];
}

export interface StorageAccount {
  label: string;
  healthy: boolean;
  used_gb: number;
  free_gb: number;
  bucket: string;
}

export interface StorageStatus {
  total_gb: number;
  used_gb: number;
  free_gb: number;
  accounts: number;
  healthy: boolean;
  per_account: StorageAccount[];
}

export interface SyncResponse {
  status: string;
  pushed?: number;
  pulled?: number;
  conflicts?: number;
  detail?: string;
  [k: string]: unknown;
}
