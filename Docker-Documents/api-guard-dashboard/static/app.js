(() => {
  const base = "/api-guard";
  const state = { csrf: "", data: null, loadSequence: 0 };
  const $ = (id) => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = value; };
  const money = (value) => new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(Number(value || 0));
  const number = (value) => new Intl.NumberFormat().format(Number(value || 0));
  const escape = (value) => String(value ?? "");

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (options.method && !["GET", "HEAD"].includes(options.method.toUpperCase()) && state.csrf) headers.set("X-CSRF-Token", state.csrf);
    const response = await fetch(`${base}${path}`, { ...options, headers, cache: "no-store", credentials: "same-origin" });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `Request failed (${response.status})`);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function showToast(message) {
    text("toast", message);
    $("toast").classList.remove("hidden");
    window.setTimeout(() => $("toast").classList.add("hidden"), 4500);
  }

  function showSecret(key, note) {
    $("new-secret").value = key;
    text("secret-note", note);
    $("secret-copy-status").textContent = "";
    $("secret-dialog").showModal();
  }

  function renderBudget(budget) {
    text("annual-spend", money(budget.annual_spend_usd));
    text("annual-detail", `${money(budget.annual_limit_usd)} shared LiteLLM hard limit`);
    text("monthly-spend", money(budget.monthly_spend_usd));
    text("monthly-detail", `${money(budget.monthly_target_usd)} pacing target, not a separate cap`);
    text("monthly-tokens", number(budget.monthly_tokens));
    text("token-detail", `${number(budget.prompt_tokens_monthly)} prompt · ${number(budget.completion_tokens_monthly)} completion`);
    $("annual-bar").style.width = `${Math.min(100, Number(budget.annual_percent || 0))}%`;
    $("monthly-bar").style.width = `${Math.min(100, Number(budget.monthly_target_percent || 0))}%`;
    $("annual-bar").classList.toggle("warning", budget.annual_percent >= 80);
    $("monthly-bar").classList.toggle("warning", budget.monthly_target_percent >= 100);
  }

  function renderOpenAICosts(costs) {
    const root = $("openai-costs");
    root.replaceChildren();
    if (!costs?.configured) {
      root.textContent = "Not configured. Add an organization Admin API key with Usage Read access to show OpenAI-reported costs.";
      return;
    }
    if (!costs.available) {
      root.textContent = costs.message || "OpenAI-reported costs are unavailable.";
      return;
    }
    const grid = document.createElement("div");
    grid.className = "provider-cost-grid";
    for (const [label, value] of [
      ["OpenAI reported costs · rolling 365 days", costs.rolling_365d_usd],
      ["OpenAI reported costs · month to date", costs.month_to_date_usd],
    ]) {
      const item = document.createElement("div");
      item.className = "provider-cost-item";
      const title = document.createElement("span");
      title.textContent = label;
      const amount = document.createElement("strong");
      amount.textContent = money(value);
      item.append(title, amount);
      grid.append(item);
    }
    root.append(grid);
    const note = document.createElement("p");
    note.className = "provider-cost-note";
    note.textContent = `${costs.scope} · ${costs.message}${costs.truncated ? " Results are incomplete due to pagination limits." : ""}`;
    root.append(note);
  }

  function renderKeys(keys, rotationDays, events) {
    const flaggedIds = new Set(events.filter((event) => event.status === "flagged" && event.matched_key_id).map((event) => event.matched_key_id));
    const oldKeyIds = new Set(keys.filter((key) => key.age_days !== null && key.age_days >= rotationDays).map((key) => key.key_id || `alias:${key.alias}`));
    const unknownAlerts = events.filter((event) => event.status === "flagged" && !event.matched_key_id).map((event) => `event:${event.event_id}`);
    const alertCount = new Set([...flaggedIds, ...oldKeyIds, ...unknownAlerts]).size;
    if (!keys.length) {
      text("key-alert-count", unknownAlerts.length ? number(alertCount) : "—");
      text("key-alert-detail", "No active key inventory; age and rotation checks are incomplete");
      $("keys").innerHTML = '<p class="empty">No LiteLLM keys were returned.</p>';
      return;
    }
    text("key-alert-count", number(alertCount));
    text("key-alert-detail", `${number(flaggedIds.size)} exposed · ${number(oldKeyIds.size)} age overdue`);
    const table = document.createElement("table");
    table.innerHTML = '<thead><tr><th>Key</th><th>Models</th><th>Spend / cap</th><th>Age</th><th>Status</th><th>Actions</th></tr></thead>';
    const body = document.createElement("tbody");
    for (const key of keys) {
      const row = document.createElement("tr");
      const exposed = key.key_id && flaggedIds.has(key.key_id);
      const old = key.age_days !== null && key.age_days >= rotationDays;
      const status = key.blocked ? "Blocked" : exposed ? "Exposed" : old ? "Rotate now" : "Current";
      row.innerHTML = `<td><strong></strong><small></small></td><td></td><td></td><td></td><td><span class="badge"></span></td><td class="actions"></td>`;
      row.cells[0].querySelector("strong").textContent = escape(key.alias);
      row.cells[0].querySelector("small").textContent = key.key_id ? `ID …${key.key_id.slice(-8)}` : "ID unavailable";
      row.cells[1].textContent = key.models.length ? key.models.join(", ") : "No model restriction";
      row.cells[2].textContent = `${money(key.spend_usd)} / ${key.max_budget_usd === null ? "no cap" : money(key.max_budget_usd)}`;
      const tokenUsage = document.createElement("small");
      tokenUsage.textContent = `${number(key.tokens_monthly)} tokens this month`;
      row.cells[2].append(tokenUsage);
      row.cells[3].textContent = key.age_days === null ? "Unknown" : `${key.age_days} days`;
      const badge = row.cells[4].querySelector(".badge");
      badge.textContent = status;
      if (exposed || old || key.blocked) badge.classList.add("alert");
      const actions = row.cells[5];
      if (key.key_id && !key.blocked) {
        const rotate = document.createElement("button");
        rotate.className = "button small secondary";
        rotate.type = "button";
        rotate.textContent = exposed ? "Replace & revoke" : "Rotate";
        rotate.addEventListener("click", () => rotateKey(key, Boolean(exposed)));
        actions.append(rotate);
        const revoke = document.createElement("button");
        revoke.className = "button small danger";
        revoke.type = "button";
        revoke.textContent = "Revoke";
        revoke.addEventListener("click", () => revokeKey(key));
        actions.append(revoke);
      }
      body.append(row);
    }
    table.append(body);
    $("keys").replaceChildren(table);
  }

  function renderExposures(events) {
    const root = $("exposures");
    if (!events.length) {
      root.innerHTML = '<p class="empty">No OMP exposure alerts recorded.</p>';
      return;
    }
    const list = document.createElement("div");
    list.className = "events";
    for (const event of events) {
      const item = document.createElement("article");
      item.className = `event${event.worker_id || event.session_id ? " worker-exposure" : ""}`;
      const title = document.createElement("strong");
      title.textContent = event.status === "flagged" ? "Credential exposure reported" : `Exposure ${event.status}`;
      const detail = document.createElement("p");
      const identifiers = [];
      if (event.worker_id) identifiers.push(`worker: ${event.worker_id}`);
      if (event.session_id) identifiers.push(`session: ${event.session_id}`);
      const workerDetails = identifiers.length ? ` · ${identifiers.join(" · ")}` : "";
      detail.textContent = `${event.key_alias}${event.matched_alias ? ` · matched: ${event.matched_alias}` : " · no key matched"}${workerDetails} · ${event.reason.replaceAll("_", " ")}`;
      const date = document.createElement("small");
      date.textContent = new Date(event.received_at).toLocaleString();
      const delivery = document.createElement("small");
      const tries = number(event.notification_attempts || 0);
      delivery.textContent = event.notification_status === "sent"
        ? "External receiver accepted alert"
        : event.notification_status === "pending"
          ? `External alert queued/retrying · ${tries} attempt${tries === 1 ? "" : "s"}`
          : "External notification route not configured";
      item.append(title, detail, date, delivery);
      if (event.status === "flagged" && !event.matched_key_id) {
        const resolve = document.createElement("button");
        resolve.className = "button small secondary";
        resolve.type = "button";
        resolve.textContent = "Resolve after review";
        resolve.addEventListener("click", async () => {
          if (!window.confirm("Resolve this alert only after you have identified and handled the exposed credential?")) return;
          await api("/api/v1/exposures/resolve", { method: "POST", body: JSON.stringify({ event_id: event.event_id }) });
          await load();
        });
        item.append(resolve);
      }
      list.append(item);
    }
    root.replaceChildren(list);
  }

  function renderActions(data) {
    const root = $("readiness-items");
    const actions = [];
    const addAction = (level, titleText, detailText) => {
      const item = document.createElement("article");
      item.className = `readiness-item ${level}`;
      const heading = document.createElement("div");
      heading.className = "readiness-heading";
      const title = document.createElement("strong");
      title.textContent = titleText;
      const priority = document.createElement("span");
      priority.className = "readiness-priority";
      priority.textContent = level === "urgent" ? "URGENT" : level === "review" ? "REVIEW" : "HARDENING";
      heading.append(title, priority);
      const detail = document.createElement("p");
      detail.textContent = detailText;
      item.append(heading, detail);
      actions.push(item);
    };
    const events = Array.isArray(data.exposure_events) ? data.exposure_events : [];
    const keys = Array.isArray(data.keys) ? data.keys : [];
    const workerAlerts = events.filter(
      (event) => event.status === "flagged" && (event.worker_id || event.session_id),
    );
    const visiblePending = events.filter((event) => event.notification_status === "pending").length;
    const pending = Math.max(Number(data.alert_notification_pending_count || 0), visiblePending);

    if (workerAlerts.length) {
      addAction(
        "urgent",
        "Investigate worker/session exposure",
        `${workerAlerts.length} flagged event(s). Verify exact session-key revocation; identify unmapped patterns before resolving them.`,
      );
    }
    if (!data.alert_notifications_configured) {
      addAction(
        "urgent",
        "Configure 24/7 alert delivery",
        "Set API_GUARD_ALERT_WEBHOOK_URL and a distinct API_GUARD_ALERT_WEBHOOK_SECRET for a Forge/paging receiver with acknowledgement and escalation. Email or this dashboard alone is not an on-call system.",
      );
    } else if (pending) {
      addAction(
        "urgent",
        "Confirm alert receiver delivery",
        `${pending} notification(s) are queued or retrying. Check the receiver and acknowledge incidents there; HTTP acceptance does not confirm a responder saw the alert.`,
      );
    }
    if (!keys.length) {
      addAction(
        "review",
        "Reconcile the empty key inventory",
        "No active LiteLLM virtual keys were returned. Short-lived worker keys may be absent between jobs; verify any expected long-lived consumer key and confirm Keybroker targets the same LiteLLM instance as spend logging.",
      );
    }
    if (data.unmatched_usage_key_count > 0) {
      addAction(
        "review",
        "Reconcile unmatched spend records",
        `${number(data.unmatched_usage_key_count)} spend-log key ID(s) have no active key record. They may be historical or from another LiteLLM database; per-key attribution is incomplete until checked.`,
      );
    }
    if (data.budget.zero_cost_requests_monthly > 0) {
      addAction(
        "review",
        "Verify zero-priced token usage",
        `${number(data.budget.zero_cost_requests_monthly)} token-bearing request(s) were priced at $0. Check LiteLLM model pricing and the provider bill before treating that usage as free.`,
      );
    }
    if (data.usage_truncated) {
      addAction(
        "review",
        "Review incomplete usage totals",
        "LiteLLM spend-log pagination reached its limit; displayed totals are lower bounds.",
      );
    }
    addAction(
      "hardening",
      "Model-escape controls remain incomplete",
      "The VaultGuard worker unit template is configured to deny direct public-IP egress and allow the service VM/Tailscale DNS; this takes effect only after deployment and verification on each host. It does not constrain other host processes, prevent DNS tunneling, or detect arbitrary model exfiltration. Keep unsandboxed script execution disabled and add host/network monitoring.",
    );
    addAction(
      "hardening",
      "Verify the worker roster separately",
      "This dashboard inventories LiteLLM keys, not enrolled hosts. Compare the Keybroker IP allowlist and Ansible inventory against every intended, owner-approved worker.",
    );

    root.replaceChildren(...actions);
    $("readiness-count").textContent = `${actions.length} action${actions.length === 1 ? "" : "s"}`;
  }

  async function load() {
    const sequence = ++state.loadSequence;
    text("connection", "Refreshing");
    try {
      const data = await api("/api/v1/overview");
      if (sequence !== state.loadSequence) return;
      state.data = data;
      renderBudget(data.budget);
      renderOpenAICosts(data.openai_costs);
      renderKeys(data.keys, data.rotation_days, data.exposure_events);
      renderActions(data);
      text("as-of", `Updated ${new Date(data.as_of).toLocaleTimeString()}`);
      const workerAlerts = data.exposure_events.filter(
        (event) => event.status === "flagged" && (event.worker_id || event.session_id),
      );
      if (workerAlerts.length) {
        $("status-banner").textContent = `URGENT: ${workerAlerts.length} worker/session credential exposure alert${workerAlerts.length === 1 ? "" : "s"} require review. The affected run is stopped; inspect the identifiers below.`;
        $("status-banner").classList.remove("hidden");
      } else {
        $("status-banner").classList.add("hidden");
      }
      $("status-banner").classList.toggle("urgent", workerAlerts.length > 0);
      $("jev-status").textContent = data.jev_configured ? `JEV advisor ready: ${data.jev_model}. Its output is advisory only.` : `JEV is not configured. Set a dedicated, restricted API_GUARD_JEV_API_KEY and route the ${data.jev_model} model alias through LiteLLM.`;
      $("ask-jev").disabled = !data.jev_configured;
      $("key-expiry-days").textContent = String(data.rotation_days * 2);
      text("connection", "Connected");
    } catch (error) {
      if (sequence !== state.loadSequence) return;
      text("connection", "Unavailable");
      $("status-banner").textContent = error.message;
      $("status-banner").classList.remove("hidden");
      $("status-banner").classList.remove("urgent");
    }
  }

  async function revokeKey(key) {
    if (!window.confirm(`Revoke ${key.alias}? All consumers using this key will stop working immediately.`)) return;
    try {
      await api("/api/v1/keys/revoke", { method: "POST", body: JSON.stringify({ key_id: key.key_id }) });
      showToast("LiteLLM key revoked.");
      await load();
    } catch (error) { showToast(error.message); }
  }

  async function rotateKey(key, exposed) {
    const scopeWarning = key.models.length ? "" : " This key is unrestricted; the replacement will be limited to the luna model.";
    const action = exposed
      ? `Replace ${key.alias} and immediately revoke the exposed old key? Consumers must be updated with the replacement.${scopeWarning}`
      : `Create a replacement for ${key.alias}? The old key remains active until you revoke it after deploying the replacement.${scopeWarning}`;
    if (!window.confirm(action)) return;
    try {
      const result = await api("/api/v1/keys/rotate", { method: "POST", body: JSON.stringify({ key_id: key.key_id }) });
      let revoked = false;
      let revokeError = "";
      if (exposed) {
        try {
          await api("/api/v1/keys/revoke", { method: "POST", body: JSON.stringify({ key_id: key.key_id }) });
          revoked = true;
        } catch (error) { revokeError = error.message; }
      }
      const narrowing = result.models_narrowed_to?.length ? `The legacy unrestricted key was narrowed to ${result.models_narrowed_to.join(", ")}. ` : "";
      const note = narrowing + (exposed
        ? revoked
          ? `The exposed old key was revoked. Update ${key.alias}'s consumer now; the replacement is valid for ${result.expires_after_days} days.`
          : `Replacement created, but old-key revocation failed (${revokeError || "unknown error"}). Update the consumer and revoke the exposed old key immediately.`
        : `The old key remains active. Update the consumer, verify requests, then revoke the old key. Rotate again within ${result.rotate_after_days} days.`);
      showSecret(result.key, note);
      await load();
    } catch (error) { showToast(error.message); }
  }

  async function start() {
    try {
      const session = await api("/api/v1/session");
      state.csrf = session.csrf_token;
      text("identity", session.login);
      $("login").classList.add("hidden");
      $("dashboard").classList.remove("hidden");
      $("logout").classList.remove("hidden");
      $("refresh").classList.remove("hidden");
      await load();
    } catch (_error) {
      $("login").classList.remove("hidden");
      text("connection", "Sign-in required");
      try {
        const config = await api("/api/v1/auth-config");
        if (!config.forgejo_oauth) {
          text("login-error", "Forgejo administrator sign-in is not configured for this service.");
          $("login-link").classList.add("hidden");
        }
      } catch (_ignored) { /* login UI remains available */ }
      if (new URLSearchParams(window.location.search).get("access") === "denied") text("login-error", "This Forgejo account is not on the API Guard administrator allowlist.");
    }
  }

  $("refresh").addEventListener("click", () => load());
  $("show-create").addEventListener("click", () => { $("create-error").textContent = ""; $("create-dialog").showModal(); });
  $("close-create").addEventListener("click", () => $("create-dialog").close());
  $("cancel-create").addEventListener("click", () => $("create-dialog").close());
  $("create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const models = $("key-models").value.split(",").map((item) => item.trim()).filter(Boolean);
    try {
      const result = await api("/api/v1/keys", { method: "POST", body: JSON.stringify({ alias: $("key-alias").value, models, max_budget_usd: Number($("key-budget").value), budget_duration: $("key-budget-duration").value }) });
      $("create-dialog").close();
      showSecret(result.key, `Save this key to its consumer's secret store. It expires in ${result.expires_after_days} days; rotate by day ${result.rotate_after_days}.`);
      $("create-form").reset();
      $("key-models").value = "luna";
      await load();
    } catch (error) { text("create-error", error.message); }
  });
  $("copy-secret").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("new-secret").value);
      text("secret-copy-status", "Copied; paste directly into the secret store.");
    } catch (_error) { text("secret-copy-status", "Clipboard unavailable; select and copy manually."); }
  });
  $("close-secret").addEventListener("click", () => { $("new-secret").value = ""; $("secret-dialog").close(); });
  $("secret-dialog").addEventListener("close", () => { $("new-secret").value = ""; });
  $("logout").addEventListener("click", async () => {
    try { await api("/auth/logout", { method: "POST" }); } finally { window.location.reload(); }
  });
  $("ask-jev").addEventListener("click", async () => {
    const review = $("jev-review");
    review.textContent = "JEV is reviewing sanitized status…";
    review.classList.remove("hidden");
    try {
      const result = await api("/api/v1/decision-review", { method: "POST" });
      review.textContent = `${result.review}\n\nAdvisory only · ${result.model} · ${new Date(result.as_of).toLocaleString()}`;
    } catch (error) { review.textContent = error.message; }
  });
  start();
  window.setInterval(() => { if (state.csrf) load(); }, 60_000);
})();
