/**
 * Kollektiv mission control — application logic.
 *
 * Structure
 *   config + api()      : API discovery, fetch wrapper, auth header
 *   store               : tiny observable state container
 *   render/*            : pure-ish renderers, one per view
 *   actions             : API mutations (create, run, replan, call, probe)
 *   palette + shortcuts : ⌘K command palette and keyboard navigation
 *   stream              : Server-Sent Events for a live project view
 *
 * Rules kept from the backend: no telemetry, no external requests, no CDN.
 * Everything here talks to the configured Kollektiv API and nothing else.
 */

/* ------------------------------------------------------------------ *
 * Configuration
 * ------------------------------------------------------------------ */
const STORAGE = {
  get api() { try { return localStorage.getItem("kollektiv.api") || ""; } catch { return ""; } },
  set api(value) { try { localStorage.setItem("kollektiv.api", value); } catch { /* private mode */ } },
  get token() { try { return localStorage.getItem("kollektiv.token") || ""; } catch { return ""; } },
  get theme() { try { return localStorage.getItem("kollektiv.theme") || ""; } catch { return ""; } },
  set theme(value) { try { localStorage.setItem("kollektiv.theme", value); } catch { /* private mode */ } },
  get onboarded() { try { return localStorage.getItem("kollektiv.onboarded") === "yes"; } catch { return false; } },
  set onboarded(value) { try { localStorage.setItem("kollektiv.onboarded", value ? "yes" : ""); } catch { /* private mode */ } },
};

/** Guess the API base: ?api= > saved > served-from-/ui > same origin. */
function guessBase() {
  const fromQuery = new URLSearchParams(location.search).get("api");
  if (fromQuery) return fromQuery.replace(/\/$/, "");
  if (STORAGE.api) return STORAGE.api;
  if (location.protocol.startsWith("http")) {
    if (location.pathname.startsWith("/ui")) return location.origin;
    if (location.port !== "8088") return location.origin;   // single-origin deploy
  }
  return "http://localhost:8000";
}

/* ------------------------------------------------------------------ *
 * Tiny state container
 * ------------------------------------------------------------------ */
const store = {
  base: "",
  health: null,
  projects: [],
  connectors: [],
  agents: [],
  storage: null,
  openProject: null,
  stream: null,
  onboardStep: 1,
  listeners: new Set(),
  set(patch) {
    Object.assign(this, patch);
    for (const listener of this.listeners) listener(this);
  },
  on(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); },
};

/* ------------------------------------------------------------------ *
 * DOM helpers
 * ------------------------------------------------------------------ */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (value) => (value === null || value === undefined ? "—" : String(value));

/** Status pill with a hover tooltip. */
function pill(value, { tone = "", tip = "" } = {}) {
  const text = String(value ?? "unknown");
  const klass = tone || toneFor(text);
  const title = tip || `Status: ${text}`;
  return `<span class="kv-pill ${klass}" data-tip="${esc(title)}">${esc(text)}</span>`;
}
function toneFor(value) {
  const v = String(value).toLowerCase();
  if (["completed", "ok", "ready", "healthy", "idle", "connected", "yes"].includes(v)) return "ok";
  if (["failed", "error", "unhealthy", "bad", "no", "off"].includes(v)) return "bad";
  if (["running", "in_progress", "busy", "planned", "setup", "pending", "degraded"].includes(v)) return "warn";
  return "info";
}

/** Toasts: transient feedback, always dismissible, never blocking. */
function toast(message, tone = "ok", { timeout = 5200 } = {}) {
  const host = $("#toasts");
  const el = document.createElement("div");
  el.className = `kv-toast ${tone}`;
  el.setAttribute("role", tone === "bad" ? "alert" : "status");
  el.innerHTML = `<div><strong>${esc(tone === "bad" ? "Failed" : tone === "warn" ? "Heads up" : "Done")}</strong>
    <div class="kv-soft">${esc(message)}</div></div>`;
  el.addEventListener("click", () => dismiss());
  host.append(el);
  let timer = setTimeout(dismiss, timeout);
  function dismiss() {
    clearTimeout(timer);
    el.dataset.leaving = "true";
    setTimeout(() => el.remove(), 320);
  }
  return dismiss;
}

/** Replace a table body with a skeleton while loading. */
function skeleton(tbody, cols, height = 16) {
  tbody.innerHTML = Array.from({ length: 3 }, () =>
    `<tr><td colspan="${cols}"><div class="kv-skel" style="height:${height}px"></div></td></tr>`).join("");
}
function empty(tbody, cols, title, detail = "") {
  tbody.innerHTML = `<tr><td colspan="${cols}"><div class="kv-empty"><strong>${esc(title)}</strong>${esc(detail)}</div></td></tr>`;
}

/* ------------------------------------------------------------------ *
 * API client
 * ------------------------------------------------------------------ */
