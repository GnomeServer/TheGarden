"use strict";

const state = {
  session: null,
  summary: null,
  tasks: [],
  workers: [],
  users: [],
  runs: [],
  activity: [],
  workerExecution: null,
  dashboardActivity: null,
  workSessions: null,
  nodeHealth: null,
  accessEvents: null,
  apiGuardIncidents: null,
  apiGuardIncidentsError: null,
  people: null,
  lastInteraction: 0,
  undoId: null,
  editingSessionId: null,
};

const $ = (id) => document.getElementById(id);
const sharedLinuxAccounts = new Set(["infra-lab-user", "df-server"]);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function formatDay(value) {
  if (!value) return "—";
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return esc(value);
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" }).format(date);
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function formatWorkerDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  return total < 60 ? `${total}s` : formatDuration(total);
}

function personLabel(user) {
  const login = String(user?.login || "");
  const name = String(user?.display_name || login);
  if (login.toLowerCase() === "api-admin" || /^api admin/i.test(name)) return "Service account";
  return name || login || "Unassigned";
}

function memberName(id) {
  if (id == null || id === "") return "Unassigned";
  const user = state.users.find((item) => String(item.id) === String(id));
  return user ? personLabel(user) : "Unknown member";
}

function workerDisplayLabel(workerId) {
  if (!workerId) return "Unassigned";
  const worker = (state.workers || []).find((item) =>
    item.id === workerId
    || item.reported_worker_ids?.includes(workerId)
    || item.legacy_worker_ids?.includes(workerId)
  );
  if (!worker) return workerId;
  const name = worker.display_name || worker.id;
  if (workerId !== worker.id) return `${name} (legacy ID ${workerId})`;
  return name === workerId ? name : `${name} (${worker.id})`;
}

function utcDateValue(date) {
  return date.toISOString().slice(0, 10);
}

function toLocalInput(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fromLocalInput(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}


const THEME_STORAGE_KEY = "thegarden-theme";

function setTheme(theme, persist = true) {
  const selected = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = selected;
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    const dark = selected === "dark";
    button.textContent = dark ? "Light mode" : "Dark mode";
    button.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    button.setAttribute("aria-pressed", String(dark));
  });
  document.querySelector("#theme-color")?.setAttribute("content", selected === "dark" ? "#12151c" : "#e7eef6");
  if (persist) {
    try { localStorage.setItem(THEME_STORAGE_KEY, selected); } catch {}
  }
}

function initTheme() {
  let selected = "";
  try { selected = localStorage.getItem(THEME_STORAGE_KEY) || ""; } catch {}
  if (!["light", "dark"].includes(selected)) selected = "dark";
  setTheme(selected, false);
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  });
}

