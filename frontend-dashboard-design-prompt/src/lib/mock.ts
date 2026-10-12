// ── Simulated Kollektiv backend ─────────────────────────────────────────────
// Used when the real API is unreachable: same shapes, same semantics, and a
// ticker that advances running projects so the dashboard stays a live
// instrument instead of a static screenshot. Clearly reported as "demo".

import type { ApiClient } from "./api";
import type {
  AgentInfo,
  Connector,
  HistoryEntry,
  ProjectStatus,
  ProjectSummary,
  StorageStatus,
  TaskT,
} from "./types";

type MockTaskFull = TaskT & MockTask;

interface MockProject {
  project_id: string;
  name: string;
  description: string;
  status: string;
  created_at: string;
  revision: number;
  tasks: MockTaskFull[];
  files: string[];
  history: HistoryEntry[];
  last_commit: string | null;
  queued_tasks: number;
  pool: { agents: number; busy: number };
}

interface MockTask {
  ticks: number;
}

const AGENT_LABELS = ["alpha-1", "beta-1", "gamma-2", "delta-2", "epsilon-2", "zeta-3"];

const rand = (n: number) => Math.floor(Math.random() * n);
const pick = <T,>(arr: T[]): T => arr[rand(arr.length)];
const hex = (n: number) =>
  Array.from({ length: n }, () => "0123456789abcdef"[rand(16)]).join("");
const nowIso = () => new Date().toISOString();
const ago = (mins: number) => new Date(Date.now() - mins * 60000).toISOString();
const delay = () =>
  new Promise<void>((r) => setTimeout(r, 110 + Math.random() * 240));

const TASK_ERRORS = [
  "429 from anthropic — rate limited, backoff 45s exhausted",
  "git push rejected: non-fast-forward on feature/agent-branch",
  "pytest exited 2: 3 failures in test_pipeline_edge",
  "ENOENT: build step expected dist/manifest.json",
];

function task(id: string, title: string, status: TaskT["status"], agent: string | null): TaskT & MockTask {
  return {
    id,
    title,
    status,
    assigned_agent: agent,
    score: status === "done" ? 58 + rand(40) : null,
    error: status === "failed" ? pick(TASK_ERRORS) : null,
    ticks: 0,
  };
}

const PLAN_POOL = [
  "Scaffold project structure and workspace config",
  "Write the data model and persistence layer",
  "Implement the core orchestration loop",
  "Add API surface with typed responses",
  "Cover the happy path with integration tests",
  "Wire structured logging and error reporting",
  "Harden input validation and edge cases",
  "Author README and operator runbook",
  "Set up CI workflow and lint gates",
  "Profile hot path and trim allocations",
  "Add retry policy and circuit breakers",
  "Ship a minimal CLI for manual runs",
];

const REPLAN_POOL = [
  "Follow-up: address review nits",
  "Smoke-test the deploy path end to end",
  "Backfill fixtures for the new schema",
  "Document rollback procedure",
];