async function api(path, { method = "GET", body, signal } = {}) {
  const headers = { "content-type": "application/json" };
  if (STORAGE.token) headers.authorization = `Bearer ${STORAGE.token}`;
  const response = await fetch(`${store.base}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { detail: text }; }
  if (!response.ok) {
    const detail = (payload && (payload.detail || payload.error)) || response.statusText;
    throw new Error(`${response.status} ${detail}`);
  }
  return payload;
}

/* ------------------------------------------------------------------ *
 * Renderers
 * ------------------------------------------------------------------ */
function renderConnection() {
  $("#api-label").textContent = store.base.replace(/^https?:\/\//, "") || "not connected";
  $("#api-input").value = store.base;
}

function renderBanner() {
  const slot = $("#banner-slot");
  const warnings = (store.health && store.health.warnings) || [];
  if (!store.health) {
    slot.innerHTML = `<div class="kv-banner bad"><div class="kv-banner-body">
      <strong>Cannot reach the API</strong>
      <div class="kv-soft">Start it with <span class="kv-mono">kollektiv serve-api</span>, then press Connect.</div>
    </div></div>`;
    return;
  }
  if (!warnings.length) {
    slot.innerHTML = `<div class="kv-banner" style="border-color:color-mix(in srgb, var(--kv-ok) 45%, var(--kv-line));background:color-mix(in srgb, var(--kv-ok) 8%, transparent)">
      <div class="kv-banner-body"><strong>Every subsystem is configured.</strong></div></div>`;
    return;
  }
  slot.innerHTML = `<div class="kv-banner">
    <div class="kv-banner-body">
      <strong>${warnings.length} configuration warning${warnings.length === 1 ? "" : "s"}</strong>
      <div class="kv-soft">Each line names the setting that removes it.</div>
      <ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
    </div>
  </div>`;
}

function renderStats() {
  const h = store.health || {};
  const subsystems = h.subsystems || {};
  $("#stat-api").textContent = h.status || "—";
  $("#stat-api-sub").textContent = h.environment ? `${h.environment}${h.started ? " · started" : ""}` : "offline";

  const storage = subsystems.storage || {};
  $("#stat-storage").textContent = storage.backend || (storage.configured ? "ready" : "local");
  $("#stat-storage-sub").textContent = `${num(storage.accounts ?? 0)} account(s), ${num(storage.healthy ?? 0)} healthy`;

  const agents = subsystems.agents || {};
  $("#stat-agents").textContent = num(agents.agents ?? 0);
  $("#stat-agents-sub").textContent = `${num(agents.busy ?? 0)} busy`;

  const brain = subsystems.brain || {};
  $("#stat-brain").textContent = brain.configured ? (brain.provider || "llm") : "heuristic";
  $("#stat-brain-sub").textContent = `${num(brain.calls ?? 0)} calls, ${num(brain.failures ?? 0)} failures`;

  const github = subsystems.github || {};
  $("#stat-github").textContent = github.configured ? "connected" : "off";
  $("#stat-github-sub").textContent = github.repo || "not configured";

  const connectors = subsystems.connectors || {};
  $("#stat-connectors").textContent = `${(connectors.configured || []).length}/${num(connectors.count ?? 0)}`;
  $("#stat-connectors-sub").textContent = `${num(connectors.actions ?? 0)} actions available`;
}

function renderProjects() {
  const tbody = $("#projects-body");
  const projects = store.projects || [];
  $("#projects-hint").textContent = projects.length
    ? `${projects.length} project(s) · click a row for the live view`
    : "none yet — create one above";
  if (!projects.length) {
    return empty(tbody, 5, "No projects yet", "Describe what to build above and press Create.");
  }
  tbody.innerHTML = projects.map((project) => {
    const total = project.tasks_total ?? project.tasks ?? null;
    const done = project.tasks_completed ?? null;
    const percent = total ? Math.round((100 * (done || 0)) / total) : Number(project.progress ?? 0);
    return `<tr data-project="${esc(project.project_id)}">
      <td data-label="Project"><strong>${esc(project.name || project.project_id)}</strong>
          <div class="kv-muted kv-mono">${esc(project.project_id)}</div></td>
      <td data-label="Status">${pill(project.status, { tip: `Updated ${esc(project.updated_at || project.created_at || "unknown")}` })}</td>
      <td data-label="Tasks" class="kv-mono">${done === null ? num(total) : `${done}/${total}`}</td>
      <td data-label="Progress" style="min-width:130px">
        <div class="kv-progress thin" data-tip="${percent}% complete"><span style="width:${percent}%"></span></div>
      </td>
      <td data-label="Actions" class="kv-nowrap">
        <button class="kv-btn ghost small" data-act="open" data-id="${esc(project.project_id)}">Open</button>
        <button class="kv-btn ghost small" data-act="run" data-id="${esc(project.project_id)}">Run</button>
        <button class="kv-btn ghost small" data-act="handoff" data-id="${esc(project.project_id)}">Handoff</button>
      </td>
    </tr>`;
  }).join("");
}

function renderConnectors() {
  const tbody = $("#connectors-body");
  const connectors = store.connectors || [];
  if (!connectors.length) {
    return empty(tbody, 4, "No connectors registered", "Start the API to load the connector catalogue.");
  }
  tbody.innerHTML = connectors.map((connector) => {
    const actions = connector.actions || [];
    const dangerous = new Set(connector.dangerous_actions || []);
    return `<tr data-connector="${esc(connector.name)}">
      <td data-label="Service"><strong>${esc(connector.name)}</strong>
          <div class="kv-muted">${esc(connector.description || connector.category || "")}</div></td>
      <td data-label="Status">${connector.configured
        ? pill("ready", { tip: "Configured and ready to call" })
        : `${pill("setup", { tip: esc(connector.detail || "missing credentials") })}<div class="kv-muted" style="margin-top:4px">${esc(connector.detail || "")}</div>`}</td>
      <td data-label="Actions" class="kv-muted kv-mono">${actions.map((a) => esc(a)).join(", ") || "—"}</td>
      <td data-label="Run" class="kv-nowrap">
        <select class="kv-select" style="max-width:190px" data-action-select="${esc(connector.name)}"
                ${connector.configured ? "" : "disabled"}>
          ${actions.map((a) => `<option value="${esc(a)}">${esc(a)}${dangerous.has(a) ? " ⚠" : ""}</option>`).join("")}
        </select>
        <button class="kv-btn ghost small" data-act="call" data-id="${esc(connector.name)}"
                ${connector.configured ? "" : "disabled"}>Call</button>
        <button class="kv-btn ghost small" data-act="probe" data-id="${esc(connector.name)}">Probe</button>
      </td>
    </tr>`;
  }).join("");
}

function renderAgentsAndStorage() {
  const agentsBody = $("#agents-body");
  const agents = store.agents || [];
  agentsBody.innerHTML = agents.length
    ? agents.map((agent) => `<tr>
        <td data-label="Agent"><strong>${esc(agent.label || agent.account_id)}</strong>
            <div class="kv-muted kv-mono">${esc(agent.account_id)}</div></td>
        <td data-label="Status">${pill(agent.busy ? "busy" : agent.status || "idle")}</td>
        <td data-label="Done" class="kv-mono">${num(agent.tasks_done ?? 0)}</td>
        <td data-label="Failures" class="kv-mono">${num(agent.tasks_failed ?? 0)}</td>
        <td data-label="Last error" class="kv-muted">${esc(agent.last_error || "")}</td>
      </tr>`).join("")
    : `<tr><td colspan="5"><div class="kv-empty"><strong>No worker agents</strong>
        Set <span class="kv-mono">ARENA_ACCOUNTS</span> (or run <span class="kv-mono">kollektiv login</span>).</div></td></tr>`;

  const storage = store.storage || {};
  const rows = storage.per_account || [];
  const storageBody = $("#storage-body");
  storageBody.innerHTML = rows.length
    ? rows.map((account) => `<tr>
        <td data-label="Account"><strong>${esc(account.label || account.account_id)}</strong></td>
        <td data-label="Healthy">${account.healthy ? pill("healthy") : pill("unhealthy")}</td>
        <td data-label="Used" class="kv-mono">${esc(account.used_human || `${account.used_gb ?? 0} GB`)}</td>
        <td data-label="Free" class="kv-mono">${esc(account.free_human || `${account.free_gb ?? 0} GB`)}</td>
        <td data-label="Bucket" class="kv-muted kv-mono">${esc(account.bucket || account.remote_root || "")}</td>
      </tr>`).join("")
    : `<tr><td colspan="5"><div class="kv-empty"><strong>No shared drive configured</strong>
        Set <span class="kv-mono">R2_*</span> or <span class="kv-mono">TERABOX_ACCOUNTS</span>; state stays in the local workspace.</div></td></tr>`;
}

function renderDrawer() {
  const project = store.openProject;
  const drawer = $("#drawer");
  if (!project) { drawer.dataset.open = "false"; $("#drawer-scrim").dataset.open = "false"; return; }

  drawer.dataset.open = "true";
  $("#drawer-scrim").dataset.open = "true";
  $("#drawer-title").textContent = project.project_name || project.project_id;
  $("#drawer-status").textContent = project.status || "—";
  $("#drawer-status").className = `kv-pill ${toneFor(project.status)}`;
  $("#drawer-status").dataset.tip = `Project ${project.project_id}`;

  const tasks = project.tasks || [];
  const done = tasks.filter((t) => t.status === "completed").length;
  const percent = tasks.length ? Math.round((100 * done) / tasks.length) : 0;
  const history = (project.history || []).slice(-25).reverse();

  $("#drawer-body").innerHTML = `
    <p class="kv-soft">${esc(project.description || "No description recorded.")}</p>
    <div class="kv-progress" data-tip="${percent}% complete"><span style="width:${percent}%"></span></div>
    <div class="kv-grid cols-4" style="margin-top:14px">
      <div class="kv-card kv-stat"><div class="kv-label">Tasks</div>
        <div class="kv-value">${done}/${tasks.length}</div><div class="kv-sub">${num(project.queued_tasks ?? 0)} queued</div></div>
      <div class="kv-card kv-stat"><div class="kv-label">Files</div>
        <div class="kv-value">${num((project.files || []).length)}</div><div class="kv-sub">collected artifacts</div></div>
      <div class="kv-card kv-stat"><div class="kv-label">Last commit</div>
        <div class="kv-value kv-mono" style="font-size:15px">${esc(project.last_commit || "—")}</div>
        <div class="kv-sub">GitHub sync</div></div>
      <div class="kv-card kv-stat"><div class="kv-label">Pool</div>
        <div class="kv-value">${num((project.pool && project.pool.agents) || store.agents.length)}</div>
        <div class="kv-sub">agents available</div></div>
    </div>

    <div class="kv-section"><header><h2>Tasks</h2></header>
      <div class="kv-table-wrap"><table class="kv-table">
        <thead><tr><th>id</th><th>title</th><th>status</th><th>agent</th><th>score</th></tr></thead>
        <tbody>${tasks.length ? tasks.map((task) => `<tr>
          <td class="kv-mono">${esc(task.id)}</td>
          <td>${esc(task.title)}<div class="kv-muted">${esc((task.description || "").slice(0, 120))}</div></td>
          <td>${pill(task.status, { tip: task.error ? `Error: ${esc(task.error)}` : `Status: ${esc(task.status)}` })}</td>
          <td class="kv-mono">${esc(task.assigned_agent || "—")}</td>
          <td class="kv-mono">${task.score == null ? "—" : Number(task.score).toFixed(2)}</td>
        </tr>`).join("") : `<tr><td colspan="5" class="kv-muted">No tasks yet.</td></tr>`}</tbody>
      </table></div>
    </div>

    <div class="kv-section"><header><h2>Files</h2>
      <span class="kv-hint">Click a file to copy a shareable URL.</span></header>
      <div class="kv-row">${(project.files || []).length
        ? (project.files || []).map((path) => `<button class="kv-btn ghost small" data-act="file-url"
             data-id="${esc(project.project_id)}" data-path="${esc(path)}"
             data-tip="Copy a shareable download URL">${esc(path)}</button>`).join("")
        : `<span class="kv-muted">No artifacts collected yet.</span>`}</div>
    </div>

    <div class="kv-section"><header><h2>Event history</h2></header>
      <pre class="kv-pre kv-mono">${history.length
        ? esc(history.map((e) => `${e.timestamp || ""}  ${e.agent_id || "?"}  ${e.action || ""}  ${e.result || ""}`).join("\n"))
        : "—"}</pre>
    </div>`;
}

/* ------------------------------------------------------------------ *
 * Data loading
 * ------------------------------------------------------------------ */
async function loadHealth() {
  try {
    const health = await api("/health");
    store.set({ health });
  } catch (error) {
    store.set({ health: null });
    toast(`API unreachable: ${error.message}`, "bad");
  }
}

async function loadProjects() {
  try {
    const data = await api("/projects");
    const projects = data.projects || data || [];
    store.set({ projects });
    // Enrich with task counts when the API exposes them cheaply.
    const enriched = await Promise.all(projects.map(async (project) => {
      try {
        const status = await api(`/projects/${project.project_id}/status`);
        const tasks = status.tasks || [];
        return {
          ...project,
          status: status.status || project.status,
          tasks_total: tasks.length,
          tasks_completed: tasks.filter((t) => t.status === "completed").length,
        };
      } catch { return project; }
    }));
    store.set({ projects: enriched });
  } catch (error) {
    store.set({ projects: [] });
    toast(`Could not list projects: ${error.message}`, "bad");
  }
}

async function loadConnectors() {
  try {
    const data = await api("/connectors");
    store.set({ connectors: data.connectors || [] });
  } catch {
    store.set({ connectors: [] });
  }
}

async function loadAgentsAndStorage() {
  try {
    const agents = await api("/agents/status");
    store.set({ agents: agents.agents || [] });
  } catch { /* report via the stat card */ }
  try {
    const storage = await api("/storage/status");
    store.set({ storage });
  } catch { /* report via the stat card */ }
}

async function refreshAll({ quiet = true } = {}) {
  await Promise.all([loadHealth(), loadProjects(), loadConnectors(), loadAgentsAndStorage()]);
  renderAll();
  if (!quiet) toast("Refreshed");
}

function renderAll() {
  const health = store.health;
  const connection = $("#conn-state");
  connection.dataset.state = health ? (health.status === "ok" ? "ok" : "warn") : "bad";
  connection.textContent = health ? `API ${health.status}` : "API offline";
  const configured = (health && health.subsystems && health.subsystems.connectors && health.subsystems.connectors.configured) || [];
  const connectorState = $("#connectors-state");
  connectorState.dataset.state = configured.length ? "ok" : "warn";
  connectorState.textContent = `${configured.length} connector(s) ready`;

  renderConnection();
  renderBanner();
  reportConnection(health ? health.status : "offline");
  renderStats();
  renderProjects();
  renderConnectors();
  renderAgentsAndStorage();
  renderDrawer();
}

/* ------------------------------------------------------------------ *
 * Actions
 * ------------------------------------------------------------------ */
async function createProject(event) {
  event.preventDefault();
  const description = $("#p-desc").value.trim();
  if (!description) { $("#create-status").textContent = "Add a description first."; return; }
  const button = $("#create-btn");
  button.disabled = true;
  button.textContent = "Planning…";
  try {
    const project = await api("/projects", {
      method: "POST",
      body: {
        name: $("#p-name").value.trim(),
        description,
        n_agents: Number($("#p-agents").value),
      },
    });
    const tasks = (project.plan && project.plan.tasks) || [];
    toast(`Planned ${tasks.length} task(s) for ${project.name || project.project_id}`);
    $("#p-desc").value = "";
    $("#p-name").value = "";
    await loadProjects();
    renderAll();
    if ($("#p-run").value === "run") await runProject(project.project_id);
    else openProject(project.project_id);
  } catch (error) {
    toast(`Planning failed: ${error.message}`, "bad");
  } finally {
    button.disabled = false;
    button.textContent = "Create";
    $("#create-status").textContent = "";
  }
}

async function runProject(projectId) {
  const close = toast(`Running ${projectId}…`, "warn", { timeout: 60000 });
  try {
    const summary = await api(`/projects/${projectId}/run`, { method: "POST" });
    close();
    toast(`${summary.status}: ${summary.completed ?? 0}/${summary.tasks_dispatched ?? 0} task(s), `
      + `${(summary.artifact && summary.artifact.file_count) ?? 0} file(s)`);
    await refreshAll();
  } catch (error) {
    close();
    toast(`Run failed: ${error.message}`, "bad");
  }
}

async function replanProject(projectId) {
  try {
    const outcome = await api(`/projects/${projectId}/replan`, { method: "POST" });
    const added = (outcome.new_tasks || []).length;
    toast(added ? `Revision ${outcome.revision}: ${added} corrective task(s)` : "Nothing to replan.");
    if (store.openProject) await openProject(projectId);
  } catch (error) {
    toast(`Replan failed: ${error.message}`, "bad");
  }
}

async function openProject(projectId) {
  try {
    const status = await api(`/projects/${projectId}/status`);
    store.set({ openProject: status });
    renderDrawer();
    startStream(projectId);
  } catch (error) {
    toast(`Could not open ${projectId}: ${error.message}`, "bad");
  }
}

async function copyHandoff(projectId) {
  try {
    const handoff = await api(`/projects/${projectId}/handoff`);
    const text = handoff.markdown || JSON.stringify(handoff, null, 2);
    await navigator.clipboard.writeText(text);
    toast("Handoff briefing copied — paste it into a new session");
  } catch (error) {
    toast(`Handoff failed: ${error.message}`, "bad");
  }
}

async function copyFileUrl(projectId, path) {
  try {
    const data = await api(`/projects/${projectId}/files/${encodeURIComponent(path)}/url`);
    const url = data.url || data.download_url || "";
    await navigator.clipboard.writeText(url);
    toast(url ? "Download URL copied" : "No URL available for this backend", url ? "ok" : "warn");
  } catch (error) {
    toast(`Could not build a URL: ${error.message}`, "bad");
  }
}

async function callConnector(name) {
  const select = $(`[data-action-select="${name}"]`);
  const action = select ? select.value : "";
  if (!action) return;
  const raw = window.prompt(`Parameters for ${name}.${action} (JSON object)`, "{}");
  if (raw === null) return;
  let params = {};
  try { params = raw.trim() ? JSON.parse(raw) : {}; }
  catch (error) { toast(`Not valid JSON: ${error.message}`, "bad"); return; }

  const dangerous = /send|create|comment|append|post|delete|commit|open_/i.test(action);
  if (dangerous) {
    const ok = await confirmDialog(`${name}.${action} changes data outside Kollektiv. Run it?`);
    if (!ok) return;
  }
  try {
    const result = await api(`/connectors/${name}/call`, {
      method: "POST",
      body: { action, params, confirm: dangerous },
    });
    toast(`${name}.${action} succeeded — see the console for the payload`);
    console.info("[kollektiv] connector result", result);
  } catch (error) {
    toast(`${name}.${action} failed: ${error.message}`, "bad");
  }
}

async function probeConnector(name) {
  const close = toast(`Probing ${name}…`, "warn", { timeout: 20000 });
  try {
    const report = await api(`/connectors/${name}/probe`, { method: "POST" });
    close();
    if (report.ok) toast(`${name}: reachable in ${report.seconds}s`);
    else toast(`${name}: ${report.error}`, "bad");
  } catch (error) {
    close();
    toast(`Probe failed: ${error.message}`, "bad");
  }
}

async function probeAll() {
  const configured = (store.connectors || []).filter((c) => c.configured);
  if (!configured.length) { toast("No configured connectors to probe", "warn"); return; }
  for (const connector of configured) await probeConnector(connector.name);
}

/** Modal confirmation used for dangerous actions. */
function confirmDialog(message, title = "Confirm action") {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  return new Promise((resolve) => {
    const done = (answer) => {
      dialog.close();
      $("#confirm-ok").removeEventListener("click", onOk);
      $("#confirm-cancel").removeEventListener("click", onCancel);
      resolve(answer);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    $("#confirm-ok").addEventListener("click", onOk);
    $("#confirm-cancel").addEventListener("click", onCancel);
    dialog.showModal();
  });
}

/* ------------------------------------------------------------------ *
 * Live project stream (Server-Sent Events)
 * ------------------------------------------------------------------ */
function startStream(projectId) {
  stopStream();
  if (!("EventSource" in window)) return;
  const url = `${store.base}/projects/${projectId}/events/stream`;
  const source = new EventSource(url);
  store.stream = source;
  $("#drawer-live").dataset.state = "warn";
  source.addEventListener("state", (event) => {
    try {
      const payload = JSON.parse(event.data);
      const current = store.openProject || {};
      store.openProject = { ...current, ...payload };
      renderDrawer();
      $("#drawer-live").dataset.state = "ok";
    } catch { /* ignore malformed frames */ }
  });
  source.addEventListener("error", () => {
    $("#drawer-live").dataset.state = "bad";
  });
}
function stopStream() {
  if (store.stream) { store.stream.close(); store.stream = null; }
  const live = $("#drawer-live");
  if (live) live.dataset.state = "";
}

/* ------------------------------------------------------------------ *
 * Command palette
 * ------------------------------------------------------------------ */
const commands = [
  { id: "nav-overview", label: "Go to Overview", hint: "view", run: () => navigate("overview") },
  { id: "nav-projects", label: "Go to Projects", hint: "view", run: () => navigate("projects") },
  { id: "nav-connectors", label: "Go to Connectors", hint: "view", run: () => navigate("connectors") },
  { id: "nav-agents", label: "Go to Agents & storage", hint: "view", run: () => navigate("agents") },
  { id: "refresh", label: "Refresh everything", hint: "data", run: () => refreshAll({ quiet: false }) },
  { id: "theme", label: "Toggle light/dark theme", hint: "ui", run: () => toggleTheme() },
  { id: "probe", label: "Probe all connectors", hint: "connectors", run: () => probeAll() },
  { id: "sync", label: "Run GitHub sync now", hint: "sync", run: () => syncNow() },
];

function paletteItems(query) {
  const q = query.trim().toLowerCase();
  const items = [...commands];
  for (const project of store.projects || []) {
    items.push({
      id: `open-${project.project_id}`,
      label: `Open ${project.name || project.project_id}`,
      hint: project.status || "project",
      run: () => openProject(project.project_id),
    });
    items.push({
      id: `run-${project.project_id}`,
      label: `Run ${project.name || project.project_id}`,
      hint: "project",
      run: () => runProject(project.project_id),
    });
    items.push({
      id: `handoff-${project.project_id}`,
      label: `Copy handoff for ${project.name || project.project_id}`,
      hint: "handoff",
      run: () => copyHandoff(project.project_id),
    });
  }
  return q ? items.filter((item) => `${item.label} ${item.hint}`.toLowerCase().includes(q)) : items;
}

function openPalette() {
  const palette = $("#palette");
  palette.hidden = false;
  $("#palette-input").value = "";
  $("#palette-input").focus();
  renderPalette("");
}
function closePalette() { $("#palette").hidden = true; }

function renderPalette(query) {
  const list = $("#palette-list");
  const items = paletteItems(query).slice(0, 40);
  if (!items.length) {
    list.innerHTML = `<div class="kv-palette-item" data-active="false">No matches</div>`;
    return;
  }
  list.innerHTML = items.map((item, index) => `
    <div class="kv-palette-item" role="option" data-index="${index}" data-active="${index === 0}">
      <span>${esc(item.label)}</span><small>${esc(item.hint)}</small>
    </div>`).join("");
  list.dataset.items = JSON.stringify(items.map((item) => item.id));
  window.__paletteItems = items;
}

function paletteMove(delta) {
  const nodes = $$(".kv-palette-item", $("#palette-list"));
  if (!nodes.length) return;
  const current = nodes.findIndex((node) => node.dataset.active === "true");
  const next = (current + delta + nodes.length) % nodes.length;
  nodes.forEach((node, index) => { node.dataset.active = String(index === next); });
  nodes[next].scrollIntoView({ block: "nearest" });
}

function paletteRun() {
  const active = $('.kv-palette-item[data-active="true"]', $("#palette-list"));
  const index = active ? Number(active.dataset.index) : 0;
  const item = (window.__paletteItems || [])[index];
  if (!item) return;
  closePalette();
  try { item.run(); } catch (error) { toast(String(error.message || error), "bad"); }
}

/* ------------------------------------------------------------------ *
 * Navigation, theme, shortcuts
 * ------------------------------------------------------------------ */
function navigate(route) {
  const views = $$("[data-view]");
  const known = views.map((view) => view.dataset.view);
  const target = known.includes(route) ? route : "overview";
  views.forEach((view) => view.classList.toggle("kv-hidden", view.dataset.view !== target));
  $$(".kv-nav a, .kv-tabbar a").forEach((link) => {
    if (link.dataset.route === target) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  closeSidebar();
  if (location.hash !== `#/${target}`) history.replaceState(null, "", `#/${target}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme
    || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  STORAGE.theme = next;
  toast(`Theme: ${next}`, "ok", { timeout: 1800 });
}

async function syncNow() {
  const close = toast("Syncing…", "warn", { timeout: 30000 });
  try {
    const result = await api("/sync", { method: "POST" });
    close();
    toast(`Synced ${result.commits ?? 0} commit(s), ${(result.errors || []).length} error(s)`);
    await refreshAll();
  } catch (error) {
    close();
    toast(`Sync failed: ${error.message}`, "bad");
  }
}

function bindEvents() {
  $("#api-connect").addEventListener("click", async () => {
    store.base = $("#api-input").value.trim().replace(/\/$/, "") || guessBase();
    STORAGE.api = store.base;
    renderConnection();
    await refreshAll();
  });
  $("#api-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") $("#api-connect").click();
  });

  $("#refresh-all").addEventListener("click", () => refreshAll({ quiet: false }));
  $("#sync-now").addEventListener("click", syncNow);
  $("#probe-all").addEventListener("click", probeAll);
  $("#project-form").addEventListener("submit", createProject);
  $("#palette-open").addEventListener("click", openPalette);
  $("#theme-toggle").addEventListener("click", toggleTheme);

  $("#projects-body").addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    const row = event.target.closest("[data-project]");
    const id = (button && button.dataset.id) || (row && row.dataset.project);
    if (!id) return;
    const act = button ? button.dataset.act : "open";
    if (act === "open") openProject(id);
    if (act === "run") runProject(id);
    if (act === "handoff") copyHandoff(id);
  });

  $("#connectors-body").addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    if (!button) return;
    if (button.dataset.act === "call") callConnector(button.dataset.id);
    if (button.dataset.act === "probe") probeConnector(button.dataset.id);
  });

  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-scrim").addEventListener("click", closeDrawer);
  $("#drawer-run").addEventListener("click", () => store.openProject && runProject(store.openProject.project_id));
  $("#drawer-replan").addEventListener("click", () => store.openProject && replanProject(store.openProject.project_id));
  $("#drawer-handoff").addEventListener("click", () => store.openProject && copyHandoff(store.openProject.project_id));
  $("#drawer-body").addEventListener("click", (event) => {
    const button = event.target.closest('[data-act="file-url"]');
    if (button) copyFileUrl(button.dataset.id, button.dataset.path);
  });

  $("#palette-input").addEventListener("input", (event) => renderPalette(event.target.value));
  $("#palette-list").addEventListener("click", (event) => {
    const item = event.target.closest(".kv-palette-item");
    if (item && window.__paletteItems) {
      const chosen = window.__paletteItems[Number(item.dataset.index)];
      closePalette();
      if (chosen) chosen.run();
    }
  });
  $$("[data-close-palette]").forEach((node) => node.addEventListener("click", closePalette));

  document.addEventListener("keydown", (event) => {
    const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault(); openPalette(); return;
    }
    if (event.key === "Escape") {
      if (!$("#palette").hidden) closePalette();
      else if ($("#drawer").dataset.open === "true") closeDrawer();
      return;
    }
    if (!$("#palette").hidden) {
      if (event.key === "ArrowDown") { event.preventDefault(); paletteMove(1); }
      if (event.key === "ArrowUp") { event.preventDefault(); paletteMove(-1); }
      if (event.key === "Enter") { event.preventDefault(); paletteRun(); }
      return;
    }
    if (inField) return;
    if (event.key === "t") toggleTheme();
    if (event.key === "r") refreshAll({ quiet: false });
    if (event.key === "g") {
      const next = (e) => {
        const map = { o: "overview", p: "projects", c: "connectors", a: "agents" };
        if (map[e.key]) { navigate(map[e.key]); document.removeEventListener("keydown", next); }
      };
      document.addEventListener("keydown", next, { once: true });
    }
  });

  window.addEventListener("hashchange", () => navigate((location.hash || "#/overview").replace("#/", "")));
}

