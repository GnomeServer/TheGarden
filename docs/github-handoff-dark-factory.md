# Dark Factory GitHub handoff

## Purpose

This document is the implementation and deployment handoff for the Dark Factory control plane in TheGarden. It is written for a GitHub-based coding agent or engineer taking over the repository.

The required result is one central, authenticated dashboard that combines:

- Editable todos, deadlines, priorities, and assignments.
- Explicit task-linked work timers and manager-approved time entries.
- Worker heartbeats, current runs, completions, and failures.
- Forgejo Git activity and individual human identity.
- Normalized SSH, sudo, Tailscale, and service access evidence.
- Prometheus node-health summaries.
- Deep links into the existing Grafana Infrastructure – Server dashboard.
- Deduplicated deadline notifications.

Do not replace Prometheus, Grafana, Forgejo, NATS, or Loki with custom equivalents. The Dark Factory dashboard is the team-facing workflow interface; the existing services remain authoritative for their own data.

JEV routing, token budgeting, OMP cutover, dashboard boundaries, fleet phases,
and service placement are specified in
[`jev-budget-omp-architecture.md`](jev-budget-omp-architecture.md). The tracked,
idempotent dashboard backlog is [`dark-factory-backlog.json`](dark-factory-backlog.json).

---

## Non-negotiable topology

### Bare-metal and infrastructure

```text
server-debian
  Login: df-server@server-debian
  LAN address: 10.1.10.156
  Tailscale address: 100.102.154.23
  Purpose: Proxmox host and administrative jump host; explicit SSH Ansible target

infra-lab-services
  Login from server-debian: infra-lab-user@10.1.10.2
  LAN address: 10.1.10.2
  Tailscale address: 100.94.49.45
  Purpose: central Docker services and authoritative factory state
```

The VM address is **10.1.10.2**, not `10.0.10.2`.

Run central services on `infra-lab-services`, not on the bare-metal Proxmox host and not permanently on a worker laptop.

### Workers

All current workers have the `coder` role and share the durable consumer name `agent-workers-coder`.

| Host | Worker ID | Tailscale address | Login user | Role |
|---|---|---|---|---|
| Raphael | `worker-01` | `100.116.35.103` | `raphael` | `coder` |
| Inkii | `worker-02` | `100.98.125.127` | `inkii` | `coder` |
| Donatello | `worker-03` | `100.94.145.104` | `bgurrol4` | `coder` |
| Naruto | `worker-04` | `100.88.92.78` | `naruto` | `coder` |

Do not rename these worker IDs to hostnames. Worker ID and hostname are separate fields.

Each worker already has a distinct LiteLLM key source in the tracked Ansible inventory. Those keys are model-access credentials only. Never reuse them as dashboard, monitoring, activity-ingestion, Forgejo, PostgreSQL, or NATS credentials.

### Last observed availability

The operator subsequently restored both central hosts and supplied a running-service snapshot. The captured deployment branch `70838b3` is now reconciled locally with the dashboard, but the combined source has not been deployed centrally. Use [infra-reconciliation.md](infra-reconciliation.md) for the current decisions and staged deployment procedure.

---

## Service responsibilities

| Service | Authoritative responsibility |
|---|---|
| Forgejo | Git repositories, branches, commits, pull requests, reviews, issues, releases, human Git identity |
| Agent Manager | Tasks, runs, worker events, time entries, normalized activity, dashboard API |
| Agent Manager PostgreSQL | Durable workflow and dashboard ledger |
| NATS JetStream | Durable worker queue and run/heartbeat events |
| Prometheus | Numeric time-series metrics and scrape health |
| Node Exporter | Per-node CPU, memory, disk, network, and uptime metrics |
| Grafana | Detailed historical metrics, log investigation, and administrator diagnostics |
| Loki | Raw searchable logs when deployed |
| Grafana Alloy | Journald/log forwarding when deployed |
| Caddy | HTTPS ingress for Forgejo, Grafana, and Agent Manager routes |
| LiteLLM | Scoped model access for workers |
| Ansible | Repeatable deployment from the approved controller over Tailscale or the VM LAN path |