function seedProjects(): MockProject[] {
  return [
    {
      project_id: "p-8f3c2d",
      name: "Telemetry pipeline",
      description:
        "Ingest OpenTelemetry spans into object storage, roll them up hourly, and expose a query API for the ops board.",
      status: "running",
      created_at: ago(142),
      revision: 2,
      tasks: [
        task("t-a1", "Define span schema and partitioning", "done", "alpha-1"),
        task("t-a2", "Implement batch ingest worker", "done", "beta-1"),
        task("t-a3", "Write hourly rollup job", "done", "gamma-2"),
        task("t-a4", "Query API with time-range filters", "done", "alpha-1"),
        task("t-a5", "Add p95/p99 aggregate endpoints", "done", "delta-2"),
        task("t-a6", "Backpressure handling on ingest", "in_progress", "beta-1"),
        task("t-a7", "Retention policy sweeper", "queued", null),
        task("t-a8", "Operator docs and alert rules", "pending", null),
      ],
      files: [
        "schemas/spans.sql",
        "workers/ingest.ts",
        "workers/rollup.ts",
        "api/routes/metrics.ts",
        "docs/alerts.md",
      ],
      history: [
        { at: ago(140), event: "project created — 8 tasks planned" },
        { at: ago(138), event: "run dispatched — 4 agents attached" },
        { at: ago(96), event: "t-a2 completed", detail: "score 84" },
        { at: ago(61), event: "replan revision 2 — no new tasks" },
        { at: ago(12), event: "t-a6 assigned to beta-1" },
      ],
      last_commit: "a91f2c8",
      queued_tasks: 1,
      pool: { agents: 4, busy: 1 },
    },
    {
      project_id: "p-61b7aa",
      name: "Docs site rebuild",
      description:
        "Move the handbook to a static generator, add full-text search, and cut the deploy time under a minute.",
      status: "done",
      created_at: ago(60 * 26),
      revision: 1,
      tasks: [
        task("t-b1", "Audit and migrate 86 markdown pages", "done", "alpha-1"),
        task("t-b2", "Theme with tokens from the design kit", "done", "gamma-2"),
        task("t-b3", "Client-side search index at build time", "done", "beta-1"),
        task("t-b4", "Redirect map for legacy URLs", "done", "delta-2"),
        task("t-b5", "Preview deploys on pull requests", "done", "alpha-1"),
        task("t-b6", "Link-checker in CI", "failed", "beta-1"),
      ],
      files: ["site/theme.css", "site/search-index.bin", "netlify.toml"],
      history: [
        { at: ago(60 * 26), event: "project created — 6 tasks planned" },
        { at: ago(60 * 25), event: "run dispatched" },
        { at: ago(60 * 4), event: "t-b6 failed", detail: "2 dead links in /legacy" },
        { at: ago(60 * 3), event: "run complete — 5 done / 1 failed" },
      ],
      last_commit: "f75d0b2",
      queued_tasks: 0,
      pool: { agents: 4, busy: 0 },
    },
    {
      project_id: "p-2ce910",
      name: "SDK wrappers",
      description:
        "Thin, typed client libraries for the orchestrator API in Python and TypeScript, generated from one schema.",
      status: "planned",
      created_at: ago(35),
      revision: 1,
      tasks: [
        task("t-c1", "Freeze the OpenAPI schema", "pending", null),
        task("t-c2", "Python client generator", "pending", null),
        task("t-c3", "TypeScript client generator", "pending", null),
        task("t-c4", "Contract tests against a stub server", "pending", null),
        task("t-c5", "Publish pipeline for both registries", "pending", null),
      ],
      files: [],
      history: [{ at: ago(35), event: "project created — 5 tasks planned" }],
      last_commit: null,
      queued_tasks: 0,
      pool: { agents: 4, busy: 0 },
    },
  ];
}

function seedConnectors(): Connector[] {
  return [
    {
      name: "github",
      category: "code",
      description: "Repos, issues, reviews and pushes — the agents' home turf.",
      configured: true,
      detail: null,
      actions: ["repos.get", "issues.list", "issues.comment", "pulls.review"],
      dangerous_actions: ["branches.delete"],
    },
    {
      name: "slack",
      category: "comms",
      description: "Run summaries and blockers, posted where the team reads them.",
      configured: false,
      detail: "KOLLEKTIV_SLACK_TOKEN",
      actions: ["chat.postMessage", "conversations.history"],
      dangerous_actions: ["chat.delete"],
    },
    {
      name: "notion",
      category: "docs",
      description: "Specs and decision logs, appended as pages in the shared space.",
      configured: true,
      detail: null,
      actions: ["pages.search", "pages.append"],
      dangerous_actions: [],
    },
    {
      name: "sentry",
      category: "errors",
      description: "Pull the newest exceptions so an agent can open a fix task.",
      configured: false,
      detail: "SENTRY_DSN",
      actions: ["issues.list", "events.tail"],
      dangerous_actions: [],
    },
    {
      name: "vercel",
      category: "deploys",
      description: "Preview URLs and production deploy state for shipped work.",
      configured: false,
      detail: "VERCEL_TOKEN",
      actions: ["deploy.status", "deploy.tail_logs"],
      dangerous_actions: ["deploy.cancel"],
    },
    {
      name: "docker",
      category: "ops",
      description: "Inspect containers and chase logs on the local daemon socket.",
      configured: true,
      detail: null,
      actions: ["ps", "logs.tail", "stats"],
      dangerous_actions: ["containers.prune", "images.prune"],
    },
  ];
}