/* ------------------------------------------------------------------ *
 * Depth: 3D tilt + pointer spotlight on the glass cards
 * ------------------------------------------------------------------ */
function initTilt() {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fine = window.matchMedia("(pointer: fine)").matches;
  if (reduced || !fine) return;  // touch and reduced-motion users get a still card
  const MAX = 7;                 // degrees; enough to feel 3D, not enough to skew text

  document.addEventListener("pointermove", (event) => {
    const card = event.target.closest(".kv-card");
    if (!card) return;
    const box = card.getBoundingClientRect();
    const px = (event.clientX - box.left) / box.width;
    const py = (event.clientY - box.top) / box.height;
    card.style.setProperty("--kv-mx", `${(px * 100).toFixed(1)}%`);
    card.style.setProperty("--kv-my", `${(py * 100).toFixed(1)}%`);
    card.style.setProperty("--kv-ry", `${((px - 0.5) * 2 * MAX).toFixed(2)}deg`);
    card.style.setProperty("--kv-rx", `${((0.5 - py) * 2 * MAX).toFixed(2)}deg`);
  }, { passive: true });

  document.addEventListener("pointerout", (event) => {
    const card = event.target.closest(".kv-card");
    if (!card || card.contains(event.relatedTarget)) return;
    for (const prop of ["--kv-rx", "--kv-ry"]) card.style.removeProperty(prop);
  }, { passive: true });
}

