# Agent Manager

This Compose project deploys the first control-plane slice for agentic coding runs:

- `agent-manager`: FastAPI API, run-state persistence, and NATS JetStream publishing
- `postgres`: private PostgreSQL database for manager state
- `nats`: private NATS server with JetStream enabled and persistent storage
- `nats-exporter`: private Prometheus metrics for NATS clients, subscriptions, and JetStream

The Agent Manager is the central orchestrator: the OMP endpoint validates a
mission, estimates crew counts, stores the run, and publishes
`agent.runs.created`. NATS JetStream assigns it to one available coder worker.
For `mission` runs, the worker's local OMP calls the worker-scoped LiteLLM
endpoint. A model-backed planner creates acceptance criteria and coder tasks;
coders produce goal-driven files, and independent reviewers assess each attempt.
After ten unsuccessful reviewer attempts, the worker reports `needs_input` with
a specific question and retains a checkpoint. The user's clarification resumes
the same mission with `reviewer_count` reset to zero.

The worker validates the accepted artifact manifest, packages every mission
result as a ZIP (including a single-file result), and uploads it to Agent
Manager. PostgreSQL stores the ZIP bytes in `mission_artifacts`; the mission
status exposes a signed download link. The worker does not clone, modify, or
push the configured Forgejo repository, and it does not execute generated
files. The manager consumes `agent.runs.status` and `agent.runs.completed`;
`needs_input` is nonterminal, terminal events close the run, and late events
cannot reopen it.

OpenWebUI polls mission status and returns a ZIP download link rather than
inline source blocks. A later “what's the status?” message queries that same
mission instead of creating another run. The dashboard shows the current run
phase and worker, refreshing every 30 seconds; per-agent model calls are
summarized after the crew finishes rather than streamed live.

In the same Open WebUI conversation, a follow-up modification starts a new child
mission from the latest completed ZIP. The Manager accepts only a completed
artifact owned by the authenticated run actor; the worker starts from those
files and returns a new ZIP. The original mission and artifact remain unchanged.

OMP chat sanitizes inbound text and model replies before forwarding them to the
model or user. It redacts the configured model credential by exact match and
recognized OpenRouter/OpenAI/Anthropic/Google key formats. When a match is
found, OMP stores a signed, secret-free event in the manager database outbox
and attempts delivery to the separate
[`API Guard dashboard`](../api-guard-dashboard/README.md). Pending events retry
with backoff if API Guard is unavailable. Configure
`API_GUARD_EXPOSURE_WEBHOOK_URL` and the shared
`API_GUARD_EXPOSURE_WEBHOOK_SECRET`; the raw key, prompt, and model output are
never placed in the event. The manager's configured `MODEL_API_KEY` remains a
longer-lived LiteLLM virtual key and requires manual rotation in API Guard.
Pattern matching is not a general-purpose secret scanner; keep provider master
keys out of prompts and model context.


## API Guard exposure incidents

API Guard can POST signed exposure events to
`/v1/webhooks/api-guard/exposure`. The receiver accepts only the six safe
fields `event_id`, `worker_id`, `session_id`, `key_alias`, `reason`, and
`occurred_at`, verifies `X-Darkfactory-Signature: sha256=<hex>`, rejects
oversized or extra-field bodies, and commits the event before returning HTTP
202. PostgreSQL uses `event_id` as the durable deduplication key. Persisted
incidents and the admin-only Agent Manager dashboard contain no prompts, raw
credentials, or run IDs.
API Guard can also query `/v1/webhooks/api-guard/status` with
`X-Darkfactory-Signature` signing the exact bytes
`GET:/v1/webhooks/api-guard/status`. The response contains only booleans for
the signed-event receiver, external notification URL, and acknowledgment
callback configuration; it never returns secret values or URLs.


Set `API_GUARD_ALERT_WEBHOOK_URL=http://agent-manager:8000/v1/webhooks/api-guard/exposure`
and `API_GUARD_ALERT_WEBHOOK_SECRET` in API Guard's deployment. Set the same
`API_GUARD_ALERT_WEBHOOK_SECRET` in Agent Manager. This signed-event secret
must differ from `API_GUARD_EXPOSURE_WEBHOOK_SECRET`, which protects the
separate OMP/Keybroker-to-API-Guard exposure endpoint. A human administrator
can acknowledge incidents in the Agent Manager Operations dashboard; the
local acknowledgment is persisted before the signed callback is queued.
`API_GUARD_ALERT_ACK_URL` defaults to
`http://api-guard:8000/api-guard/api/v1/webhooks/alerts/ack`, and
`API_GUARD_ALERT_ACK_SECRET` must match API Guard's `API_GUARD_ALERT_ACK_SECRET`.
Keep the acknowledgment secret distinct from both event-delivery secrets.
Both event and callback outboxes retry failed deliveries durably.