Data flow:

```text
Node Exporter -> Prometheus -> Dark Factory current summaries
                           \-> Grafana historical diagnostics

journald -> node activity collector -> Agent Manager normalized evidence
         \-> Alloy/Loki when available for raw logs

Forgejo -> signed webhook -> Agent Manager activity

Agent worker -> NATS -> Agent Manager -> PostgreSQL -> SSE -> browser
```

---

## Current local development state

Donatello currently runs a local development stack:

```text
http://127.0.0.1:8090/dashboard/
```

Local settings must use:

```dotenv
DASHBOARD_PUBLIC_URL=http://127.0.0.1:8090
DASHBOARD_SESSION_SECURE=false
```

`localhost` and `127.0.0.1` are distinct browser origins. Use `127.0.0.1` consistently for the local stack.

Forgejo OAuth is not configured locally. The local login uses `AGENT_MANAGER_API_TOKEN` and maps to the bootstrap identity:

```text
api-admin / API administrator
```

This identity is suitable for development and recovery only. It does not provide individual human attribution.

Do not migrate temporary local node keys to production unless the entire local Agent Manager PostgreSQL database is intentionally migrated.

---

## Implemented repository changes

### Agent Manager backend

Relevant files:

```text
Docker-Documents/agent-manager-service/agent_manager/
  auth.py
  dashboard.py
  database.py
  live.py
  main.py
  migrations.py
  models.py
  settings.py
  tracking.py
```

Implemented:

- Forgejo OAuth2 support.
- Bootstrap bearer-token login.
- Signed `HttpOnly`, `SameSite=Lax` browser sessions.
- CSRF protection for browser mutations.
- Viewer, member, manager, and administrator roles.
- Tasks with status, priority, deadline, human assignment, worker assignment, repository, issue, and run links.
- Optimistic task concurrency using a required version and `409 Conflict`.
- Task audit history with before/after state.
- Soft task archival.
- Worker heartbeat, offline derivation, role, hostname, version, capabilities, current run, and last error.
- Run creation, running status, completion, failure, and cancellation requests (not active worker interruption).
- Normalized activity timeline.
- Signed and idempotent Forgejo webhook ingestion.
- Human identity mappings for Linux and other external identities.
- Per-node `activity:write` credentials stored as keyed hashes.
- Idempotent batched access-event ingestion.
- Explicit work timers with one-running-timer-per-person database enforcement.
- Time stop, submit, approve, and reject transitions.
- Prometheus current-health queries.
- Configurable Grafana deep links.
- Deduplicated 24-hour, 2-hour, and overdue notification webhook delivery.
- Prometheus metrics for Agent Manager operations.
- Versioned schema migrations.

### Dashboard frontend

Relevant files:

```text
Docker-Documents/agent-manager-service/agent_manager/static/
  index.html
  app.js
  styles.css
```

Implemented views:

- Overview.
- Tasks.
- Work time.
- Nodes.
- Access evidence.
- Workers.
- People.
- Runs.
- Activity.

The Nodes view shows current Prometheus summaries and links to Grafana globally and per node. The dashboard document uses `Cache-Control: no-store`; static assets use versioned URLs to avoid stale login behavior.

### Node activity collector

Relevant files:

```text
node-activity-collector/
  collector.py
  README.md
  test_collector.py
```

Implemented:

- Journald cursor-based consumption.
- SSH login, logout, session, and failed-authentication normalization.
- `sudo` use without forwarding raw commands.
- Selected Tailscale identity events.
- Agent worker systemd state events.
- Bounded batches.
- Retry with replay-safe cursor advancement.
- Node-scoped `X-Node-Key` authentication.
- No passwords, private keys, shell input, environment dumps, or raw sudo commands.

### Ansible

Relevant additions:

```text
Ansible/playbooks/node-activity-collectors.yml
Ansible/playbooks/node-metrics-exporters.yml
Ansible/roles/node_activity_collector/
Ansible/roles/node_metrics_exporter/
```

The active inventory preserves worker IDs and current Tailscale addresses. `infra-lab-services` is reached as `infra-lab-user@10.1.10.2` from the controller.

Controller-side node activity keys are expected under:

```text
/home/infra-lab-user/.config/dark-factory/activity-keys/
```

They must be mode `0600`, copied with `no_log: true`, and never committed.

### Prometheus and Grafana

Relevant files:

```text
Docker-Documents/grafana_services/prometheus/prometheus.yml
Docker-Documents/grafana_services/grafana/provisioning/datasources/prometheus.yml
Docker-Documents/grafana_services/grafana/provisioning/dashboards/dark-factory.yml
Docker-Documents/grafana_services/grafana/provisioning/dashboards/json/dark-factory-node.json
```

Implemented:

- Scrape targets for all four workers and `server-debian`.
- Stable `node_id` labels.
- Stable Prometheus datasource UID `prometheus`.
- Provisioned Grafana dashboard UID `dark-factory-node`.
- Reachability, CPU, memory, disk, network, and six-hour history panels.
- Dashboard-to-Grafana and Grafana-to-dashboard links.

Grafana remains separately authenticated. Do not enable anonymous access or expose Grafana credentials in browser JavaScript.

---

## Known identity state and required migration

Forgejo currently has one shared central administrator account. Do not record its email address or password in this repository. Do not continue routine team activity through that shared account after recovery.

Required individual Forgejo users:

```text
raphael
inkii
donatello
naruto
```

After central recovery:

1. Sign in using the existing Forgejo administrator.
2. Create the four individual human accounts in Site Administration.
3. Require unique passwords and password changes.
4. Enable two-factor authentication.
5. Add one unique SSH public key per person.
6. Add the users to an organization team with repository write and pull-request permissions.
7. Keep default branches protected.
8. Retain the shared administrator only for recovery/site administration.

Individual Forgejo users are human profiles. They do not replace worker IDs or worker-specific LiteLLM keys.

### Forgejo OAuth application

Create an OAuth2 application with callback:

```text
https://infra-lab-services.tail494f6d.ts.net/auth/forgejo/callback
```

Set central Agent Manager variables:

```dotenv
FORGEJO_PUBLIC_URL=https://infra-lab-services.tail494f6d.ts.net/forgejo
FORGEJO_OAUTH_CLIENT_ID=<secret-client-id>
FORGEJO_OAUTH_CLIENT_SECRET=<secret-client-secret>
DASHBOARD_PUBLIC_URL=https://infra-lab-services.tail494f6d.ts.net
DASHBOARD_SESSION_SECURE=true
DASHBOARD_ADMINS=<designated-forgejo-login>
```

Each person must sign into the dashboard once through Forgejo before an administrator can assign their dashboard role.

Recommended dashboard roles:

```text
designated operator -> admin
raphael             -> manager
inkii               -> manager
donatello            -> manager
naruto               -> manager
```

Use `manager` because all four need to change assignments, priorities, and deadlines. Keep site-administrator access narrower than dashboard-manager access.

---

## Secrets and credential boundaries

Never request or print secret values during implementation or verification.

| Credential | Scope | Storage |
|---|---|---|
| Forgejo administrator password | Forgejo recovery | Password manager only |
| Forgejo OAuth client secret | OAuth exchange | Central Agent Manager `.env` |
| Agent Manager API token | Bootstrap/machine administration | Central Agent Manager `.env` |
| Dashboard session secret | Cookie signing | Central Agent Manager `.env` |
| Forgejo webhook secret | Forgejo event signature | Forgejo + central Agent Manager `.env` |
| PostgreSQL passwords | Database access | Service-specific `.env` |
| LiteLLM worker key | Model requests for one worker | Existing worker/controller secret path |
| Node activity key | `activity:write` for one node | Controller key file + `/etc/dark-factory/activity-key` |
| NATS credential | Queue/event subjects | Future per-worker secret file/configuration |
| Grafana administrator password | Grafana administration | Grafana `.env`/password manager |