/* ------------------------------------------------------------------ *
 * Mobile chrome: slide-in sidebar, dismissed on navigation
 * ------------------------------------------------------------------ */
function openSidebar() {
  const sidebar = $("#sidebar");
  if (sidebar) sidebar.dataset.open = "true";
}
function closeSidebar() {
  const sidebar = $("#sidebar");
  if (sidebar) sidebar.dataset.open = "false";
}
function initSidebar() {
  const menu = $("#menu-toggle");
  const sidebar = $("#sidebar");
  if (!menu || !sidebar) return;
  menu.addEventListener("click", () => {
    sidebar.dataset.open = sidebar.dataset.open === "true" ? "false" : "true";
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSidebar();
  });
}

/* ------------------------------------------------------------------ *
 * Onboarding: three steps, skippable, remembered locally
 * ------------------------------------------------------------------ */
function renderOnboardStep(step) {
  const clamped = Math.min(3, Math.max(1, step));
  $$(".kv-onboard-step").forEach((node) => { node.dataset.active = String(Number(node.dataset.step) === clamped); });
  $$(".kv-onboard-dot").forEach((node) => { node.dataset.active = String(Number(node.dataset.dot) === clamped); });
  $("#onboard-back").disabled = clamped === 1;
  $("#onboard-next").textContent = clamped === 3 ? "Start building ✦" : "Next";
  const field = $("#onboard-api");
  if (field && !field.value) field.value = store.base || "";
  return clamped;
}