When configured, the existing `NOTIFICATION_WEBHOOK_URL` and optional
`NOTIFICATION_WEBHOOK_TOKEN` deliver a safe incident event to the wider Forge
or an existing relay. A successful HTTP response means only that the relay
accepted the event; it is not a human acknowledgment. Both the initial event
and each escalation carry a fixed `severity: "critical"` label set by Agent
Manager; severity is not accepted from API Guard input. The dashboard reports
Agent Manager receipt, external delivery, local human acknowledgment, and
API Guard callback state separately. No external paging destination, on-call
schedule, or paging credentials are supplied here, so deployment must not
claim 24/7 paging or acknowledgment coverage until those are configured and
operationally verified.

Until a human administrator acknowledges an incident, Agent Manager resends
it to the configured notification webhook every
`API_GUARD_ALERT_ESCALATE_AFTER_SECONDS` (default 900 seconds) as
`api_guard.exposure.escalated`, incrementing `escalation_level` for each new
no-ack escalation. Failed delivery retries reuse the current level and use
their own durable backoff, independent of initial-event delivery retries.
A human acknowledgment stops future escalation; the separate signed callback
to API Guard is still retried if that service is unavailable.

The separate API Guard dashboard remains its own service and remains the
credential inventory and exposure source of truth; this incident panel is in
the wider Forge's Agent Manager dashboard.

The dashboard's worker runtime view aggregates `RunRecord` start and completion
timestamps by worker and UTC day. The
`/v1/dashboard/worker-execution?from=YYYY-MM-DD&to=YYYY-MM-DD` endpoint returns
elapsed seconds and run counts over a maximum 31-day range; active runs accrue
time until queried. SSH remains in Access evidence and is not used for worker
runtime. These are machine execution durations, not employee work hours.
The Work page also separates approximate dashboard activity from worker
runtime. Dashboard time counts one minute per foreground pulse while the page
is visible and recently active; it excludes SSH sessions and is not verified
work time.

The Work page's self-reported time table can be filtered by inclusive UTC
start-date bounds. Leave either bound blank for an open-ended range; leave both
blank to show the latest 500 entries. Running timers remain visible even when
their start date falls outside the selected range, so they remain available to
stop, edit, or discard. The list endpoint accepts `from`, `to`, and `limit`;
date filtering does not change the existing edit or approval rules.


Queued and running runs are reconciled after
`WORKER_RUN_STALE_AFTER_SECONDS` (default 1800). A running run is retained if
a fresh worker heartbeat still claims it. Otherwise Agent Manager marks it
failed, sets its terminal timestamp, and stops its displayed runtime. New queue
events expire after the same interval; a worker acknowledges an expired event
without executing it. Late status or completion events cannot reopen a terminal
run. This timeout exceeds the worker's 900-second JetStream acknowledgment
window to allow redelivery before finalization.