Do not reuse any credential across purposes.

Do not paste `docker compose config` output into issues or chat; it expands secrets.

If a real password or key has been exposed in chat, Git, logs, or tickets, rotate it after the service is restored.

---

## Outstanding work

### 1. Restore physical infrastructure

This is an external prerequisite, not a code change.

1. Power on and inspect `server-debian` physically.
2. Verify Proxmox.
3. Start VM 100, `infra-lab-services`.
4. Verify `10.1.10.2` is reachable from `server-debian`.
5. Verify Tailscale connectivity for `100.94.49.45`.
6. Verify storage and filesystem health before starting all services.

### 2. Reconcile `factory-testing`

The operator reports that `factory-testing` contains or was intended to contain work around:

- The first Inkii smoke test.
- Duplicate worker processes.
- Runs not completing.
- User notifications.
- Ansible worker accessibility.

Before deployment:

1. Fetch all branches.
2. Compare `factory-testing` with the branch containing this dashboard work.
3. Preserve valid worker process-management and smoke-test fixes.
4. Reconcile event subjects and payload fields with the current worker protocol.
5. Remove obsolete duplicate implementations; do not add compatibility shims.
6. Run one complete local regression before updating central services.

Do not overwrite or reset either branch without reviewing the diff.

### 3. Back up central state

Before upgrading:

- Back up Forgejo PostgreSQL and `forgejo_data`.
- Back up Agent Manager PostgreSQL and NATS volumes.
- Back up Grafana and Prometheus volumes.
- Preserve deployment `.env` files through the approved secret process.
- Verify at least one restore path before destructive migrations.

### 4. Deploy central services

From the approved checkout on `infra-lab-services`:

```bash
cd Docker-Documents/agent-manager-service
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1:8090/readyz
```

Expected readiness:

```json
{"status":"ok","database":"ok","nats":"ok"}
```

Merge, validate, and reload the tracked Caddy Agent Manager routes before external testing. Do not overwrite an independently modified live Caddyfile without reviewing it.

### 5. Deploy Prometheus and Grafana changes

```bash
cd Docker-Documents/grafana_services
sudo docker compose config --quiet
sudo docker compose up -d --force-recreate prometheus grafana
sudo docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.yml
```

Verify Grafana dashboard UID `server-overview` exists and Dark Factory links select `var-host=infra-lab-services` or the node's observed Prometheus host label. The optional `dark-factory-node` provisioning does not replace this existing dashboard.

### 6. Create individual Forgejo profiles and OAuth

Follow the identity migration above. Verify each user appears separately in Forgejo pushes, OAuth login, dashboard task mutation, and work-time records.

### 7. Create central node activity keys

Using the central Agent Manager administrator token, create distinct keys for:

```text
worker-01
worker-02
worker-03
worker-04
infra-lab-services
server-debian
```

Store plaintext tokens once under:

```text
/home/infra-lab-user/.config/dark-factory/activity-keys/<node-id>
```

Agent Manager stores only keyed hashes. A node key must be rejected when its body claims another `node_id`.

### 8. Create identity mappings

After OAuth users exist, map external identities to dashboard users. At minimum map each worker's Linux username. Shared accounts cannot prove individual human action; prefer individual Unix accounts on shared servers.

### 9. Deploy exporters and collectors

From the Ansible project on the approved controller:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/node-metrics-exporters.yml --syntax-check
ansible-playbook -i inventory/hosts.yml playbooks/node-metrics-exporters.yml --check --diff
ansible-playbook -i inventory/hosts.yml playbooks/node-metrics-exporters.yml