function confetti() {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const host = document.createElement("div");
  host.className = "kv-confetti";
  const colours = ["#7c5cff", "#22d3ee", "#f472b6", "#a3e635", "#fb923c", "#fde047"];
  for (let i = 0; i < 34; i += 1) {
    const piece = document.createElement("span");
    piece.style.left = `${Math.random() * 100}%`;
    piece.style.background = colours[i % colours.length];
    piece.style.animationDelay = `${(Math.random() * 0.5).toFixed(2)}s`;
    piece.style.transform = `rotate(${Math.random() * 180}deg)`;
    host.append(piece);
  }
  document.body.append(host);
  setTimeout(() => host.remove(), 3400);
}

function closeOnboarding(remember = true) {
  const overlay = $("#onboard");
  if (overlay) overlay.hidden = true;
  if (remember) STORAGE.onboarded = true;
}

function openOnboarding() {
  const overlay = $("#onboard");
  if (!overlay) return;
  overlay.hidden = false;
  store.onboardStep = renderOnboardStep(1);
  $("#onboard-next").focus({ preventScroll: true });
}

function initOnboarding() {
  const overlay = $("#onboard");
  if (!overlay) return;
  $("#onboard-next").addEventListener("click", () => {
    if (store.onboardStep >= 3) {
      const typed = ($("#onboard-api") && $("#onboard-api").value.trim()) || "";
      if (typed && typed !== store.base) {
        store.base = typed.replace(/\/$/, "");
        STORAGE.api = store.base;
        $("#api-input").value = store.base;
        renderConnection();
        refreshAll();
      }
      closeOnboarding(true);
      confetti();
      toast("You are in. Create a project to start the first wave.", "ok");
      return;
    }
    store.onboardStep = renderOnboardStep(store.onboardStep + 1);
  });
  $("#onboard-back").addEventListener("click", () => { store.onboardStep = renderOnboardStep(store.onboardStep - 1); });
  $("#onboard-skip").addEventListener("click", () => closeOnboarding(true));
  const tour = $("#tour-start");
  if (tour) tour.addEventListener("click", openOnboarding);
  document.addEventListener("keydown", (event) => {
    if (overlay.hidden) return;
    if (event.key === "Escape") closeOnboarding(true);
    if (event.key === "Enter") $("#onboard-next").click();
  });
  if (!STORAGE.onboarded) setTimeout(() => openOnboarding(), 420);
}