initTheme();

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (!['GET', 'HEAD'].includes(method) && state.session?.csrf_token) {
    headers["X-CSRF-Token"] = state.session.csrf_token;
  }

  const response = await fetch(path, { ...options, method, headers });
  if (response.status === 204) {
    const undoId = response.headers.get("X-Undo-ID");
    return undoId ? { undo_id: undoId } : null;
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(`Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function setError(container, message) {
  container.innerHTML = `<p class="state-message error-state" role="alert">${esc(message)}</p>`;
}

function setEmpty(container, message) {
  container.innerHTML = `<p class="state-message empty-state">${esc(message)}</p>`;
}

function showToast(message, undoId = null) {
  const node = $("toast");
  const messageNode = $("toast-message");
  const button = $("toast-undo");
  messageNode.textContent = message;
  state.undoId = typeof undoId === "string" && undoId ? undoId : null;
  button.classList.toggle("hidden", !state.undoId);
  button.disabled = false;
  node.classList.remove("hidden");
  clearTimeout(node.timer);
  const activeUndo = state.undoId;
  node.timer = setTimeout(() => {
    node.classList.add("hidden");
    if (state.undoId === activeUndo) state.undoId = null;
  }, activeUndo ? 10000 : 3000);
}

async function undoLastAction() {
  const undoId = state.undoId;
  if (!undoId) return;
  state.undoId = null;
  $("toast-undo").disabled = true;
  clearTimeout($("toast").timer);
  try {
    await api(`/v1/undo/${encodeURIComponent(undoId)}`, { method: "POST" });
    $("toast").classList.add("hidden");
    await refreshAll();
    showToast("Action undone");
  } catch (error) {
    if (!handleAuthError(error)) {
      showToast(error.status === 409 || error.status === 410
        ? "This action can no longer be undone."
        : error.message);
    }
  } finally {
    $("toast-undo").disabled = false;
  }
}

function showLogin(message = "") {
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("login-error").textContent = message;
}

function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  const user = state.session.user;
  $("identity").textContent = user.display_name || user.login || "Signed in";
}

async function configureAuth() {
  const link = $("oauth-login");
  const message = $("oauth-status");
  try {
    const config = await api("/v1/auth/config");
    if (config?.forgejo_oauth) {
      link.classList.remove("hidden");
      message.classList.add("hidden");
      return;
    }
    link.classList.add("hidden");
    message.textContent = "Forgejo sign-in is not configured for this deployment. Contact your administrator.";
    message.classList.remove("hidden");
  } catch {
    link.classList.add("hidden");
    message.textContent = "Forgejo sign-in configuration could not be reached. Reload the page or contact your administrator.";
    message.classList.remove("hidden");
  }
}

function handleAuthError(error) {
  if (error.status !== 401) return false;
  state.session = null;
  showLogin("Your session has expired. Sign in again to continue.");
  return true;
}

function isAdministrator() {
  if (state.session?.user?.role === "admin") return true;
  return (state.users || []).some((user) =>
    String(user.id) === String(state.session?.user?.id) && user.role === "admin",
  );
}

function taskQuery() {
  const query = new URLSearchParams();
  if ($("task-status-filter").value) {
    query.set("status", $("task-status-filter").value);
  }
  const value = query.toString();
  return `/v1/tasks${value ? `?${value}` : ""}`;
}

async function refreshAll() {
  if (!state.session) return;
  const dashboardFrom = $("dashboard-time-from").value;
  const dashboardTo = $("dashboard-time-to").value;
  const results = await Promise.allSettled([
    api("/v1/dashboard/summary"),
    api(taskQuery()),
    api("/v1/users"),
    api("/v1/workers"),
    api("/v1/dashboard/runs?limit=100"),
    api("/v1/activity?limit=150"),
    api(`/v1/activity/estimates?from=${encodeURIComponent(dashboardFrom)}&to=${encodeURIComponent(dashboardTo)}`),
    api("/v1/nodes/health"),
    api("/v1/access-events?limit=100"),
    api("/v1/work-sessions?limit=100"),
    api("/v1/people/summary"),
  ]);

  const unauthorized = results.find(
    (result) => result.status === "rejected" && result.reason?.status === 401,
  );
  if (unauthorized) {
    handleAuthError(unauthorized.reason);
    return;
  }

  const keys = [
    "summary",
    "tasks",
    "users",
    "workers",
    "runs",
    "activity",
    "dashboardActivity",
    "nodeHealth",
    "accessEvents",
    "workSessions",
    "people",
  ];
  results.forEach((result, index) => {
    const key = keys[index];
    if (result.status === "fulfilled") {
      state[key] = result.value;
      state[`${key}Error`] = null;
    } else {
      state[`${key}Error`] = result.reason;
    }
  });

  if (isAdministrator()) {
    const adminResults = await Promise.allSettled([
      api("/v1/identity-mappings"),
      api("/v1/api-guard/incidents?limit=100"),
    ]);
    const unauthorizedAdminResult = adminResults.find(
      (result) => result.status === "rejected" && result.reason?.status === 401,
    );
    if (unauthorizedAdminResult) {
      handleAuthError(unauthorizedAdminResult.reason);
      return;
    }
    state.identityMappings = adminResults[0].status === "fulfilled" ? adminResults[0].value : null;
    state.identityMappingsError = adminResults[0].status === "rejected" ? adminResults[0].reason : null;
    state.apiGuardIncidents = adminResults[1].status === "fulfilled" ? adminResults[1].value : null;
    state.apiGuardIncidentsError = adminResults[1].status === "rejected" ? adminResults[1].reason : null;
  } else {
    state.identityMappings = null;
    state.identityMappingsError = null;
    state.apiGuardIncidents = null;
    state.apiGuardIncidentsError = null;
  }

  const partial = results.some((result) => result.status === "rejected") ||
    Boolean(state.identityMappingsError) || Boolean(state.apiGuardIncidentsError);
  $("connection").textContent = partial
    ? "Connected · some data unavailable"
    : "Connected";
  $("connection").classList.toggle("live", !partial);
  render();

}


function render() {
  renderSummary();
  renderTasks();
  renderAgentTasks();
  renderExternalLinks();
  populateAssignments();
  renderWorkers();
  renderRuns();
  renderActivity();
  renderDashboardActivity();
  renderNodes();
  renderAccess();
  renderIdentityMappings();
  renderAccessGrants();
  renderWorkSessions();
  renderPresence();
  renderApiGuardIncidents();
}

function accessChoice(user) {
  if (user.active === false || user.role === "pending") return "none";
  if (user.role === "admin") return "admin";
  if (user.role === "manager") return "manager";
  if (user.role === "viewer") return "viewer";
  return "member";
}

function renderAccessGrants() {
  const panel = $("access-admin");
  const target = $("access-requests");
  if (!panel || !target) return;
  const canChange = ["admin", "manager", "member"].includes(state.session?.user?.role);
  panel.classList.toggle("hidden", !canChange);
  if (!canChange) return;
  if (state.usersError) {
    setError(target, "People could not be loaded.");
    return;
  }
  if (!state.users) {
    target.innerHTML = '<p class="loading">Loading people…</p>';
    return;
  }
  const people = state.users.filter((user) => String(user.login).toLowerCase() !== "api-admin" && user.role !== "service");
  if (!people.length) {
    setEmpty(target, "No Forgejo sign-ins yet.");
    return;
  }
  const options = [
    ["admin", "Admin"],
    ["manager", "Manager"],
    ["member", "See and edit"],
    ["viewer", "See only"],
    ["none", "No access"],
  ];
  target.innerHTML = people.map((user) => {
    const selected = accessChoice(user);
    const self = String(user.id) === String(state.session.user.id);
    return `
      <article class="identity-card">
        <div>
          <strong class="identity-name">${esc(personLabel(user))}</strong>
          <small>${esc(user.login)}</small>
        </div>
        <label>Status
          <select data-access-user="${esc(user.id)}" ${self ? "disabled" : ""}>
            ${options.map(([value, label]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </label>
      </article>
    `;
  }).join("");
}

function externalDeliveryLabel(status) {
  if (status === "delivered") return "Delivered to the configured webhook; this is not a human acknowledgment.";
  if (status === "pending") return "Queued for external delivery.";
  if (status === "failed") return "External delivery failed; automatic retry is queued.";
  return "External notification delivery is not configured.";
}

function ackCallbackLabel(status) {
  if (status === "delivered") return "Acknowledgment callback delivered to API Guard.";
  if (status === "pending") return "Acknowledgment callback is queued for API Guard.";
  if (status === "failed") return "API Guard callback failed; automatic retry is queued.";
  if (status === "unconfigured") return "API Guard acknowledgment callback is not configured.";
  return "No acknowledgment callback has been queued.";
}

function escalationLabel(incident) {
  const status = incident.escalation_status;
  const level = Number(incident.escalation_level) || 0;
  const attempts = Number(incident.escalation_attempts) || 0;
  const next = incident.escalation_next_attempt_at
    ? ` Next attempt: ${formatDate(incident.escalation_next_attempt_at)}.`
    : "";
  if (status === "acknowledged") return "Stopped after human acknowledgment.";
  if (status === "unconfigured") return "External notification delivery is not configured; no-ack escalation is paused.";
  if (status === "waiting_for_delivery") return "Waiting for initial external delivery before starting no-ack escalation.";
  if (status === "pending") return `Escalation level ${level} delivery attempt ${attempts} is in progress.${next}`;
  if (status === "failed") return `Escalation level ${level} delivery failed; independent retry backoff is active.${next}`;
  if (status === "scheduled") {
    return level
      ? `Escalation level ${level} delivered; next no-ack escalation is scheduled.${next}`
      : `First no-ack escalation is scheduled.${next}`;
  }
  return "No-ack escalation is not scheduled.";
}

function renderApiGuardIncidents() {
  const panel = $("api-guard-incidents");
  const target = $("api-guard-incident-list");
  if (!panel || !target) return;
  const admin = isAdministrator();
  panel.classList.toggle("hidden", !admin);
  if (!admin) return;
  if (state.apiGuardIncidentsError) {
    setError(target, "API Guard incidents could not be loaded.");
    return;
  }
  if (!state.apiGuardIncidents) {
    target.innerHTML = '<p class="loading">Loading API Guard incidents…</p>';
    return;
  }
  if (!state.apiGuardIncidents.length) {
    setEmpty(target, "No API Guard exposure incidents have been received.");
    return;
  }
  target.innerHTML = state.apiGuardIncidents.map((incident) => {
    const acknowledged = Boolean(incident.acknowledged_at);
    const identifiers = [
      incident.worker_id ? `Worker ${workerDisplayLabel(incident.worker_id)}` : "",
      incident.session_id ? `Session ${incident.session_id}` : "",
    ].filter(Boolean).join(" · ");
    return `
      <article class="identity-card incident-card">
        <div>
          <strong class="identity-name">${esc(incident.key_alias)} · ${esc(incident.reason.replaceAll("_", " "))}</strong>
          <small>Event ${esc(incident.event_id)}${identifiers ? ` · ${esc(identifiers)}` : ""}</small>
        </div>
        <p><strong>Received:</strong> Agent Manager recorded this incident at ${esc(formatDate(incident.received_at))}.</p>
        <p><strong>External delivery:</strong> ${esc(externalDeliveryLabel(incident.notification_status))}</p>
        <p><strong>No-ack escalation:</strong> ${esc(escalationLabel(incident))}</p>
        <p><strong>Human acknowledgment:</strong> ${acknowledged
          ? `Acknowledged by ${esc(incident.acknowledged_by)} at ${esc(formatDate(incident.acknowledged_at))}.`
          : "Not acknowledged."}</p>
        ${acknowledged ? `<p><strong>API Guard callback:</strong> ${esc(ackCallbackLabel(incident.ack_callback_status))}</p>` : ""}
        <small>Exposure reported ${esc(formatDate(incident.occurred_at))}</small>
        ${acknowledged ? "" : `<button class="button danger" type="button" data-incident-ack="${esc(incident.event_id)}">Acknowledge after handling</button>`}
      </article>
    `;
  }).join("");
}

async function savePerson(userId, fields) {
  const user = (state.users || []).find((item) => String(item.id) === String(userId));
  const choice = fields.choice || accessChoice(user || {});
  const granted = choice !== "none";
  await api(`/v1/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    body: JSON.stringify({
      role: granted ? choice : "viewer",
      active: granted,
      display_name: fields.display_name,
    }),
  });
  showToast(fields.display_name ? "Name saved" : "Status updated");
  await refreshAll();
}

function recentPresence(value, windowMs = 180000) {
  const time = value ? new Date(value).getTime() : NaN;
  return Number.isFinite(time) && Date.now() - time <= windowMs;
}

function renderPresence() {
  const people = $("people-channels");
  const workers = $("worker-channels");
  if (!people && !workers) return;
  const timers = new Map((state.people || [])
    .filter((item) => item.active_session)
    .map((item) => [String(item.user?.id), item.active_session]));
  if (people) {
  if (state.usersError) {
    setError(people, "People could not be loaded.");
  } else if (!state.users) {
    people.innerHTML = '<p class="loading">Loading people…</p>';
  } else if (!state.users.length) {
    setEmpty(people, "No people yet.");
  } else {
    const canEdit = ["admin", "manager", "member"].includes(state.session?.user?.role);
    const options = [["admin", "Admin"], ["manager", "Manager"], ["member", "See and edit"], ["viewer", "See only"], ["none", "No access"]];
    const roster = state.users.filter((user) => user.role !== "service" && String(user.login).toLowerCase() !== "api-admin");
    people.innerHTML = roster.map((user) => {
      const online = user.active !== false && recentPresence(user.last_activity_at);
      const self = String(user.id) === String(state.session?.user?.id);
      const selected = accessChoice(user);
      const timer = timers.get(String(user.id));
      return `
        <article class="person-widget ${online ? "active" : "away"}">
          <span class="channel-dot" aria-hidden="true"></span>
          <input data-person-name="${esc(user.id)}" value="${esc(personLabel(user))}" maxlength="255" aria-label="Name for ${esc(user.login)}" ${canEdit ? "" : "disabled"}>
          <select data-access-user="${esc(user.id)}" aria-label="Role for ${esc(personLabel(user))}" ${canEdit && !self ? "" : "disabled"}>
            ${options.map(([value, label]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`).join("")}
          </select>
          <button class="button secondary" type="button" data-save-name="${esc(user.id)}" ${canEdit ? "" : "disabled"}>Save</button>
          <button class="button danger" type="button" data-delete-user="${esc(user.id)}" ${canEdit && !self ? "" : "disabled"}>Delete</button>
        </article>
      `;
    }).join("");
  }
  }
  if (!workers) return;
  if (state.workersError) {
    setError(workers, "Agents could not be loaded.");
  } else if (!state.workers) {
    workers.innerHTML = '<p class="loading">Loading agents…</p>';
  } else if (!state.workers.length) {
    setEmpty(workers, "No agent heartbeats yet.");
  } else {
    const roster = state.workers.filter((worker) => worker.is_roster_member !== false);
    const legacy = state.workers.filter((worker) => worker.is_roster_member === false);
    workers.innerHTML = roster.map((worker) => {
      const offline = worker.presence_status === "offline" || worker.status === "offline";
      const run = (state.runs || []).find((item) => item.id === worker.current_run_id);
      const detail = !worker.has_record || worker.has_canonical_identity === false
        ? worker.enrollment_status
        : run?.phase || (worker.current_run_id ? "On a run" : offline ? "Offline" : "Idle");
      return `<article class="channel ${offline ? "away" : "active"}"><span class="channel-dot" aria-hidden="true"></span><strong>${esc(worker.display_name || worker.id)}</strong><small>${esc(worker.id)} · ${esc(detail)}</small></article>`;
    }).join("") + (legacy.length
      ? `<p class="channel-legacy-note">Unregistered heartbeat IDs retained separately: ${legacy.map((worker) => esc(worker.id)).join(", ")}</p>`
      : "");
  }
}

function metric(label, value, detail) {
  return `<article class="metric"><strong>${esc(value ?? 0)}</strong><span>${esc(label)}</span><small>${esc(detail)}</small></article>`;
}

function renderSummary() {
  const target = $("summary");
  if (state.summaryError) {
    setError(target, "Team summary is temporarily unavailable.");
    return;
  }
  if (!state.summary) {
    target.innerHTML = '<p class="loading">Loading team summary…</p>';
    return;
  }
  const summary = state.summary;
  target.innerHTML = [
    metric("To do", summary.tasks?.todo, "tasks"),
    metric("In progress", summary.tasks?.in_progress, "tasks"),
    metric("Blocked", summary.tasks?.blocked, "tasks"),
    metric("Workers online", summary.workers?.online, "heartbeat presence"),
    metric("Worker time today", formatWorkerDuration(summary.worker_execution_seconds_today), `${summary.worker_runs_today || 0} runs · elapsed execution`),
  ].join("");
}

function renderTasks() {
  const target = $("task-board");
  if (state.tasksError) {
    setError(target, "Tasks could not be loaded. Try again shortly.");
    return;
  }
  if (!state.tasks) {
    target.innerHTML = '<p class="loading">Loading tasks…</p>';
    return;
  }
  const visible = state.tasks.filter((task) => !task.assignee_worker_id);
  if (!visible.length) {
    setEmpty(target, "No human tasks match this view. Create a task to get started.");
    return;
  }

  const columns = [
    ["todo", "To do"],
    ["in_progress", "In progress"],
    ["blocked", "Blocked"],
    ["done", "Done"],
    ["cancelled", "Cancelled"],
  ];
  target.innerHTML = columns.map(([status, label]) => {
    const tasks = visible.filter((task) => task.status === status);
    const cards = tasks.map((task) => `
      <article class="task-card">
        <button class="task-open" type="button" data-task-id="${esc(task.id)}" aria-label="Edit task: ${esc(task.title)}">
          <span class="task-title">${esc(task.title)}</span>
          <span class="task-meta">
            <span class="badge priority-${esc(task.priority)}">${esc(task.priority || "normal")}</span>
            <span>${esc(memberName(task.assignee_user_id))}</span>
            ${task.assignee_worker_id ? `<span>Worker ${esc(workerDisplayLabel(task.assignee_worker_id))}</span>` : ""}
            ${task.due_at ? `<time datetime="${esc(task.due_at)}">Due ${esc(formatDate(task.due_at))}</time>` : ""}
          </span>
          ${task.description ? `<span class="task-description">${esc(task.description)}</span>` : ""}
        </button>
      </article>
    `).join("");
    return `
      <section class="task-column" aria-label="${esc(label)} tasks">
        <h3>${esc(label)}<span class="count">${tasks.length}</span></h3>
        ${cards || '<p class="empty-column">Nothing here yet.</p>'}
      </section>
    `;
  }).join("");
}

function renderAgentTasks() {
  const target = $("agent-task-board");
  if (!target) return;
  if (state.tasksError) {
    setError(target, "Agent tasks could not be loaded.");
    return;
  }
  if (!state.tasks) {
    target.innerHTML = '<p class="loading">Loading agent tasks…</p>';
    return;
  }
  const statusFilter = $("agent-task-status-filter")?.value || "";
  const visible = state.tasks.filter((task) => task.assignee_worker_id && (!statusFilter || task.status === statusFilter));
  if (!visible.length) {
    setEmpty(target, "No agent tasks match this filter. Assign a worker on the TODO board to route a task here.");
    return;
  }
  const columns = [
    ["todo", "To do"],
    ["in_progress", "In progress"],
    ["blocked", "Blocked"],
    ["done", "Complete"],
    ["cancelled", "Cancelled"],
  ];
  target.innerHTML = columns.map(([status, label]) => {
    const tasks = visible.filter((task) => task.status === status);
    const cards = tasks.map((task) => {
      const run = (state.runs || []).find((item) => item.id === task.run_id);
      return `
      <article class="task-card">
        <button class="task-open" type="button" data-task-id="${esc(task.id)}" aria-label="Edit agent task: ${esc(task.title)}">
          <span class="task-title">${esc(task.title)}</span>
          <span class="task-meta">
            <span class="badge priority-${esc(task.priority)}">${esc(task.priority || "normal")}</span>
            <span>Worker ${esc(workerDisplayLabel(task.assignee_worker_id))}</span>
            <span>${esc(memberName(task.assignee_user_id))}</span>
            ${run ? `<span>${esc(run.phase || run.status)}</span>` : ""}
          </span>
        </button>
      </article>
    `;
    }).join("");
    return `
      <section class="task-column" aria-label="${esc(label)} agent tasks">
        <h3>${esc(label)}<span class="count">${tasks.length}</span></h3>
        ${cards || '<p class="empty-column">Nothing here yet.</p>'}
      </section>
    `;
  }).join("");
}

function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
  } catch {
    return "";
  }
}

function setExternalLink(id, url) {
  const link = $(id);
  if (!link) return;
  const safe = safeHttpUrl(url);
  link.classList.toggle("hidden", !safe);
  if (safe) link.href = safe;
}

function renderExternalLinks() {
  const overview = state.nodeHealth?.grafana_url;
  setExternalLink("grafana-overview-link", overview);
  if (overview) {
    const url = new URL(overview);
    const marker = url.pathname.indexOf("/d/");
    const prefix = marker >= 0 ? url.pathname.slice(0, marker) : url.pathname.replace(/\/+$/, "");
    url.pathname = `${prefix}/d/nats-jetstream/nats-jetstream`;
    url.search = "";
    url.hash = "";
    url.searchParams.set("orgId", "1");
    url.searchParams.set("from", "now-6h");
    url.searchParams.set("to", "now");
    setExternalLink("grafana-jetstream-link", url.href);
  } else {
    setExternalLink("grafana-jetstream-link", "");
  }
}

function populateAssignments() {
  const userSelect = $("task-user");
  const workerSelect = $("task-worker");
  const oldUser = userSelect.value;
  const oldWorker = workerSelect.value;
  userSelect.innerHTML = '<option value="">Unassigned</option>' + (state.users || [])
    .filter((user) => user.active !== false)
    .map((user) => `<option value="${esc(user.id)}">${esc(user.display_name || user.login)}</option>`)
    .join("");
  const assignableWorkers = (state.workers || []).filter((worker) =>
    worker.is_roster_member !== false && worker.assignable !== false
  );
  const workerOptions = assignableWorkers
    .map((worker) => `<option value="${esc(worker.id)}">${esc(worker.display_name || worker.id)} (${esc(worker.id)}) · ${esc(worker.role || "worker")}</option>`);
  if (oldWorker && !assignableWorkers.some((worker) => worker.id === oldWorker)) {
    workerOptions.unshift(
      `<option value="${esc(oldWorker)}">Existing legacy assignment · ${esc(workerDisplayLabel(oldWorker))}</option>`,
    );
  }
  workerSelect.innerHTML = '<option value="">Unassigned</option>' + workerOptions.join("");
  userSelect.value = oldUser;
  workerSelect.value = oldWorker;
}

function toLocalDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}

function openTask(task = null) {
  $("task-form").reset();
  $("task-error").textContent = "";
  $("task-id").value = task?.id || "";
  $("task-version").value = task?.version ?? "";
  $("task-form-title").textContent = task ? "Edit task" : "Create a task";
  $("task-title").value = task?.title || "";
  $("task-description").value = task?.description || "";
  $("task-status").value = task?.status || "todo";
  $("task-priority").value = task?.priority || "normal";
  $("task-due").value = task?.due_at ? toLocalDateTime(task.due_at) : "";
  $("task-user").value = task?.assignee_user_id || "";
  $("task-worker").value = task?.assignee_worker_id || "";
  $("archive-task").classList.toggle("hidden", !task);
  $("task-dialog").showModal();
  $("task-title").focus();
}

function taskPayload() {
  const due = $("task-due").value;
  return {
    title: $("task-title").value.trim(),
    description: $("task-description").value.trim(),
    status: $("task-status").value,
    priority: $("task-priority").value,
    due_at: due ? new Date(due).toISOString() : null,
    assignee_user_id: $("task-user").value || null,
    assignee_worker_id: $("task-worker").value || null,
  };
}

async function saveTask(event) {
  event.preventDefault();
  const id = $("task-id").value;
  const payload = taskPayload();
  if (id) payload.version = Number($("task-version").value);
  const button = $("save-task");
  button.disabled = true;
  try {
    const result = await api(id ? `/v1/tasks/${encodeURIComponent(id)}` : "/v1/tasks", {
      method: id ? "PATCH" : "POST",
      body: JSON.stringify(payload),
    });
    $("task-dialog").close();
    showToast(id ? "Task updated" : "Task created", result?.undo_id);
    state.tasksError = null;
    await refreshAll();
  } catch (error) {
    $("task-error").textContent = error.status === 409
      ? "This task changed elsewhere. Close and reopen it before saving."
      : error.message;
  } finally {
    button.disabled = false;
  }
}

async function archiveTask() {
  const id = $("task-id").value;
  if (!id || !window.confirm("Archive this task?")) return;
  try {
    const result = await api(`/v1/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
    $("task-dialog").close();
    showToast("Task archived", result?.undo_id);
    state.tasksError = null;
    await refreshAll();
  } catch (error) {
    $("task-error").textContent = error.message;
  }
}
function admissionInfo(worker) {
  const value = worker.admission_status;
  if (value == null || value === "") {
    return { label: "Unverified", kind: "unverified" };
  }
  if (value === true) return { label: "Ready", kind: "ready" };
  if (value === false) return { label: "Not ready", kind: "unready" };
  if (typeof value !== "string") {
    return { label: "Unverified", kind: "unverified" };
  }

  const normalized = value.toLowerCase();
  if (["unknown", "unverified", "not_checked"].includes(normalized)) {
    return { label: "Unverified", kind: "unverified" };
  }
  if (["ready", "available", "admitted"].includes(normalized)) {
    return { label: value.replaceAll("_", " "), kind: "ready" };
  }
  if (["not_ready", "unavailable", "disabled", "blocked"].includes(normalized)) {
    return { label: value.replaceAll("_", " "), kind: "unready" };
  }
  return { label: value.replaceAll("_", " "), kind: "reported" };
}

function renderWorkers() {
  const target = $("workers");
  if (state.workersError) {
    setError(target, "Worker presence could not be loaded. Try again shortly.");
    return;
  }
  if (!state.workers) {
    target.innerHTML = '<p class="loading">Loading workers…</p>';
    return;
  }
  if (!state.workers.length) {
    setEmpty(target, "No worker heartbeats have been recorded.");
    return;
  }
  const roster = state.workers.filter((worker) => worker.is_roster_member !== false);
  const legacy = state.workers.filter((worker) => worker.is_roster_member === false);
  const cards = roster.map((worker) => {
    const readiness = admissionInfo(worker);
    const heartbeat = !worker.has_record
      ? (worker.ansible_managed ? "Awaiting first heartbeat" : "Not provisioned")
      : worker.has_canonical_identity === false
        ? "Legacy worker identity"
        : `Heartbeat · ${worker.status || "unknown"}`;
    const legacyIds = Array.isArray(worker.legacy_worker_ids) && worker.legacy_worker_ids.length
      ? `<small>Other stored IDs: ${esc(worker.legacy_worker_ids.join(", "))}</small>`
      : "";
    const legacyOnline = Array.isArray(worker.legacy_online_worker_ids) && worker.legacy_online_worker_ids.length
      ? `<p class="notice">Legacy worker ID(s) still heartbeating: ${esc(worker.legacy_online_worker_ids.join(", "))}. Migrate them before retiring the old process.</p>`
      : "";
    return `
      <article class="worker-card">
        <header>
          <div><p class="eyebrow">${esc(worker.role || "WORKER")}</p><h3>${esc(worker.display_name || worker.id)}</h3><small>${esc(worker.id)}</small></div>
          <span class="badge status-${esc(worker.status || "unknown")}">${esc(heartbeat)}</span>
        </header>
        <dl>
          <div><dt>Host</dt><dd>${esc(worker.hostname || "—")}</dd></div>
          <div><dt>Tailnet IP</dt><dd>${esc(worker.tailnet_ip || "—")}</dd></div>
          <div><dt>Enrollment</dt><dd>${esc(worker.enrollment_status || "Heartbeat record")}</dd></div>
          <div><dt>Last heartbeat</dt><dd>${esc(formatDate(worker.last_seen_at))}</dd></div>
          <div><dt>Current run</dt><dd>${esc(worker.current_run_id || "None")}</dd></div>
          <div><dt>Capabilities</dt><dd>${esc(Array.isArray(worker.capabilities) ? worker.capabilities.join(", ") : "—")}</dd></div>
          <div><dt>Run-admission readiness</dt><dd><span class="badge readiness-${readiness.kind}">${esc(readiness.label)}</span></dd></div>
        </dl>
        ${legacyIds}
        ${legacyOnline}
        <p class="worker-footnote">Heartbeat is presence only; it does not prove model access or host-wide security.</p>
      </article>
    `;
  }).join("");
  const legacySection = legacy.length
    ? `<details class="legacy-worker-records"><summary>Unregistered/legacy heartbeat IDs (${legacy.length})</summary>${legacy.map((worker) => `<p>${esc(worker.display_name || worker.id)} · ${esc(worker.hostname || "unknown host")} · ${esc(worker.status || "unknown")}</p>`).join("")}</details>`
    : "";
  target.innerHTML = cards + legacySection;
}

function runStatus(run) {
  const raw = String(run.status || "unknown");
  const status = raw.toLowerCase();
  if (status.includes("reject") || status.includes("unavailable")) {
    return { label: `Not queued · ${raw}`, kind: "rejected" };
  }
  if (status === "queued") return { label: "Accepted · queued", kind: "queued" };
  if (status === "running") return { label: "Running", kind: "running" };
  return { label: raw, kind: status };
}

function renderRuns() {
  const target = $("runs");
  if (state.runsError) {
    setError(target, "Run history could not be loaded. Try again shortly.");
    return;
  }
  if (!state.runs) {
    target.innerHTML = '<p class="loading">Loading runs…</p>';
    return;
  }
  if (!state.runs.length) {
    setEmpty(target, "No agent runs recorded.");
    return;
  }
  const rows = state.runs.map((run) => {
    const status = runStatus(run);
    const source = run.source === "open-webui" ? "Open WebUI" : run.source === "api" ? "API" : "Unknown";
    return `
      <tr>
        <td><time datetime="${esc(run.created_at)}">${esc(formatDate(run.created_at))}</time></td>
        <td>${esc(run.request_summary || "No summary provided")}${run.script_name ? `<small class="subline">${esc(run.script_name)}</small>` : ""}</td>
        <td>${esc(source)}${run.operation ? `<small class="subline">${esc(run.operation)}</small>` : ""}</td>
        <td>${esc(run.phase || "—")}${Array.isArray(run.chatter) && run.chatter.length ? `<ol class="chatter">${run.chatter.map((line) => `<li>${esc(line)}</li>`).join("")}</ol>` : ""}${run.bounce ? `<small class="subline">Bounce: ${esc(run.bounce)}</small>` : ""}${run.result_summary ? `<small class="subline">${esc(run.result_summary)}</small>` : ""}</td>
        <td>${esc(run.worker_role || "—")}${run.worker_id ? `<small class="subline">${esc(workerDisplayLabel(run.worker_id))}</small>` : ""}</td>
        <td>${typeof run.duration_seconds === "number" ? esc(formatWorkerDuration(run.duration_seconds)) : "—"}</td>
        <td><span class="badge run-${esc(status.kind)}">${esc(status.label)}</span></td>
        <td>${esc(run.error_summary || "—")}</td>
      </tr>
    `;
  }).join("");
  target.innerHTML = `
    <table>
      <thead><tr><th scope="col">Created</th><th scope="col">Request</th><th scope="col">From</th><th scope="col">Step</th><th scope="col">Worker</th><th scope="col">Run time</th><th scope="col">State</th><th scope="col">Result</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderActivity() {
  const target = $("activity");
  if (state.activityError) {
    setError(target, "Activity could not be loaded. Try again shortly.");
    return;
  }
  if (!state.activity) {
    target.innerHTML = '<p class="loading">Loading activity…</p>';
    return;
  }
  if (!state.activity.length) {
    setEmpty(target, "No activity has been recorded yet.");
    return;
  }
  target.innerHTML = state.activity.map((event) => `
    <article class="activity-item">
      <time datetime="${esc(event.occurred_at)}">${esc(formatDate(event.occurred_at))}</time>
      <div>
        <p>${esc(event.summary || "Activity event")}</p>
        <small>${esc(event.source || "System")} · ${esc(event.event_type || "event")}${event.actor_user_id ? ` · ${esc(memberName(event.actor_user_id))}` : ""}</small>
      </div>
    </article>
  `).join("");
}

function renderNodes() {
  const target = $("node-health");
  if (state.nodeHealthError) {
    setError(target, "Node health could not be loaded.");
    return;
  }
  if (!state.nodeHealth) {
    target.innerHTML = '<p class="loading">Loading node health…</p>';
    return;
  }
  if (!state.nodeHealth.available) {
    setEmpty(target, "Node health monitoring is currently unavailable.");
    return;
  }
  const nodes = state.nodeHealth.nodes || [];
  if (!nodes.length) {
    setEmpty(target, "No nodes are available.");
    return;
  }
  const rows = nodes.map((node) => `
    <tr>
      <td>${esc(node.node_id || "—")}</td>
      <td><span class="badge ${node.up ? "status-online" : "status-offline"}">${node.up ? "Reachable" : "Unreachable"}</span></td>
      <td>${node.cpu_percent == null ? "—" : `${esc(node.cpu_percent)}%`}</td>
      <td>${node.memory_percent == null ? "—" : `${esc(node.memory_percent)}%`}</td>
      <td>${node.disk_percent == null ? "—" : `${esc(node.disk_percent)}%`}</td>
    </tr>
  `).join("");
  target.innerHTML = `
    <table>
      <thead><tr><th scope="col">Node</th><th scope="col">Reachability</th><th scope="col">CPU</th><th scope="col">Memory</th><th scope="col">Disk</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderAccess() {
  const target = $("access-events");
  if (state.accessEventsError) {
    setError(target, "Access events could not be loaded.");
    return;
  }
  if (!state.accessEvents) {
    target.innerHTML = '<p class="loading">Loading access events…</p>';
    return;
  }
  if (!state.accessEvents.length) {
    setEmpty(target, "No access events recorded.");
    return;
  }
  const rows = state.accessEvents.map((event) => `
    <tr>
      <td>${esc(formatDate(event.occurred_at))}</td>
      <td>${esc(event.node_id || "—")}</td>
      <td>${esc(event.user_id ? memberName(event.user_id) : "Unmapped")}</td>
      <td>${esc(event.source || "—")}</td>
      <td>${esc(event.event_type || "—")}</td>
      <td>${esc(event.service || "—")}</td>
    </tr>
  `).join("");
  target.innerHTML = `
    <table>
      <thead><tr><th scope="col">Time</th><th scope="col">Node</th><th scope="col">Person</th><th scope="col">Source</th><th scope="col">Event</th><th scope="col">Service</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function taskTitle(taskId) {
  if (!taskId) return "No linked task";
  return state.tasks.find((task) => String(task.id) === String(taskId))?.title || "Unknown task";
}

function renderWorkSessions() {
  const target = $("work-sessions");
  const current = $("timer-current");
  const form = $("timer-form");
  const startButton = $("start-time");
  const ownRunning = (state.workSessions || []).find((entry) =>
    String(entry.user_id) === String(state.session.user.id) && entry.status === "running",
  );

  if (state.workSessionsError) {
    setError(current, "Reported-time entries could not be loaded.");
    setError(target, "Reported-time entries could not be loaded.");
    startButton.disabled = true;
  } else if (!state.workSessions) {
    current.innerHTML = '<p class="loading">Loading reported time…</p>';
    target.innerHTML = '<p class="loading">Loading reported entries…</p>';
    startButton.disabled = true;
  } else {
    current.innerHTML = ownRunning
      ? `<p><strong>Timer running.</strong> ${esc(taskTitle(ownRunning.task_id))} · started ${esc(formatDate(ownRunning.started_at))}. Stop or discard it in the entries below.</p>`
      : '<p class="muted">No reported-time timer is running.</p>';
    startButton.disabled = Boolean(ownRunning);
    if (!state.workSessions.length) {
      setEmpty(target, "No reported-time entries yet.");
    } else {
      const currentUserId = String(state.session.user.id);
      const rows = state.workSessions.map((entry) => {
        const isOwner = String(entry.user_id) === currentUserId;
        const actions = [];
        if (isOwner && entry.status === "running") {
          actions.push(`<button class="button secondary" type="button" data-time-action="stop" data-session-id="${esc(entry.id)}">Stop</button>`);
        }
        if (isOwner && ["stopped", "rejected"].includes(entry.status)) {
          actions.push(`<button class="button secondary" type="button" data-time-action="submit" data-session-id="${esc(entry.id)}">${entry.status === "rejected" ? "Resubmit" : "Submit"}</button>`);
        }
        if (isOwner && entry.source === "timer" && ["running", "stopped", "rejected"].includes(entry.status)) {
          actions.push(`<button class="button danger" type="button" data-time-action="discard" data-session-id="${esc(entry.id)}">${entry.status === "rejected" ? "Delete" : "Discard"}</button>`);
          actions.push(`<button class="button ghost" type="button" data-time-action="edit" data-session-id="${esc(entry.id)}">Edit times</button>`);
        }
        if (String(state.editingSessionId) === String(entry.id) && isOwner) {
          return `
            <tr>
              <td colspan="6">
                <form class="time-edit-form" data-session-id="${esc(entry.id)}">
                  <label>Started<input type="datetime-local" name="started-at" required value="${esc(toLocalInput(entry.started_at))}"></label>
                  <label>Stopped<input type="datetime-local" name="ended-at" ${entry.status === "running" ? "" : "required"} value="${esc(toLocalInput(entry.ended_at))}"></label>
                  <div class="time-actions">
                    <button class="button primary" type="submit">Save times</button>
                    <button class="button ghost" type="button" data-time-action="cancel-edit" data-session-id="${esc(entry.id)}">Cancel</button>
                  </div>
                </form>
                <p class="muted">Set the stop time if the timer was left running. Submitted and approved entries stay locked.</p>
              </td>
            </tr>
          `;
        }
        return `
          <tr>
            <td>${esc(memberName(entry.user_id))}</td>
            <td>${esc(taskTitle(entry.task_id))}</td>
            <td>${esc(formatDate(entry.started_at))}</td>
            <td>${esc(formatDuration(entry.duration_seconds))}</td>
            <td><span class="badge time-${esc(entry.status)}">${esc(entry.status)}</span></td>
            <td class="time-actions">${actions.join(" ") || "—"}</td>
          </tr>
        `;
      }).join("");
      target.innerHTML = `
        <table>
          <thead><tr><th scope="col">Person</th><th scope="col">Task</th><th scope="col">Started</th><th scope="col">Duration</th><th scope="col">Status</th><th scope="col">Actions</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    }
  }

  form.classList.toggle("hidden", Boolean(ownRunning));
  const taskSelect = $("timer-task");
  const selectedTask = taskSelect.value;
  taskSelect.innerHTML = '<option value="">No linked task</option>' + (state.tasks || [])
    .filter((task) => !["done", "cancelled"].includes(task.status))
    .map((task) => `<option value="${esc(task.id)}">${esc(task.title)}</option>`)
    .join("");
  taskSelect.value = selectedTask;
}

async function startReportedTimer(event) {
  event.preventDefault();
  const button = $("start-time");
  button.disabled = true;
  try {
    const result = await api("/v1/work-sessions/start", {
      method: "POST",
      body: JSON.stringify({
        task_id: $("timer-task").value || null,
        notes: $("timer-notes").value.trim(),
      }),
    });
    $("timer-form").reset();
    showToast("Reported-time timer started", result?.undo_id);
    state.workSessionsError = null;
    await refreshAll();
  } catch (error) {
    if (!handleAuthError(error)) showToast(error.message);
  } finally {
    button.disabled = Boolean(state.workSessionsError || !state.workSessions || state.workSessions.some((entry) =>
      String(entry.user_id) === String(state.session?.user?.id) && entry.status === "running",
    ));
  }
}

async function handleWorkSessionAction(button) {
  const action = button.dataset.timeAction;
  const sessionId = button.dataset.sessionId;
  const entry = (state.workSessions || []).find((item) => String(item.id) === String(sessionId));
  if (!entry) return;
  const isOwner = String(entry.user_id) === String(state.session.user.id);
  if (action === "discard") {
    if (!isOwner || entry.source !== "timer" || !["running", "stopped", "rejected"].includes(entry.status)) return;
    const verb = entry.status === "rejected" ? "Delete" : "Discard";
    if (!window.confirm(`${verb} this reported-time entry? It will no longer count as reported time.`)) return;
  } else if (action === "stop" && (!isOwner || entry.status !== "running")) {
    return;
  } else if (action === "edit") {
    if (!isOwner || entry.source !== "timer" || !["running", "stopped", "rejected"].includes(entry.status)) return;
    state.editingSessionId = sessionId;
    renderWorkSessions();
    return;
  } else if (action === "cancel-edit") {
    state.editingSessionId = null;
    renderWorkSessions();
    return;
  } else if (action === "submit" && (!isOwner || !["stopped", "rejected"].includes(entry.status))) {
    return;
  } else if (["approve", "reject"].includes(action) && (isOwner || entry.status !== "submitted")) {
    return;
  }

  button.disabled = true;
  try {
    let result = null;
    if (action === "discard") {
      result = await api(`/v1/work-sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      showToast(entry.status === "rejected" ? "Rejected time entry deleted" : "Reported-time entry discarded", result?.undo_id);
    } else if (action === "stop") {
      result = await api(`/v1/work-sessions/${encodeURIComponent(sessionId)}/stop`, {
        method: "POST",
        body: JSON.stringify({ notes: "" }),
      });
      showToast("Reported-time timer stopped", result?.undo_id);
    } else if (action === "submit") {
      result = await api(`/v1/work-sessions/${encodeURIComponent(sessionId)}/submit`, {
        method: "POST",
        body: JSON.stringify({ notes: "" }),
      });
      showToast("Reported time submitted for review", result?.undo_id);
    } else if (["approve", "reject"].includes(action)) {
      result = await api(`/v1/work-sessions/${encodeURIComponent(sessionId)}/decision`, {
        method: "POST",
        body: JSON.stringify({ approved: action === "approve", reason: "" }),
      });
      showToast(action === "approve" ? "Reported time approved" : "Reported time rejected", result?.undo_id);
    }
    state.workSessionsError = null;
    await refreshAll();
  } catch (error) {
    if (!handleAuthError(error)) showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function saveEditedTimer(form) {
  const sessionId = form.dataset.sessionId;
  const entry = (state.workSessions || []).find((item) => String(item.id) === String(sessionId));
  if (!entry) return;
  const started = fromLocalInput(form.querySelector("[name=started-at]")?.value);
  const ended = fromLocalInput(form.querySelector("[name=ended-at]")?.value);
  if (!started) {
    showToast("Start time is required.");
    return;
  }
  if (entry.status !== "running" && !ended) {
    showToast("Stopped time needs an end time.");
    return;
  }
  const saveButton = form.querySelector("button[type=submit]");
  if (saveButton) saveButton.disabled = true;
  try {
    const body = { started_at: started };
    if (ended) body.ended_at = ended;
    const result = await api(`/v1/work-sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    state.editingSessionId = null;
    showToast("Reported time updated", result?.undo_id);
    state.workSessionsError = null;
    await refreshAll();
  } catch (error) {
    if (!handleAuthError(error)) showToast(error.message);
  } finally {
    if (saveButton) saveButton.disabled = false;
  }
}

function renderIdentityMappings() {
  const panel = $("identity-admin");
  const admin = isAdministrator();
  panel.classList.toggle("hidden", !admin);
  if (!admin) return;

  const candidateTarget = $("identity-candidates");
  if (state.accessEventsError) {
    setError(candidateTarget, "Recent access identities could not be loaded.");
  } else if (!state.accessEvents) {
    candidateTarget.innerHTML = '<p class="loading">Loading identities…</p>';
  } else if (state.usersError) {
    setError(candidateTarget, "Member options could not be loaded.");
  } else {
    const mappedIdentities = new Set((state.identityMappings || [])
      .filter((mapping) => String(mapping.source).toLowerCase() === "linux")
      .map((mapping) => mapping.external_identity));
    const identities = [...new Set(state.accessEvents
      .filter((event) => event.identity_source === "linux" && event.identity && !event.user_id)
      .map((event) => event.identity))]
      .filter((identity) => !mappedIdentities.has(identity))
      .sort((a, b) => a.localeCompare(b));

    if (!identities.length) {
      setEmpty(candidateTarget, "No unmapped Linux identities in recent access events.");
    } else {
      const options = '<option value="">Choose a member…</option>' + (state.users || [])
        .filter((user) => user.active !== false && user.role !== "service")
        .map((user) => `<option value="${esc(user.id)}">${esc(user.display_name || user.login)}</option>`)
        .join("");
      candidateTarget.innerHTML = identities.map((identity) => {
        const isShared = sharedLinuxAccounts.has(identity);
        return `
          <article class="identity-card" data-identity-card>
            <div class="identity-name"><code>${esc(identity)}</code><span class="badge">Linux</span></div>
            ${isShared
              ? '<p class="identity-warning">Shared account; it must not be mapped to an individual.</p>'
              : `<label>Map to member<select data-identity-user>${options}</select></label><button class="button primary" type="button" data-map-identity="${esc(identity)}">Add mapping</button>`}
            <p class="identity-error" data-identity-error role="alert"></p>
          </article>
        `;
      }).join("");
    }
  }

  const mappingTarget = $("identity-mapping-list");
  if (state.identityMappingsError) {
    setError(mappingTarget, "Identity mappings could not be loaded.");
  } else if (!state.identityMappings) {
    mappingTarget.innerHTML = '<p class="loading">Loading mappings…</p>';
  } else if (!state.identityMappings.length) {
    setEmpty(mappingTarget, "No identity mappings have been added.");
  } else {
    mappingTarget.innerHTML = state.identityMappings.map((mapping) => `
      <article class="mapping-row">
        <div><code>${esc(mapping.external_identity)}</code><small>${esc(mapping.source)}</small></div>
        <span>${esc(memberName(mapping.user_id))}</span>
        <button class="button secondary" type="button" data-delete-mapping="${esc(mapping.id)}" aria-label="Delete mapping for ${esc(mapping.external_identity)}">Remove</button>
      </article>
    `).join("");
  }
}

async function createIdentityMapping(button) {
  const card = button.closest("[data-identity-card]");
  if (!card) return;
  const identity = button.dataset.mapIdentity;
  if (sharedLinuxAccounts.has(identity)) return;
  const userId = card.querySelector("[data-identity-user]").value;
  if (!userId) {
    card.querySelector("[data-identity-error]").textContent = "Choose a member before adding this mapping.";
    return;
  }
  button.disabled = true;
  try {
    const result = await api("/v1/identity-mappings", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, source: "linux", external_identity: identity }),
    });
    showToast("Linux identity mapped", result?.undo_id);
    await refreshAll();
  } catch (error) {
    if (!handleAuthError(error)) showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function deleteIdentityMapping(button) {
  const mappingId = button.dataset.deleteMapping;
  if (!mappingId || !window.confirm("Remove this identity mapping?")) return;
  button.disabled = true;
  try {
    const result = await api(`/v1/identity-mappings/${encodeURIComponent(mappingId)}`, { method: "DELETE" });
    showToast("Identity mapping removed", result?.undo_id);
    await refreshAll();
  } catch (error) {
    if (!handleAuthError(error)) showToast(error.message);
  } finally {
    button.disabled = false;
  }
}



function renderDashboardActivity() {
  const target = $("dashboard-time");
  if (state.dashboardActivityError) {
    setError(target, "Dashboard activity estimates could not be loaded.");
    return;
  }
  if (!state.dashboardActivity) {
    target.innerHTML = '<p class="loading">Loading dashboard activity…</p>';
    return;
  }
  const items = state.dashboardActivity
    .map((item) => ({
      ...item,
      dashboardSeconds: Number(item.sources?.dashboard || 0),
    }))
    .filter((item) => item.dashboardSeconds > 0)
    .sort((a, b) =>
      String(b.day).localeCompare(String(a.day)) ||
      memberName(a.user_id).localeCompare(memberName(b.user_id)),
    );
  if (!items.length) {
    setEmpty(target, "No hours-worked estimates in this date range.");
    return;
  }
  const cards = items.map((item) => `
    <article class="estimate-card">
      <div class="estimate-main">
        <div><p class="eyebrow">${esc(formatDay(item.day))} UTC</p><h3>${esc(memberName(item.user_id))}</h3></div>
        <strong class="estimate-total">${esc(formatWorkerDuration(item.dashboardSeconds))}<small>hours worked estimate</small></strong>
      </div>
    </article>
  `).join("");
  target.innerHTML = cards;
}

async function reloadDashboardActivity() {
  const from = encodeURIComponent($("dashboard-time-from").value);
  const to = encodeURIComponent($("dashboard-time-to").value);
  try {
    state.dashboardActivity = await api(`/v1/activity/estimates?from=${from}&to=${to}`);
    state.dashboardActivityError = null;
  } catch (error) {
    state.dashboardActivityError = error;
    if (handleAuthError(error)) return;
  }
  renderDashboardActivity();
}

function switchView(view, updateHash = true) {
  const selected = ["work", "operations"].includes(view) ? view : "work";
  document.querySelectorAll(".area-tab").forEach((button) => {
    const active = button.dataset.view === selected;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("active", section.id === `view-${selected}`);
  });
  if (updateHash) history.replaceState(null, "", `#${selected}`);
}

function pulseSessionId() {
  try {
    let id = sessionStorage.getItem("fieldwork-session-id");
    if (!id) {
      id = crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
      sessionStorage.setItem("fieldwork-session-id", id);
    }
    return id;
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
}

const browserSessionId = pulseSessionId();

function noteInteraction(event) {
  if (event.isTrusted) state.lastInteraction = Date.now();
}

async function sendPulse(bootstrap = false) {
  if (!state.session) return;
  if (document.visibilityState !== "visible") return;
  if (!bootstrap && Date.now() - state.lastInteraction > 5 * 60 * 1000) return;
  if (Date.now() - state.lastPulse < 60000) return;
  state.lastPulse = Date.now();
  const view = document.querySelector(".area-tab.active")?.dataset.view || "work";
  try {
    await api("/v1/activity/pulse", {
      method: "POST",
      body: JSON.stringify({ session_id: browserSessionId, view }),
    });
  } catch (error) {
    if (error.status === 401) handleAuthError(error);
  }
}

$("task-board").addEventListener("click", (event) => {
  const button = event.target.closest("[data-task-id]");
  if (!button) return;
  const task = state.tasks.find((item) => String(item.id) === button.dataset.taskId);
  openTask(task || null);
});
$("agent-task-board").addEventListener("click", (event) => {
  const button = event.target.closest("[data-task-id]");
  if (!button) return;
  const task = state.tasks.find((item) => String(item.id) === button.dataset.taskId);
  openTask(task || null);
});
$("agent-task-status-filter").addEventListener("change", renderAgentTasks);
$("new-task").addEventListener("click", () => openTask());
$("close-task").addEventListener("click", () => $("task-dialog").close());
$("cancel-task").addEventListener("click", () => $("task-dialog").close());
$("task-form").addEventListener("submit", saveTask);
$("archive-task").addEventListener("click", archiveTask);
$("timer-form").addEventListener("submit", startReportedTimer);
$("work-sessions").addEventListener("click", (event) => {
  const button = event.target.closest("[data-time-action]");
  if (button) handleWorkSessionAction(button);
});
$("work-sessions").addEventListener("submit", (event) => {
  const form = event.target.closest(".time-edit-form");
  if (!form) return;
  event.preventDefault();
  saveEditedTimer(form);
});
$("task-status-filter").addEventListener("change", () => {
  state.tasks = null;
  state.tasksError = null;
  renderTasks();
  refreshAll().catch(() => {});
});
 $("load-dashboard-time").addEventListener("click", reloadDashboardActivity);
$("identity-admin").addEventListener("click", (event) => {
  const mapButton = event.target.closest("[data-map-identity]");
  if (mapButton) {
    createIdentityMapping(mapButton);
    return;
  }
  const deleteButton = event.target.closest("[data-delete-mapping]");
  if (deleteButton) deleteIdentityMapping(deleteButton);
});
async function acknowledgeIncident(eventId, button) {
  if (!window.confirm("Confirm that you have reviewed and handled this exposure before recording a human acknowledgment?")) return;
  button.disabled = true;
  try {
    await api(`/v1/api-guard/incidents/${encodeURIComponent(eventId)}/ack`, { method: "POST" });
    showToast("Incident acknowledgment recorded");
    await refreshAll();
  } catch (error) {
    if (!handleAuthError(error)) showToast("Incident acknowledgment could not be recorded.");
  } finally {
    button.disabled = false;
  }
}
document.querySelector("#api-guard-incident-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-incident-ack]");
  if (button) acknowledgeIncident(button.dataset.incidentAck, button);
});
$("people-channels").addEventListener("change", (event) => {
  const select = event.target.closest("[data-access-user]");
  if (!select) return;
  savePerson(select.dataset.accessUser, { choice: select.value }).catch((error) => {
    if (!handleAuthError(error)) showToast(error.message);
  });
});
$("people-channels").addEventListener("click", (event) => {
  const save = event.target.closest("[data-save-name]");
  const remove = event.target.closest("[data-delete-user]");
  if (save) {
    const input = document.querySelector(`[data-person-name="${CSS.escape(save.dataset.saveName)}"]`);
    savePerson(save.dataset.saveName, { display_name: input?.value || "" }).catch((error) => {
      if (!handleAuthError(error)) showToast(error.message);
    });
    return;
  }
  if (!remove) return;
  const person = (state.users || []).find((item) => String(item.id) === String(remove.dataset.deleteUser));
  if (!window.confirm(`Delete ${personLabel(person)} from the board?`)) return;
  api(`/v1/users/${encodeURIComponent(remove.dataset.deleteUser)}`, { method: "DELETE" })
    .then(() => refreshAll())
    .then(() => showToast("Person deleted"))
    .catch((error) => {
      if (!handleAuthError(error)) showToast(error.message);
    });
});
document.querySelectorAll(".area-tab").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});
$("toast-undo").addEventListener("click", undoLastAction);
$("logout").addEventListener("click", async () => {
  if (!window.confirm("Sign out of TheGarden?")) return;
  try {
    await api("/auth/logout", { method: "POST" });
  } catch {}
  state.session = null;
  showLogin("You have signed out.");
});

 $("dashboard-time-to").value = utcDateValue(new Date());
 $("dashboard-time-from").value = $("dashboard-time-to").value;
for (const eventName of ["pointerdown", "touchstart", "click", "keydown"]) {
  document.addEventListener(eventName, noteInteraction, { passive: true });
}
document.addEventListener("visibilitychange", sendPulse);
setInterval(sendPulse, 60000);
setInterval(() => {
  if (!state.session) return;
  refreshAll().catch((error) => {
    if (!handleAuthError(error)) {
      showToast("Refresh failed. The last loaded information is still shown.");
    }
  });
}, 30000);

async function boot() {
  if (location.hash === "#operations") switchView("operations", false);
  await configureAuth();
  try {
    state.session = await api("/v1/session");
    showApp();
    $("connection").textContent = "Loading";
    void sendPulse(true);
    await refreshAll();
  } catch (error) {
    state.session = null;
    showLogin(new URLSearchParams(location.search).get("access") === "pending"
      ? "Forgejo sign-in worked, but this account is waiting for access. An admin can grant it from the board."
      : error.status === 401
        ? "Sign in with your Forgejo account to continue."
        : "Dashboard unavailable. Try reloading in a moment.");
  }
}

boot();