The human control surface is available at `/dashboard/` through Caddy. It
tracks runs, deadline-aware tasks, Forgejo users and activity, worker presence,
and audit history. This README covers the manager's dashboard, OAuth, webhook,
task, and worker behavior. The Ansible
[`Docker Compose deployment guide`](../../Ansible/README.md#docker-compose-deployment)
covers deployment paths and operations.

The Agents view presents the six canonical host names (Raphael, Inkii,
Donatello, Naruto, Sammy, and Michelangelo) rather than duplicate historical
worker IDs. Legacy heartbeat IDs are grouped under a known host or placed in a
separate unregistered section; history is not deleted. Workers without a
canonical heartbeat are visible but cannot be assigned to a task. Sammy and
Michelangelo are in Ansible inventory; deploy their workers and verify heartbeats
before treating them as ready.

For an **existing central VM**, preserve its live credentials, NATS overrides,
database volumes, Caddy routes, and manager image when applying manager-only
changes. The Ansible playbook deploys existing project directories; it does not
copy source files or create secret `.env` files.
Open WebUI integration is [opt-in](../open_webui_services/README.md#agent-manager-coder-function-manual-opt-in);
`create-python-script` executes generated Python as the worker user and is not sandboxed; restrict it to disposable workers. The `mission` demo container has no network and retains no candidate files after exit.

## Open the dashboard

After the dashboard release and Caddy route are deployed, connect a device to
the lab's Tailscale network and open:

```text
https://infra-lab-services.tail494f6d.ts.net/dashboard/
```

Choose **Continue with Forgejo** and sign in with your Forgejo account. Agent
Manager's Forgejo OAuth application must use this callback URL:

```text
https://infra-lab-services.tail494f6d.ts.net/auth/forgejo/callback
```

OAuth access is allowlisted by `DASHBOARD_MEMBERS` (defaults to
`raphael`, `inkii`, `bgurrol4`, and `naruto`). These Forgejo users sign in as
shared-workspace members; browser mutations require the session CSRF token.
The API and Open WebUI bearer tokens are not dashboard login credentials.

Caddy uses a private internal CA. The browser device must trust its root
certificate; do not bypass browser certificate warnings. See the
[Caddy setup guide](../caddy_service/caddy-setup.md) for TLS details.

A repository checkout does not deploy the dashboard. For an existing central
VM, follow the [guarded manager-only deployment
procedure](../../docs/central-dashboard-deployment.md) and its post-deployment checks.

## Networks

The manager joins the existing networks used by the lab:

- `caddy_proxy` for Forgejo and LiteLLM HTTP/API access and Caddy routing
- `open-webui_internal` for Ollama access
- `agent_manager_internal` for PostgreSQL and NATS

The manager does **not** join `forgejo_internal`; Forgejo's database network remains isolated.

The NATS exporter joins only `agent_manager_internal` and `caddy_proxy`.
It reads `http://nats:8222` with `-varz -connz -subz -jsz=all` and exposes
`nats-exporter:7777` to Prometheus without publishing a host port. Its pinned
default image is `natsio/prometheus-nats-exporter:0.20.2`; like NATS, it uses
`restart: unless-stopped`.

Caddy and the Open WebUI Compose project must already be running so that the two external networks exist:

```bash
export REPO_ROOT=/path/to/TheGarden

cd "$REPO_ROOT/Docker-Documents/caddy_service"
sudo docker compose up -d caddy

cd "$REPO_ROOT/Docker-Documents/open_webui_services"
sudo docker compose up -d
```

## Configure and start

```bash
export REPO_ROOT=/path/to/TheGarden
cd "$REPO_ROOT/Docker-Documents/agent-manager-service"
cp .env.example .env
chmod 600 .env
```

Set unique values for at least:

```dotenv
POSTGRES_PASSWORD=use-a-long-random-database-password
AGENT_MANAGER_API_TOKEN=use-a-long-random-api-token
```

The manager API is bound to `127.0.0.1:8090` by default. It is also reachable by containers on `caddy_proxy` at `http://agent-manager:8000`.

Validate and start:

```bash
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose ps
```

Check readiness from the VM:

```bash
curl -sS http://127.0.0.1:8090/readyz
```

Expected response:

```json
{"status":"ok","database":"ok","nats":"ok"}
```

Check logs:

```bash
sudo docker compose logs --tail=100 agent-manager
sudo docker compose logs --tail=100 nats
sudo docker compose logs --tail=100 postgres
```

## Verification command files

The multiline verification commands are also available as runnable text files:

```text
verify-health.txt       liveness and readiness checks
verify-auth.txt         prints the status of an unauthenticated GET /v1/runs; it does not assert 401
submit-test-run.txt     creates a test run and saves its ID
get-test-run.txt        retrieves the saved test run
verify-postgres.txt     checks the persisted run row
verify-nats.txt         checks the AGENT_RUNS JetStream stream
consume-test-event.txt  reads one agent.runs.created event
```

Run them from this directory with Bash:

```bash
bash verify-health.txt
bash verify-auth.txt
bash submit-test-run.txt
bash get-test-run.txt
bash verify-postgres.txt
bash verify-nats.txt
bash consume-test-event.txt
```

`submit-test-run.txt` stores an accepted run ID in `.last-run-id`, which is ignored by Git. Before creating a row, Agent Manager probes `agent.workers.health.<role>`. Without a matching execution-enabled worker it returns HTTP `503`, `NO_WORKER_AVAILABLE`, and `Retry-After: 15`; no run is created or queued, and a sanitized `run.rejected` activity is recorded. An accepted run remains `queued` until a worker starts it. Completion stores the worker result under `metadata.result`.

## Submit a run

The bootstrap API token protects the machine `/v1/runs` API and administrator
operations such as node-credential and identity-mapping management. Normal
human dashboard APIs require a Forgejo OAuth session and CSRF header. The
separate Open WebUI run-only token is accepted only on run submission and
individual-run retrieval; it cannot authorize dashboard, task, or user APIs.

```bash
export AGENT_MANAGER_API_TOKEN='the-value-from-.env'

curl -sS \
  -H "Authorization: Bearer ${AGENT_MANAGER_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:8090/v1/runs \
  -d '{
    "goal": "Fix the failing authentication tests",
    "forgejo_repository": "owner/project",
    "base_ref": "main",
    "model": "local-model",
    "project_name": "auth-repair",
    "script_name": "verify_auth.py"
  }'
```

`project_name` and `script_name` are optional for direct API clients. The
manager retains its UUID run ID for queueing and status tracking; the worker
uses a filesystem-safe project slug as its working directory instead of the
UUID. Script names are filenames, not paths; `.py` is appended if omitted.

The request is stored in PostgreSQL and published to the JetStream subject:

```text
agent.runs.created
```

The administrator cancellation endpoint is `POST /v1/runs/{run_id}/cancel`.
It marks the run `cancel_requested` and publishes `agent.runs.cancelled`.
Workers consume that event, stop active work, and report a `cancelled`
completion; a late success cannot override the cancellation request. OMP
missions use their run-scoped cancellation capability, described below.

`worker_role` and `operation` are not request fields. Put them in `metadata`. A top-level object is accepted and ignored:

```json
{
  "goal": "Create a dependency-free Python script that writes a report.json file.",
  "forgejo_repository": "owner/project",
  "project_name": "auth-repair",
  "script_name": "verify_auth.py",
  "metadata": {
    "worker_role": "coder",
    "operation": "create-python-script"
  }
}
```

Do not set `metadata.worker_id` on the shared consumer. A mismatch raises in the worker before ack and exits that process, leaving the delivery unacked.

The stream is `AGENT_RUNS` with subjects `agent.runs.>`. Coder workers share a durable consumer such as `agent-workers-coder`. Publish dedupe uses `Nats-Msg-Id: {subject}:{run_id}`.

## API

No bearer token:

| Method | Path | Behavior |
|---|---|---|
| GET | `/healthz` | Always `{"status":"ok","database":"not_checked","nats":"not_checked"}`. Compose healthcheck uses this path. |
| GET | `/readyz` | Checks PostgreSQL and NATS. Returns 503 when either is down. |
| GET | `/metrics` | Prometheus counters `agent_runs_created_total`, `agent_runs_started_total`, `agent_runs_completed_total`, and `agent_runs_failed_total`. |
| GET | `/docs` | Default FastAPI docs, also `/redoc` and `/openapi.json`. |

Bearer token required. A missing or wrong token is 401.

| Method | Path | Behavior |
|---|---|---|
| POST | `/v1/runs` | 202. Stores `queued`, then publishes `agent.runs.created`. Publish failure sets `failed` and returns 503 `run could not be queued`. |
| GET | `/v1/runs` | Newest first. Query `status` is an exact match. `limit` defaults to 50 and must be 1–200. |
| GET | `/v1/runs/{run_id}` | 404 detail is `run not found`. |
| POST | `/v1/runs/{run_id}/cancel` | Administrator API token. Requests cancellation through the worker event stream. |
| POST | `/v1/omp/missions/{mission_id}/cancel` | Run-scoped cancellation capability returned to the Pipe; queued missions cancel immediately, running missions become `cancel_requested`. |
| GET | `/v1/omp/missions/{mission_id}/control` | Worker-only read using its upload capability; reports whether cancellation was requested. |
| POST | `/v1/omp/missions/{mission_id}/resume` | Run-scoped capability. Resumes a `needs_input` mission on its original worker, preserving the checkpoint and resetting `reviewer_count` to zero. |

The mission-specific routes use their per-run Bearer capabilities rather than
the global administrator API token.

`project_name` and `script_name` are stored inside `metadata`, not as columns. The same keys in the JSON body replace copies inside client `metadata`.

## Mission artifacts

Each queued mission receives a run-scoped upload capability in its
`agent.runs.created` event metadata. The worker uploads a ZIP containing every
output file, including a single-file result, as the raw request body to
`PUT /v1/omp/missions/{run_id}/artifact`, with `X-Artifact-Filename` and
`Content-Type` headers. Uploads are limited to 50 MiB;
the manager rejects unsafe filenames, allows same-content retries, and rejects
a different second artifact. Artifact bytes are stored in the
`mission_artifacts` PostgreSQL table, backed by the existing
`manager_postgres_data` volume. Mission status exposes the artifact descriptor
and a signed download URL that expires after seven days. Daily cleanup removes
artifact rows after the default 30-day retention period; set
`ARTIFACT_RETENTION_DAYS` to change the retention period.

## Modifying a completed artifact

`POST /v1/omp/turns` accepts an optional `parent_mission_id` for a requested
modification. The Manager validates that the parent is completed, belongs to
the run-submission actor, and still has a retained artifact. For Open WebUI,
it also checks the requester ID supplied by the Pipe against the ID recorded
on the parent mission; older missions without that ID cannot be used as sources.
The Manager publishes each child with a fresh signed source-download URL; that
URL is carried only in the worker event. The worker verifies TLS, checksum,
size, and ZIP paths before placing the source files under `/work`. Each
modification receives a new mission ID and artifact; it does not overwrite
the parent.

In Open WebUI, continue in the same conversation and begin the request with a
modification such as “Add a dark mode to the app.” The Pipe uses the most recent
completed artifact link in that conversation. Start a separate conversation
when requesting an unrelated mission.


## Mission cancellation

Open WebUI's explicit Stop action posts to
`/v1/omp/missions/{mission_id}/cancel` with a run-scoped capability. Queued
missions become `cancelled`; running missions become `cancel_requested` until
the worker confirms the stop. The worker checks cancellation state before
launching and while its named OMP container runs, then reports `cancelled`.
Closing the browser tab alone does not cancel a background Pipe task. If a
worker is offline, a cancellation remains pending; the worker checks state
before it can start that queued mission after reconnecting.


## Reviewer-limit clarification

After ten coder-reviewer attempts without approval, the run enters the
nonterminal `needs_input` state and exposes a specific clarification question.
The worker saves the current `/work` files in its per-run checkpoint, then
becomes idle. Open WebUI prompts for clarification and posts the answer to
`/v1/omp/missions/{mission_id}/resume`; the Manager targets the same worker and
run. The resumed review cycle starts with `reviewer_count: 0`. Explicit cancel
remains available while waiting for input.
Compose and `.env.example` set `MODEL_BASE_URL` to `http://llama:8080/v1`. The Python fallback `http://litellm:4000/v1` is unused while Compose sets the variable. The API does not call Forgejo, llama.cpp, LiteLLM, or Ollama. Ollama is on `open-webui_internal` at `http://ollama:11434`.

## Caddy routes and metrics

The tracked Caddy configuration routes Agent Manager pages and APIs to
`agent-manager:8000` without stripping their prefixes. Human dashboard sessions
use Forgejo OAuth; machine run and administrator APIs use bearer tokens. Keep
tokens secret and prefer the internal `http://agent-manager:8000` endpoint for
containers on `caddy_proxy`.

The distinct API Guard application is served by `api-guard:8000` at
`/api-guard/`; it has its own Forgejo OAuth application and allowlist. Caddy
limits that path to Tailscale source addresses. It reads LiteLLM key/spend data
server-side; it is not an Agent Manager route or part of its dashboard.

These manager routes coexist with `/api-guard/`, `/llama/`, and `/litellm/`,
`/grafana/`, `/forgejo/`, and the Proxmox fallback. Open WebUI remains
available at `https://infra-lab-services.tail494f6d.ts.net:8443` through Caddy.
The manager defaults to LiteLLM's internal URL rather than taking this external
proxy path; an existing `.env` override must be changed deliberately to use the
new default.

Prometheus scrapes the manager at `http://agent-manager:8000/metrics/` and the
exporter at `http://nats-exporter:7777/metrics`. Neither metrics endpoint is
added as a public Caddy route. The exporter is part of this Compose project,
while its scrape job lives in `../grafana_services/prometheus/prometheus.yml`.

## Troubleshooting

### NATS port and handshake

The base Compose file exposes NATS only to Docker networks. Remote workers
require the test override, which binds port `4222` to the service host's
Tailscale address:

```bash
cd "$REPO_ROOT/Docker-Documents/agent-manager-service"
sudo docker compose \
  -f compose.yml \
  -f compose.worker-test.yml \
  ps nats
sudo ss -ltnp | grep ':4222'
printf '' | nc -v 100.94.49.45 4222
```

A working NATS endpoint sends an `INFO` line. `Connection refused` means the
port is not published or NATS is not listening. An empty response while
expecting `INFO` is a handshake/endpoint problem, not an empty queue.

### Inspect NATS clients and consumers

The monitoring port is internal to the NATS container:

```bash
sudo docker compose \
  -f compose.yml \
  -f compose.worker-test.yml \
  exec -T nats wget -q -O - \
  http://127.0.0.1:8222/connz

sudo docker compose \
  -f compose.yml \
  -f compose.worker-test.yml \
  exec -T nats wget -q -O - \
  'http://127.0.0.1:8222/jsz?streams=true&consumers=true'
```

The worker pool should use one shared durable consumer per role, such as
`agent-workers-coder`. A worker's client name includes its `WORKER_ID`.

### Worker environment

The worker process must receive its variables in the same shell or service
unit that starts it:

Ansible configures each worker's NATS identity and Keybroker HTTPS endpoint;
workers no longer receive a static LiteLLM virtual key. Keybroker returns the
approved model alias and LiteLLM URL with each run session. For example:

```text
NATS_URL=nats://100.94.49.45:4222
WORKER_ID=worker-03
WORKER_ROLE=coder
WORKER_CONSUMER=agent-workers-coder
KEYBROKER_URL=https://infra-lab-services.tail494f6d.ts.net/keybroker
MODEL_TLS_INSECURE=0
ALLOW_GENERATED_CODE=0
```

Inventory worker IDs are `worker-01` through `worker-06`, not hostnames.
Sammy is `worker-05` and Michelangelo is `worker-06`; they must be deployed and
show a canonical heartbeat before they are treated as ready. Remote workers use
the Tailnet-only Caddy endpoint
`https://infra-lab-services.tail494f6d.ts.net/litellm/v1`; do not use Docker-only
`http://litellm:4000/v1` or the VM loopback address from a laptop. Each mission
uses a model-scoped, budget-limited key returned by Keybroker for that run.
Workers must trust Caddy's root CA and keep `MODEL_TLS_INSECURE=0`.

Ansible supervises the process with `dark-factory-agent-worker.service`. At run
start, the worker obtains a model-scoped, budget-limited LiteLLM key from
Keybroker, uses it only for that run, and revokes it in cleanup. Keybroker uses
the worker's Tailnet identity; no provider or LiteLLM master key is sent to the
worker or persisted in its environment. A TTL reaper handles crashed workers.
The worker redacts recognized credential patterns, stops the run on exposure,
and returns a sanitized security notice. Keybroker persists exposure events
before forwarding them to API Guard; API Guard's optional notification outbox
retries downstream delivery.

Approved OMP mission files are uploaded to Agent Manager's PostgreSQL-backed
artifact storage and returned as signed download links, not embedded in the
mission status payload. The temporary OMP workspace is removed after upload.
Mission jobs do not modify Forgejo, and mission artifacts are not executed. The
separate `create-python-script` operation remains disabled by default because
it runs generated Python unsandboxed.


### Duplicate completion events

A single delivery should produce one terminal completion event. If a run has
both `failed` and `completed` results, compare `run_id`, `worker_id`, and
`hostname`. Stop stale worker processes, ensure all workers share the same
role consumer, and keep the model/script runtime below the JetStream
`ack_wait` window. A redelivery can occur when the worker takes longer than
`ack_wait` to acknowledge the event.

## Backups

Back up both persistent volumes for a complete manager restore:

```text
agent-manager_postgres_data
agent-manager_nats_data
```

PostgreSQL is the authoritative run-state store. NATS JetStream is durable work delivery; retaining it allows queued events to survive a NATS restart.

## Dashboard references

- [Dashboard operations](../../docs/dark-factory-dashboard.md)
- [MCP integration contract](../../docs/mcp-dashboard.md)
- [Node activity collector](../../node-activity-collector/README.md)
- Interactive API schema: `/docs` on Agent Manager

Normal users sign in with Forgejo OAuth. The bearer token remains available
for approved machine clients and bootstrap recovery; do not expose it to
browser JavaScript or store it in Forgejo webhook configuration.

## Infrastructure reconciliation

The live capture `70838b3` has been reconciled with the dashboard source; this
does not deploy it to the VM. Follow
[the reviewed deployment procedure](../../docs/infra-reconciliation.md).
Availability probes are Core NATS request/reply and are not task reservations.
The `AGENT_WORKERS` stream stores only `agent.workers.heartbeat`; do not widen
it to `agent.workers.>` or storage acknowledgements can race health replies.