function closeDrawer() {
  store.set({ openProject: null });
  stopStream();
  $("#drawer").dataset.open = "false";
  $("#drawer-scrim").dataset.open = "false";
}

/* ------------------------------------------------------------------ *
 * PWA: the installed-app path
 * ------------------------------------------------------------------ */

/** The deferred install prompt, when the browser offered one. */
let installPrompt = null;

/**
 * Register the service worker and wire up "Install app".
 *
 * The shell works offline and installs as a standalone window on desktop
 * (Chrome/Edge) and Android; iOS Safari has no install prompt, so it gets a
 * one-line hint instead. Nothing here is required for the dashboard to work.
 */
function initPwa() {
  if ("serviceWorker" in navigator && /^https?:$/.test(location.protocol)) {
    navigator.serviceWorker.register("sw.js").catch((error) => {
      console.warn("Service worker registration failed", error);
    });
  }

  const button = $("#install-app");
  const hint = $("#install-hint");
  if (!button) return;

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    button.hidden = false;
  });

  button.addEventListener("click", async () => {
    if (!installPrompt) {
      if (hint) hint.hidden = false;
      return;
    }
    installPrompt.prompt();
    try {
      const choice = await installPrompt.userChoice;
      if (choice && choice.outcome === "accepted") button.hidden = true;
    } catch (error) {
      console.warn("Install prompt failed", error);
    }
    installPrompt = null;
  });

  window.addEventListener("appinstalled", () => {
    button.hidden = true;
    if (hint) hint.hidden = true;
    toast("Installed — open Kollektiv from your app list", "ok");
  });

  const iOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  if (iOS && !standalone && hint) hint.hidden = false;
}

