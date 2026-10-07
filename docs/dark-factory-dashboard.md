# Dark Factory dashboard operations

The dashboard is the human control surface for Agent Manager. PostgreSQL remains the authoritative ledger; NATS transports run and worker events; Forgejo remains authoritative for code and repository identity. Grafana remains the numeric observability surface.

## Capabilities

- Overview of worker presence, active and blocked work, deadlines, and run state.
- Editable tasks with priorities, UTC deadlines, human or worker assignment, optimistic concurrency, soft archival, and an immutable audit history.
- Forgejo-backed human identities and role-based dashboard access.
- Worker registration, heartbeat, capability, current-run, and status tracking.
- Signed, idempotent Forgejo push, branch, pull-request, issue, and release event ingestion.
- A normalized activity timeline for task, run, worker, and Forgejo events.
- Server-Sent Events (SSE) for live browser refresh.

The dashboard sees server-side Git activity. It cannot observe uncommitted local file changes. Do not present local hooks as an authoritative audit mechanism.

## Request flow

```text
Browser -> Caddy -> Agent Manager -> PostgreSQL
                              |----> NATS JetStream
Forgejo webhook --------------|
Worker heartbeat -> NATS -----|
```

Public routes are `/dashboard/`, `/auth/*`, and `/v1/*`. PostgreSQL, NATS, and `/metrics` are not routed publicly by Caddy.
For an existing central deployment, follow the
[manager-only rollout and recovery guide](central-dashboard-deployment.md).
Do not run a blanket Compose update against the live VM; the installation
commands below are for a new isolated stack. The optional
[Open WebUI Pipe](../Docker-Documents/open_webui_services/README.md#agent-manager-coder-function-manual-opt-in)
is installed separately by an administrator and needs a disposable worker.


## Initial configuration

Copy `.env.example` to `.env` in `Docker-Documents/agent-manager-service`. Set at least:

```dotenv
POSTGRES_PASSWORD=<unique database password>
AGENT_MANAGER_API_TOKEN=<unique bootstrap and automation token>
DASHBOARD_SESSION_SECRET=<independent random session-signing secret>
DASHBOARD_PUBLIC_URL=https://infra-lab-services.tail494f6d.ts.net
DASHBOARD_SESSION_SECURE=true
DASHBOARD_ADMINS=<comma-separated Forgejo logins>
FORGEJO_WEBHOOK_SECRET=<unique webhook secret>
```

Generate independent values rather than reusing a database, webhook, or API secret:

```bash
openssl rand -base64 48
```

`DASHBOARD_SESSION_SECRET` falls back to `AGENT_MANAGER_API_TOKEN` for compatibility, but production deployments should always set an independent value.

### Forgejo OAuth

1. Sign in to Forgejo as an administrator.
2. Create an OAuth2 application.
3. Set its redirect URI exactly to:

   ```text
   https://infra-lab-services.tail494f6d.ts.net/auth/forgejo/callback
   ```

4. Put its client ID and secret in `.env`:

   ```dotenv
   FORGEJO_PUBLIC_URL=https://infra-lab-services.tail494f6d.ts.net/forgejo
   FORGEJO_OAUTH_CLIENT_ID=<client-id>
   FORGEJO_OAUTH_CLIENT_SECRET=<client-secret>
   ```

5. Recreate Agent Manager.
6. Open `/dashboard/` and use **Continue with Forgejo**.

A login listed in `DASHBOARD_ADMINS` becomes an administrator when first observed. Changing the environment later does not silently overwrite an existing database role; use the People screen to change established roles.

The API-token login is bootstrap access. It maps to the `api-admin` identity and should not be the normal human login.
`api-admin` and `service:open-webui` are reserved service identities, not
Forgejo accounts. A Forgejo login must carry a valid immutable Forgejo user ID;
renamed or recycled logins cannot inherit another person's dashboard role.
Disabled dashboard accounts cannot re-enter through OAuth.

### Forgejo webhook

Create an organization webhook when all organization repositories should be tracked; otherwise create one per repository.

- Target URL: `https://infra-lab-services.tail494f6d.ts.net/v1/webhooks/forgejo`
- Content type: `application/json`
- Secret: the exact `FORGEJO_WEBHOOK_SECRET` value
- Events: push, create, delete, pull request, issue, and release

Agent Manager verifies `X-Forgejo-Signature` as a hexadecimal HMAC-SHA256 of the raw request body. `X-Forgejo-Delivery` is the idempotency key. When Forgejo omits it, the body SHA-256 is used. Replayed deliveries return success without creating another activity row.

Do not place the OAuth client secret or webhook secret in webhook URLs.

## New-stack install (not a live VM upgrade)

The following commands create/update a new isolated stack. For the existing VM,
use [the guarded central rollout](central-dashboard-deployment.md), which backs
up state and replaces only the Agent Manager service.


```bash
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1:8090/readyz
```

Agent Manager creates missing tables and applies tracked, transactional schema migrations during startup before it consumes events. A failed schema migration prevents readiness; inspect the manager log rather than editing the database manually.

Validate Caddy after changing its route:

```bash
cd ../caddy_service
sudo docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose restart caddy
```

Then open:

```text
https://infra-lab-services.tail494f6d.ts.net/dashboard/
```

## User and permission operations

Roles are:

| Role | Read dashboard | Create tasks | Edit owned/assigned tasks | Reassign/deadline | Manage users |
|---|---:|---:|---:|---:|---:|
| viewer | yes | no | no | no | no |
| member | yes | yes | yes | no | no |
| manager | yes | yes | yes | yes | no |
| admin | yes | yes | yes | yes | yes |

An administrator changes roles from **People**. The API rejects disabling or demoting the currently authenticated administrator, preventing an accidental self-lockout.

Sessions last 12 hours, use `HttpOnly` and `SameSite=Lax`, and are signed. Browser mutations require the session CSRF token. Machine clients use the bearer token and do not receive browser sessions.

## Task operations

### Create

Select **Tasks -> New task**. Title is required. Status, priority, deadline, person, worker, and repository are optional. Browser-local date/time input is converted to an absolute UTC timestamp before submission.

### Update

Open a task card, change fields, and save. Each task has a version. If another session saves first, the stale update receives `409 Conflict`; close and reopen the form before applying the intended change. The server never silently overwrites a concurrent update.

Members can update tasks they created or that are assigned to them. Only managers and administrators can change assignees, priority, or deadline.

### Complete or cancel

Set status to `done` or `cancelled`. Agent Manager records `completed_at`. Returning the task to a nonterminal status clears that timestamp.

### Archive

Managers and administrators can archive a task. Archival is a soft delete: normal lists hide it, while audit and activity records remain.

### API example

```bash
export AGENT_MANAGER_API_TOKEN='<token>'

curl -fsS \
  -H "Authorization: Bearer ${AGENT_MANAGER_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  https://infra-lab-services.tail494f6d.ts.net/v1/tasks \
  -d '{
    "title": "Validate worker upgrade",
    "priority": "high",
    "due_at": "2030-01-02T15:00:00Z",
    "repository": "owner/project"
  }'
```

For an update, send the version returned by the prior read:

```json
{"version": 3, "status": "in_progress"}
```

## Worker operations

Workers publish `agent.workers.heartbeat` every 15 seconds. The payload includes a unique event ID, worker ID, role, hostname, status, current run, version, capabilities, optional last error, and timezone-aware send time.

Agent Manager treats a worker as offline when its latest heartbeat is older than `WORKER_OFFLINE_AFTER_SECONDS` (45 seconds by default). Offline is derived at read time; no cleanup job rewrites worker rows.

A worker publishes:

1. An idle heartbeat after connecting.
2. A busy heartbeat and `agent.runs.status` event before executing a run.
3. `agent.runs.completed` after execution.
4. An idle heartbeat with the latest error, if any.
5. Periodic heartbeats while idle or busy.

Configure optional worker metadata:

```bash
export WORKER_VERSION='0.2.0'
export WORKER_HEARTBEAT_INTERVAL=15
export WORKER_CAPABILITIES='python-script,git-read'
```

Use a heartbeat interval comfortably below the offline threshold. Do not advertise a capability that the worker cannot safely execute.

## Run operations

Existing run endpoints remain compatible with bearer clients:

- `POST /v1/runs`
- `GET /v1/runs`
- `GET /v1/runs/{run_id}`
- `POST /v1/runs/{run_id}/cancel`

Run status becomes `running` when the worker publishes `agent.runs.status`, then `completed` or `failed` from the completion event. Redelivered events are deduplicated in the activity ledger.

Admission now preserves the central worker-health gate: no matching
execution-enabled responder returns `503 NO_WORKER_AVAILABLE` with no new run
row. Health is a preflight check, not a lease. Cancellation remains a request;
the current Python worker does not interrupt running code on that event.

## Activity and Git operations

The Activity view normalizes sources without replacing their authoritative stores:

- `forgejo`: pushes, refs, pull requests, issues, and releases.
- `worker`: registration, state transitions, run starts, and completions.
- `task`: task creation, updates, and archival.
- `run`: run creation.

For pushes, the authenticated pusher, commit author, and committer remain separate fields. Do not use commit count as a productivity score.

## Backups and restore

Back up both named volumes:

```text
agent-manager_postgres_data
agent-manager_nats_data
```

PostgreSQL contains tasks, audit records, users, workers, activity, and run state. NATS contains durable queued and retained events. Restore both to recover the complete control-plane history. Forgejo repositories and metadata require their own backup.

After a restore, verify:

```bash
curl -fsS http://127.0.0.1:8090/readyz
curl -fsS -H "Authorization: Bearer ${AGENT_MANAGER_API_TOKEN}" \
  http://127.0.0.1:8090/v1/dashboard/summary
```

## Troubleshooting

### Dashboard falls through to Proxmox

The deployed Caddyfile lacks the Agent Manager matcher or Caddy was not reloaded. Validate the mounted configuration and confirm both containers share `caddy_proxy`.

### Forgejo login returns 503

`FORGEJO_OAUTH_CLIENT_ID` or `FORGEJO_OAUTH_CLIENT_SECRET` is empty. Set both and recreate Agent Manager.

### Forgejo rejects the callback

The OAuth application's redirect URI must exactly match `DASHBOARD_PUBLIC_URL + /auth/forgejo/callback`, including scheme, host, and path.

### Webhook returns 403

The Forgejo webhook and Agent Manager secrets differ, or a proxy altered the body. Compare configuration without printing secrets. Forgejo signs the raw body, so verification must happen before JSON normalization.

### Webhook is accepted twice but appears once

This is expected idempotency. Inspect `X-Forgejo-Delivery`; repeated delivery IDs represent the same event.

### Worker appears offline

Confirm the process is running, its NATS URL is reachable, `AGENT_WORKERS` exists, and the worker clock is synchronized. Inspect the manager log for rejected worker payloads. Do not increase the offline threshold merely to hide a broken heartbeat path.

### Task update returns 409

Another request updated the task version. Fetch the task again and reapply the intended fields.

### Task mutation returns 403

For browser sessions, confirm the user role and CSRF header. For machine clients, send `Authorization: Bearer <AGENT_MANAGER_API_TOKEN>`.

## Performance and retention

Task, run, worker, activity time, repository, status, assignee, and due-date lookup paths are indexed. List endpoints are bounded. Routine heartbeats update one worker row and only state transitions create activity rows, avoiding an unbounded heartbeat audit stream.

SSE is an in-process fan-out optimized for the current single Agent Manager process. If the service is intentionally scaled to multiple API processes, replace this broker with a shared NATS-backed fan-out before scaling; otherwise browsers connected to one process will not receive events emitted by another.

## MCP contract

The machine-consumable operation contract and safe tool mapping are documented in [`mcp-dashboard.md`](mcp-dashboard.md). It documents an adapter contract; it does not claim that an MCP server is currently deployed.

## Central topology and identities

Run the authoritative dashboard on `infra-lab-services`. The bare-metal
`server-debian` host remains the hypervisor and administrative jump host.
The captured worker provisioning runs as `infra-lab-user` on the infrastructure
VM, reached at `10.1.10.2` or Tailscale `100.94.49.45`. Keep controller-side
key files there until an explicit controller migration is planned.

All current workers have the `coder` role:

| Host | Worker ID | Tailscale address |
|---|---|---|
| Raphael | `worker-01` | `100.116.35.103` |
| Inkii | `worker-02` | `100.98.125.127` |
| Donatello | `worker-03` | `100.94.145.104` |
| Naruto | `worker-04` | `100.88.92.78` |

Do not replace these IDs with hostnames in worker events. The dashboard shows
both the stable worker ID and reported hostname.

## Access evidence

`POST /v1/ingest/events` accepts batches of up to 100 normalized events. It
requires a node-scoped `X-Node-Key`; the shared Agent Manager administrator
token is rejected for this endpoint. Node credentials have only the
`activity:write` scope and are stored as keyed hashes.

Raw journald records belong in journald or Loki. PostgreSQL stores selected
normalized evidence: node, mapped person, observed identity, service, event
type, source, session reference, remote identity, timestamps, and bounded
metadata. Every `(node_id, event_id)` is unique and safe to replay.

Collector installation and key provisioning are documented in
[`../node-activity-collector/README.md`](../node-activity-collector/README.md).

Administrators correlate Linux, Tailscale, or service identities with Forgejo
dashboard users through `/v1/identity-mappings`. An unmapped event remains
visible as evidence but is not attributed to a person.

## Explicit work time

Login duration is not work time. Each person uses the Time view to start and
stop a task-linked timer. A stopped entry is submitted by its owner and
approved or rejected by a manager.

```text
running -> stopped -> submitted -> approved
                            \----> rejected -> submitted
```

Only one running timer is allowed per person. The database enforces this even
when two browsers submit simultaneously. Managers can review all entries;
members and viewers receive only their own entries. Approved duration and
submitted duration are shown separately on the People view.

The work-session endpoints are:

```text
POST /v1/work-sessions/start
POST /v1/work-sessions/{id}/stop
POST /v1/work-sessions/{id}/submit
POST /v1/work-sessions/{id}/decision
GET  /v1/work-sessions
GET  /v1/people/summary
```

## Node health summaries

The Nodes view queries Prometheus through its HTTP API. Agent Manager and
Prometheus share `caddy_proxy`; node exporters remain direct Prometheus scrape
targets over Tailscale. The dashboard does not rescrape every node.

Configure:

```dotenv
PROMETHEUS_URL=http://prometheus:9090
PROMETHEUS_TIMEOUT=5
GRAFANA_PUBLIC_URL=https://infra-lab-services.tail494f6d.ts.net/grafana
GRAFANA_NODE_DASHBOARD_UID=server-overview
```

The Nodes view shows current Prometheus summaries and provides two kinds of
Grafana navigation:

- **Open Grafana** opens the existing Infrastructure – Server dashboard
  (`server-overview`) with `var-host=infra-lab-services`.
- **Open in Grafana** uses the node's Prometheus `host` label, not its worker
  ID. For example, `worker-03` selects `var-host=donatello`. A per-node link
  is omitted when the metrics do not supply a host label.
- Links preserve a six-hour time window, browser timezone, and 30-second refresh.

The repository also includes an optional `Dark Factory Node Detail` dashboard.
It is not the selected destination for the existing central Grafana deployment.
Worker selection requires that the existing server dashboard queries support
the worker's host label; a link does not create metrics or modify Grafana panels.

Grafana authentication remains separate. A dashboard user who follows a link
must also have an authorized Grafana session. The integration deliberately
does not put a Grafana administrator password or service token in browser
JavaScript, and it does not enable anonymous access or iframe embedding.

If Prometheus is unavailable, the Nodes view reports degraded observability
without breaking tasks, timers, runs, or access evidence. The Grafana link
remains visible for historical data and diagnostics.

## Deadline notifications

An optional webhook receives deduplicated task events at three thresholds:

```text
due_24h
due_2h
overdue
```

Configure:

```dotenv
NOTIFICATION_WEBHOOK_URL=https://<approved-adapter>/events
NOTIFICATION_WEBHOOK_TOKEN=<adapter-scoped-token>
NOTIFICATION_SCAN_SECONDS=60
```

Each task and threshold has one durable notification key. Successful delivery
is never repeated. Failed delivery is retained with its attempt count and last
error, retried on the next scan, and displayed on the dashboard. The adapter
may deliver to Matrix, email, or another approved channel; Agent Manager does
not embed a provider-specific secret format.

## Retention and privacy

- Use login evidence for security and attribution, not automatic payroll.
- Keep raw logs in Loki under an explicit retention policy.
- Keep normalized access and task audit records according to the team's agreed
  security and employment policy.
- Do not collect shell input, passwords, private keys, environment variables,
  or raw sudo commands.
- Synchronize every node with NTP so deadline, login, Git, and timer evidence
  can be compared reliably.
- Give every person an individual Forgejo identity and, where practical, an
  individual Unix account. Shared accounts weaken attribution.
