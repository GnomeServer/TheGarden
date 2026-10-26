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
coders produce goal-driven files; independent reviewers can request up to three
revision attempts. Accepted artifact contents and review evidence return in the
worker completion event and are stored in `metadata.result`. Artifacts are
saved under the worker's `AGENT_WORKSPACE/missions/` directory. This flow does
not clone, modify, or push the configured Forgejo repository, and it does not
execute generated files. The manager consumes `agent.runs.status` and
`agent.runs.completed`; status moves `queued` to `running`, terminal events
close the run, and late events cannot reopen it.

OpenWebUI polls mission status and returns generated artifact files after
completion. A later “what's the status?” message queries that same mission
instead of creating another run. The dashboard shows the current run phase and
worker, refreshing every 30 seconds; per-agent model calls are summarized after
the crew finishes rather than streamed live.

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

The cancellation endpoint is `POST /v1/runs/{run_id}/cancel`. It sets `cancel_requested` and publishes:

```text
agent.runs.cancelled
```

Already `completed`, `failed`, or `cancelled` is returned unchanged and is not published. Nothing consumes `agent.runs.cancelled`, so the status never becomes `cancelled`. The Open WebUI Pipe treats only `completed`, `failed`, and `cancelled` as finished, so `cancel_requested` keeps it polling.

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
| POST | `/v1/runs/{run_id}/cancel` | See the cancellation rules above. |

`project_name` and `script_name` are stored inside `metadata`, not as columns. The same keys in the JSON body replace copies inside client `metadata`.

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

Ansible supervises the process with `dark-factory-agent-worker.service`. At run
start, the worker obtains a model-scoped, budget-limited LiteLLM key from
Keybroker, uses it only for that run, and revokes it in cleanup. Keybroker uses
the worker's Tailnet identity; no provider or LiteLLM master key is sent to the
worker or persisted in its environment. A TTL reaper handles crashed workers.
The worker redacts recognized credential patterns, stops the run on exposure,
and returns a sanitized security notice. Keybroker persists exposure events
before forwarding them to API Guard; API Guard's optional notification outbox
retries downstream delivery.

The worker uses the installed Caddy root CA to validate Keybroker and LiteLLM
TLS. Mission artifacts are saved on the worker and returned to OpenWebUI;
mission-generated artifacts are not executed. The separate
`create-python-script` operation remains disabled by default because it runs
generated Python unsandboxed.


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