/* ------------------------------------------------------------------ *
 * Native shell bridge (Tauri)
 * ------------------------------------------------------------------ */

/**
 * What the desktop shell tells us about itself, when there is one.
 *
 * In a browser this stays null and nothing below runs: `web/` is the same static
 * site on Cloudflare Pages, on GitHub Pages and inside the desktop app. The
 * shell emits `kollektiv://shell-ready` with `{shell, version, bundled_api,
 * api_base, sidecar_note}` — `desktop/README.md` documents it.
 */
let shellInfo = null;
const IS_SHELL = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

/**
 * Remember what the shell said, and use the API it brought with it.
 *
 * A bundled install (the "bundle" variant) starts its own API on
 * 127.0.0.1:8765, so the dashboard points there once and never asks. A shell-only
 * install keeps whatever the user typed.
 */
function adoptShellInfo(info) {
  if (!info || info.shell !== "tauri") return;
  shellInfo = info;
  if (!STORAGE.api && info.bundled_api && info.api_base) {
    store.base = info.api_base;
    STORAGE.api = info.api_base;
  }
  const note = $("#shell-note");
  if (note) {
    note.hidden = !info.sidecar_note;
    note.textContent = info.sidecar_note || "";
  }
  renderConnection();
}

/** Ask the shell about itself (and listen, in case it announces first). */
function initShell() {
  if (!IS_SHELL) return;
  window.addEventListener("kollektiv://shell-ready", (event) => adoptShellInfo(event.detail));
  const internals = window.__TAURI_INTERNALS__;
  if (internals && typeof internals.invoke === "function") {
    Promise.resolve(internals.invoke("shell_info"))
      .then(adoptShellInfo)
      .catch((error) => console.warn("shell_info failed", error));
  }
}