ansible-playbook -i inventory/hosts.yml playbooks/node-activity-collectors.yml --syntax-check
ansible-playbook -i inventory/hosts.yml playbooks/node-activity-collectors.yml --check --diff
ansible-playbook -i inventory/hosts.yml playbooks/node-activity-collectors.yml
```

Roll out with `--limit` in this order:

```text
inkii -> donatello -> raphael -> naruto
```

Do not deploy all workers until the Inkii smoke path is verified.

### 10. Add per-worker NATS authentication

This is not yet implemented. Current NATS exposure is Tailscale-bound but not cryptographically tied to each worker ID.

Required credentials:

```text
worker-01 credential -> Raphael
worker-02 credential -> Inkii
worker-03 credential -> Donatello
worker-04 credential -> Naruto
manager credential   -> Agent Manager
```

Worker permissions must allow only the required JetStream pull-consumer API/inbox subjects and the worker heartbeat/run status/completion publish subjects. Workers must not receive the manager credential. Bind NATS only to the VM Tailscale address; never publish unauthenticated NATS on `0.0.0.0`.

### 11. Configure deadline notification adapter

Agent Manager supports a generic webhook:

```dotenv
NOTIFICATION_WEBHOOK_URL=https://<approved-adapter>/events
NOTIFICATION_WEBHOOK_TOKEN=<adapter-scoped-token>
NOTIFICATION_SCAN_SECONDS=60
```

Connect it to Matrix, email, or another approved channel. Delivery is deduplicated by task and threshold. Do not send one alert per dashboard refresh.

### 12. Add Alloy and Loki when central services are stable

The normalized collector is implemented. Raw logs still need an approved Alloy-to-Loki deployment if searchable long-term logs are required. Keep raw logs out of Agent Manager PostgreSQL.

---

## Required acceptance tests

### Authentication and identity

- [ ] Unauthenticated `/v1` request returns `401`.
- [ ] Local bootstrap login works only with the local Agent Manager token.
- [ ] OAuth login creates an individual user.
- [ ] Four team members appear as distinct users.
- [ ] Viewer/member/manager/admin boundaries are enforced.
- [ ] Task audit records identify the correct person.
- [ ] Shared administrator usage is limited to recovery.

### Tasks and time

- [ ] Create, update, complete, cancel, and archive tasks.
- [ ] Stale task version returns `409` and does not overwrite.
- [ ] Overdue state uses an absolute timezone-aware deadline.
- [ ] Only one running timer exists per person.
- [ ] Timer stops with measured duration.
- [ ] Owner submits time.
- [ ] Manager approves or rejects time.
- [ ] Approved and submitted totals remain separate.
- [ ] Login duration is never automatically converted to worked time.

### Worker protocol

- [ ] One submitted run reaches one coder worker.
- [ ] Run transitions `queued -> running -> completed` or `failed`.
- [ ] One worker process runs per laptop.
- [ ] Duplicate completion delivery creates no duplicate activity.
- [ ] Worker identity and hostname are both retained.
- [ ] Worker becomes offline after missed heartbeats.
- [ ] Reconnecting worker returns online without heartbeat timeline spam.

### Forgejo

- [ ] Valid webhook signature is accepted.
- [ ] Invalid signature returns `403`.
- [ ] Replayed delivery is marked duplicate.
- [ ] Push, PR, issue, branch, and release events appear once.
- [ ] Pusher, commit author, and committer remain distinct.
- [ ] Protected branch cannot be directly changed by a worker.

### Node activity

- [ ] Each node key can submit only its own `node_id`.
- [ ] Cross-node key use returns `403`.
- [ ] Replayed journald event is deduplicated.
- [ ] SSH success and failure appear in Access.
- [ ] `sudo` evidence contains no raw command.
- [ ] External identity mapping attributes the correct user.
- [ ] No secret appears in activity payloads or logs.

### Prometheus and Grafana

- [ ] Prometheus config passes `promtool`.
- [ ] Every expected target has a stable `node_id`.
- [ ] Nodes view shows current health when Prometheus is available.
- [ ] Prometheus outage degrades only the Nodes summary.
- [ ] Existing Infrastructure – Server dashboard (`server-overview`) remains available.
- [ ] Global Grafana link opens the dashboard.
- [ ] Per-node link preserves the selected `host` variable, not a worker ID.
- [ ] Grafana still requires authentication.
- [ ] No Grafana credential appears in browser source or API responses.

### Notifications

- [ ] `due_24h`, `due_2h`, and `overdue` send once each.
- [ ] Successful delivery is not repeated after restart.
- [ ] Failed delivery retains error and attempt count.
- [ ] Notification adapter receives no unrelated secrets.

---

## Existing verification evidence

The current working tree has already been exercised locally with:

- Python compilation and browser JavaScript syntax checks.
- Agent Manager contract tests.
- Node collector normalization tests.
- Docker Compose validation.
- Ansible syntax checks for collector and exporter playbooks.
- Prometheus `promtool` configuration validation.
- Grafana provisioning into Grafana 12.1.1.
- Grafana API retrieval of `dark-factory-node`.
- Actual browser navigation from the Dark Factory Nodes view to authenticated Grafana.
- Node key acceptance, cross-node rejection, and replay deduplication.
- Timer start, conflict, stop, submit, and approval.
- Deadline webhook delivery.
- Prometheus-unavailable degradation.
- Donatello `worker-03` deterministic run `9544f6b4-5d1e-4b2b-9423-aa5d7bcf2752` reached `completed` with return code `0`, expected stdout, empty stderr, and one stored result.
- Foreground worker `Ctrl+C` cleanup exits `0`, prints `worker stopped`, leaves no worker process, and emits no traceback.

These local checks are not substitutes for central deployment or four-worker acceptance.

---

## Commands for local Donatello development

Start the local stack:

```bash
cd Docker-Documents/agent-manager-service
docker compose -f compose.yml -f compose.worker-test.yml up -d --build
curl -fsS http://127.0.0.1:8090/readyz
```

Open:

```text
http://127.0.0.1:8090/dashboard/
```

Use the local `AGENT_MANAGER_API_TOKEN`. Do not use a LiteLLM key, Forgejo password, PostgreSQL password, node activity key, or token from another deployment.

Stop without deleting data:

```bash
docker compose -f compose.yml -f compose.worker-test.yml down
```

Never add `-v` unless local PostgreSQL and NATS data should be destroyed intentionally.

---

## Engineering constraints for the next agent

- Preserve the topology, addresses, worker IDs, and all-coder role assignment above.
- Treat operator-reported infrastructure state as authoritative.
- Do not ask for, print, log, or commit secrets.
- Do not reuse credentials across services.
- Keep PostgreSQL authoritative for workflow state, NATS for transport, Forgejo for Git, Prometheus for metrics, and Grafana for investigation.
- Do not infer worked hours from login, Git, heartbeat, or laptop uptime.
- Keep explicit timer approval separate from access evidence.
- Do not add anonymous Grafana or dashboard access.
- Do not expose PostgreSQL, Prometheus, or unauthenticated NATS publicly.
- Do not create a second task system in Forgejo or Grafana.
- Do not create compatibility aliases when reconciling branches; migrate all callers and remove obsolete paths.
- Do not mark central deployment complete while `server-debian` or `infra-lab-services` is offline.

---

## Definition of done

The handoff is complete only when:

1. Central infrastructure is restored and backed up.
2. Branches are reconciled without losing Inkii's smoke-test/process fixes.
3. Central Agent Manager, Prometheus, and Grafana deploy successfully.
4. Four individual Forgejo/OAuth users exist.
5. All four people can update authorized tasks and deadlines.
6. Each worker has its stable ID, own LiteLLM key, own activity key, and eventually own NATS credential.
7. One run is consumed by exactly one worker and completes once.
8. Git, worker, task, timer, login, metric, and notification evidence are visible in the correct system.
9. Grafana deep links open the selected node's historical diagnostics.
10. Every acceptance test above has observed evidence and no secret is exposed.