export function createMockClient(base: string): ApiClient {
  const projects: MockProject[] = seedProjects();
  const connectors = seedConnectors();
  const agents: AgentInfo[] = [
    {
      account_id: "acc_01JQF2HA9M",
      label: "alpha-1",
      busy: false,
      status: "idle",
      tasks_done: 17,
      tasks_failed: 1,
      last_error: null,
    },
    {
      account_id: "acc_01JQF2HN2C",
      label: "beta-1",
      busy: true,
      status: "working",
      tasks_done: 12,
      tasks_failed: 2,
      last_error: "pytest exited 2: 3 failures in test_pipeline_edge",
    },
    {
      account_id: "acc_01JQF2J7W8",
      label: "gamma-2",
      busy: false,
      status: "idle",
      tasks_done: 9,
      tasks_failed: 0,
      last_error: null,
    },
    {
      account_id: "acc_01JQF2JQ4D",
      label: "delta-2",
      busy: false,
      status: "idle",
      tasks_done: 14,
      tasks_failed: 3,
      last_error: "429 from anthropic — rate limited, backoff exhausted",
    },
  ];

  agents.find((a) => a.label === "beta-1")!.busy = true;
  const storage: StorageStatus = {
    total_gb: 48,
    used_gb: 27.6,
    free_gb: 20.4,
    accounts: 3,
    healthy: true,
    per_account: [
      { label: "drive-01 · personal", healthy: true, used_gb: 11.2, free_gb: 3.8, bucket: "kollektiv-alpha" },
      { label: "drive-02 · workspace", healthy: true, used_gb: 14.6, free_gb: 0.4, bucket: "kollektiv-builds" },
      { label: "drive-03 · archive", healthy: true, used_gb: 1.8, free_gb: 16.2, bucket: "kollektiv-archive" },
    ],
  };

  const brainCalls = { calls: 128, failures: 2 };
  const listeners = new Set<() => void>();

  const snapshot = (p: MockProject): ProjectStatus => ({
    project_name: p.name,
    status: p.status,
    description: p.description,
    tasks: p.tasks.map(({ ticks: _ticks, ...t }) => t),
    files: [...p.files],
    history: [...p.history].slice(-14).reverse(),
    last_commit: p.last_commit,
    queued_tasks: p.tasks.filter((t) => t.status === "queued").length,
    pool: { agents: p.pool.agents, busy: p.pool.busy },
  });

  const log = (p: MockProject, event: string, detail?: string) =>
    p.history.push({ at: nowIso(), event, detail });

  function tick() {
    let changed = false;
    for (const p of projects) {
      if (p.status !== "running") continue;
      const running = p.tasks.filter((t) => t.status === "in_progress");
      const queue = p.tasks.filter((t) => t.status === "queued");

      // promote from the queue while the pool has room
      while (queue.length && running.length < p.pool.agents) {
        const t = queue.shift()!;
        const idle = agents.find((a) => !a.busy);
        const label = idle ? idle.label : pick(AGENT_LABELS);
        if (idle) {
          idle.busy = true;
          idle.status = "working";
        }
        t.status = "in_progress";
        t.assigned_agent = label;
        t.ticks = 0;
        running.push(t);
        log(p, `${t.id} assigned to ${label}`);
        changed = true;
      }

      // advance in-flight work
      for (const t of running.slice()) {
        t.ticks += 1;
        const needed = 2 + (t.id.charCodeAt(t.id.length - 1) % 3);
        if (t.ticks < needed) continue;
        t.status = Math.random() < 0.12 ? "failed" : "done";
        if (t.status === "done") {
          t.score = 58 + rand(40);
          t.error = null;
          const a = agents.find((x) => x.label === t.assigned_agent);
          if (a) a.tasks_done += 1;
          log(p, `${t.id} completed`, `score ${t.score}`);
          if (Math.random() < 0.35) {
            const nf = `out/${t.id}-${pick(["notes.md", "patch.diff", "report.json", "bench.txt"])}`;
            p.files.push(nf);
            log(p, `artifact written — ${nf}`);
          }
        } else {
          t.error = pick(TASK_ERRORS);
          const a = agents.find((x) => x.label === t.assigned_agent);
          if (a) {
            a.tasks_failed += 1;
            a.last_error = t.error;
          }
          log(p, `${t.id} failed`, t.error);
        }
        const a = agents.find((x) => x.label === t.assigned_agent);
        if (a) {
          a.busy = false;
          a.status = "idle";
        }
        changed = true;
      }

      p.pool.busy = agents.filter((a) => a.busy).length;
      p.queued_tasks = p.tasks.filter((t) => t.status === "queued").length;

      // settle a finished run
      if (
        !p.tasks.some((t) => t.status === "in_progress" || t.status === "queued") &&
        p.tasks.some((t) => t.status === "done" || t.status === "failed")
      ) {
        const done = p.tasks.filter((t) => t.status === "done").length;
        const failed = p.tasks.filter((t) => t.status === "failed").length;
        p.status = "done";
        p.last_commit = hex(7);
        log(p, `run complete — ${done} done / ${failed} failed`, `commit ${p.last_commit}`);
        changed = true;
      }
    }
    if (changed) {
      brainCalls.calls += 1;
      listeners.forEach((fn) => fn());
    }
  }

  const ticker = window.setInterval(tick, 1400);
  void ticker; // lives for the app session

  const findProject = (id: string): MockProject => {
    const p = projects.find((x) => x.project_id === id);
    if (!p) throw new Error(`unknown project "${id}"`);
    return p;
  };

  const client: ApiClient = {
    mode: "demo",
    base: "demo",

    async health() {
      await delay();
      const running = projects.some((p) => p.status === "running");
      return {
        status: "ok",
        environment: "demo",
        subsystems: {
          storage: { backend: "rclone/union", accounts: storage.accounts, healthy: storage.healthy },
          agents: { agents: agents.length, busy: agents.filter((a) => a.busy).length },
          brain: {
            configured: false,
            provider: null,
            calls: brainCalls.calls,
            failures: brainCalls.failures,
          },
          github: { configured: true, repo: "you/kollektiv-demo" },
          connectors: {
            count: connectors.length,
            configured: connectors.filter((c) => c.configured).map((c) => c.name),
            actions: connectors.reduce((n, c) => n + c.actions.length + c.dangerous_actions.length, 0),
          },
        },
        warnings: [
          "KOLLEKTIV_BRAIN_PROVIDER is not set — planning falls back to the heuristic task splitter.",
          "KOLLEKTIV_SLACK_TOKEN missing — the slack connector is disabled.",
          "drive-02 is 97% full — free space or rotate KOLLEKTIV_STORAGE_ACCOUNTS.",
        ].concat(running ? [] : []),
      };
    },

    async listProjects() {
      await delay();
      const ordered = [...projects].sort(
        (a, b) => +new Date(b.created_at) - +new Date(a.created_at),
      );
      return {
        count: ordered.length,
        projects: ordered.map(
          (p): ProjectSummary => ({
            project_id: p.project_id,
            name: p.name,
            status: p.status,
            created_at: p.created_at,
          }),
        ),
      };
    },

    async createProject(body) {
      await delay();
      const n = 4 + rand(4);
      const titles = [...PLAN_POOL].sort(() => Math.random() - 0.5).slice(0, n);
      const tasks = titles.map((t, i) => task(`t-${hex(3)}${i}`, t, "pending", null));
      const p: MockProject = {
        project_id: `p-${hex(6)}`,
        name: body.name,
        description: body.description,
        status: "planned",
        created_at: nowIso(),
        revision: 1,
        tasks,
        files: [],
        history: [{ at: nowIso(), event: `project created — ${tasks.length} tasks planned` }],
        last_commit: null,
        queued_tasks: 0,
        pool: { agents: Math.max(1, Math.min(8, body.n_agents || 3)), busy: 0 },
      };
      projects.push(p);
      log(p, `plan accepted · ${p.pool.agents} agents requested`);
      return {
        project: {
          project_id: p.project_id,
          name: p.name,
          status: p.status,
          created_at: p.created_at,
        },
        plan: { tasks: p.tasks.map(({ ticks: _t, ...rest }) => rest) },
      };
    },

    async runProject(id) {
      await delay();
      const p = findProject(id);
      const pending = p.tasks.filter((t) => t.status === "pending");
      pending.forEach((t) => (t.status = "queued"));
      p.status = "running";
      p.queued_tasks = pending.length;
      log(
        p,
        `run dispatched — ${pending.length} tasks queued`,
        `${p.pool.agents} agents in the pool`,
      );
      listeners.forEach((fn) => fn());
      return {
        status: "running",
        tasks_dispatched: pending.length,
        completed: p.tasks.filter((t) => t.status === "done").length,
        errors: [],
      };
    },

    async replanProject(id) {
      await delay();
      const p = findProject(id);
      p.revision += 1;
      const count = 1 + rand(2);
      const fresh = [...REPLAN_POOL]
        .sort(() => Math.random() - 0.5)
        .slice(0, count)
        .map((t, i) => task(`t-r${p.revision}${i}-${hex(2)}`, t, "pending", null));
      p.tasks.push(...(fresh as TaskT[] & MockTask[]));
      log(p, `replan revision ${p.revision} — ${fresh.length} new tasks`);
      listeners.forEach((fn) => fn());
      return {
        revision: p.revision,
        new_tasks: fresh.map(({ ticks: _t, ...rest }) => rest),
      };
    },

    async projectStatus(id) {
      await delay();
      return snapshot(findProject(id));
    },

    async handoff(id) {
      await delay();
      const p = findProject(id);
      const done = p.tasks.filter((t) => t.status === "done");
      const failed = p.tasks.filter((t) => t.status === "failed");
      const todo = p.tasks.filter((t) => t.status === "pending" || t.status === "queued");
      const progress = Math.round((done.length / Math.max(1, p.tasks.length)) * 100);
      const next = todo.slice(0, 3).map((t) => t.title);
      if (!next.length) next.push("Merge the run, tag a release, and archive artifacts.");
      const blockers = failed.map((t) => `${t.id} · ${t.error ?? "unknown error"}`);
      const markdown = [
        `## Handoff · ${p.name}`,
        ``,
        `Progress **${progress}%** — ${done.length}/${p.tasks.length} tasks done, ${failed.length} failed.`,
        ``,
        `### Next actions`,
        ...next.map((n) => `- ${n}`),
        ``,
        `### Blockers`,
        ...(blockers.length ? blockers.map((b) => `- ${b}`) : [`- None — clean to continue.`]),
        ``,
        `Resume with \`pnpm kollektiv resume ${p.project_id}\` — context is restored from storage, nothing is lost between sessions.`,
      ].join("\n");
      return { progress, next_actions: next, blockers, markdown };
    },

    async fileUrl(id, path) {
      await delay();
      findProject(id);
      return {
        url: `${base === "demo" ? "https://api.kollektiv.local" : base}/projects/${id}/files/${encodeURIComponent(path)}?sig=${hex(10)}`,
      };
    },

    async listConnectors() {
      await delay();
      return {
        count: connectors.length,
        configured: connectors.filter((c) => c.configured).map((c) => c.name),
        connectors,
      };
    },

    async callConnector(name, body) {
      await delay();
      const c = connectors.find((x) => x.name === name);
      if (!c) return { error: `unknown connector "${name}"` };
      if (!c.configured) return { error: `${name} is not configured — set ${c.detail}.` };
      const dangerous = c.dangerous_actions.includes(body.action);
      if (dangerous && !body.confirm) {
        return { error: "confirmation required — resubmit with confirm: true." };
      }
      return {
        ok: true,
        action: body.action,
        detail: dangerous
          ? `${body.action} executed after confirmation.`
          : `${body.action} completed.`,
        echo: body.params,
      };
    },

    async probeConnector(name) {
      await delay();
      const c = connectors.find((x) => x.name === name);
      if (!c) return { ok: false, seconds: 0, action: "unknown", error: `unknown connector "${name}"` };
      const seconds = Math.round((0.06 + Math.random() * 0.5) * 100) / 100;
      if (!c.configured) {
        return { ok: false, seconds, action: c.actions[0], error: `not configured — set ${c.detail}` };
      }
      return { ok: true, seconds, action: c.actions[0], detail: `${name} reachable` };
    },

    async agentsStatus() {
      await delay();
      return { agents };
    },

    async storageStatus() {
      await delay();
      return storage;
    },

    async sync() {
      await delay();
      return {
        status: "ok",
        pushed: 1 + rand(4),
        pulled: rand(3),
        conflicts: 0,
        detail: "work/telemetry-pipeline synced with origin",
      };
    },

    stream(id, onState, onError) {
      let closed = false;
      const push = () => {
        if (closed) return;
        try {
          onState(snapshot(findProject(id)));
        } catch (e) {
          onError?.();
        }
      };
      push();
      const fn = () => push();
      listeners.add(fn);
      const heartbeat = window.setInterval(push, 6000);
      return () => {
        closed = true;
        listeners.delete(fn);
        window.clearInterval(heartbeat);
      };
    },
  };

  return client;
}