/**
 * Open an external link in the user's real browser, from inside the shell.
 *
 * A desktop window that navigates away from the dashboard is a dead end: there
 * is no back button and no tab. Every http(s) target that is not this app goes to
 * the shell, which refuses anything that is not http(s) — see
 * `desktop/src-tauri/src/lib.rs`.
 */
function openExternal(url) {
  const internals = window.__TAURI_INTERNALS__;
  if (!IS_SHELL || !internals || typeof internals.invoke !== "function") return false;
  internals.invoke("open_external", { url }).catch((error) => console.warn("open_external failed", error));
  return true;
}

/** Keep the window title honest about the connection (taskbar, not banner). */
function reportConnection(state) {
  const internals = window.__TAURI_INTERNALS__;
  if (!IS_SHELL || !internals || typeof internals.invoke !== "function") return;
  internals.invoke("set_connection_state", { state }).catch(() => {});
}

/* ------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------ */
function boot() {
  if (STORAGE.theme) document.documentElement.dataset.theme = STORAGE.theme;
  store.base = guessBase();
  renderConnection();
  bindEvents();
  initTilt();
  initSidebar();
  initOnboarding();
  initPwa();
  initShell();
  navigate((location.hash || "#/overview").replace("#/", ""));
  refreshAll();

  // Gentle background refresh so a long-open dashboard stays honest.
  setInterval(() => {
    if (document.visibilityState === "visible") refreshAll();
  }, 30000);
}

document.addEventListener("DOMContentLoaded", boot);
