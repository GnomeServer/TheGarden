# Agent Manager

This Compose project deploys the first control-plane slice for agentic coding runs:

- `agent-manager`: FastAPI API, run-state persistence, and NATS JetStream publishing
- `postgres`: private PostgreSQL database for manager state
- `nats`: private NATS server with JetStream enabled and persistent storage
- `nats-exporter`: private Prometheus metrics for NATS clients, subscriptions, and JetStream

The manager does not execute repository code. Long-running workers consume durable NATS events and publish completion events. The manager consumes `agent.runs.completed` through a durable JetStream consumer and updates the run status and result metadata. The pooled worker protocol is documented in [`../../agent-worker/README.md`](../../agent-worker/README.md).

The human control surface is available at `/dashboard/` through Caddy. It
tracks runs, deadline-aware tasks, Forgejo users and activity, worker presence,
and audit history. Complete deployment, OAuth, webhook, task, worker, backup,
and troubleshooting procedures are in
[`../../docs/dark-factory-dashboard.md`](../../docs/dark-factory-dashboard.md).
For an **existing central VM**, use the guarded manager-only
[`central deployment procedure`](../../docs/central-dashboard-deployment.md), not
the new-install commands below. It preserves live credentials, the NATS override,
database volumes, Caddy routes, and the current manager image for recovery.
Open WebUI integration is [opt-in](../open_webui_services/README.md#agent-manager-coder-function-manual-opt-in);
generated Python is not sandboxed and must run only on disposable workers.


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
verify-auth.txt         confirms unauthenticated requests return 401
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

`submit-test-run.txt` stores an accepted run ID in `.last-run-id`, which is ignored by Git. Before creating a row, Agent Manager probes `agent.workers.health.<role>`. Without a matching execution-enabled worker it returns HTTP `503`, `NO_WORKER_AVAILABLE`, and `Retry-After: 15`; no run is created or queued. An accepted run remains `queued` until a worker starts it. Completion stores the worker result under `metadata.result`.

## Submit a run

The bootstrap API token is accepted by the manager's run and dashboard APIs.
Normal human sessions use Forgejo OAuth. The separate Open WebUI run-only token
is accepted only on run submission and individual-run retrieval; it cannot
authorize dashboard, tasks, user management, or run listing.

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
    "model": "local-model"
  }'
```

The request is stored in PostgreSQL and published to the JetStream subject:

```text
agent.runs.created
```

The cancellation endpoint publishes to:

```text
agent.runs.cancelled
```

For pooled worker work, omit `worker_id` and include the worker role:

```json
{
  "worker_role": "coder",
  "operation": "create-python-script"
}
```

The stream is named `AGENT_RUNS` and is stored in the `nats_data` volume. Coder
workers share a durable consumer such as `agent-workers-coder`; the first
available worker pulls each event. The manager's durable completion consumer is
named `agent-manager-completions` and replays retained completion events after
a restart.

## Existing service endpoints

The defaults match the current Docker networks:

```text
Forgejo API:  http://forgejo:3000/api/v1
LiteLLM:      http://litellm:4000/v1 (default MODEL_BASE_URL)
llama.cpp:    http://llama:8080/v1 (optional direct endpoint)
Ollama:       http://ollama:11434
```

`MODEL_BASE_URL`, `MODEL_API_KEY`, `FORGEJO_API_URL`, and `FORGEJO_TOKEN` are included in the environment for the next planner/orchestrator implementation. The initial API does not call those services yet.

## Caddy routes and metrics

The tracked Caddy configuration routes `/dashboard`, `/dashboard/*`, `/auth/*`,
`/v1/*`, `/docs`, `/docs/*`, and `/openapi.json` to `agent-manager:8000` without
stripping their prefixes. Human dashboard sessions use Forgejo OAuth; machine
API clients use their bearer token. Keep tokens secret and prefer the internal
`http://agent-manager:8000` endpoint for containers on `caddy_proxy`.

These manager routes coexist with the captured `/llama/` and `/litellm/` model
routes, `/grafana/`, `/forgejo/`, and the Proxmox fallback. Open WebUI remains
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

```text
NATS_URL=nats://100.94.49.45:4222
WORKER_ID=<unique-worker-id>
WORKER_ROLE=coder
WORKER_CONSUMER=agent-workers-coder
MODEL_BASE_URL=https://infra-lab-services.tail494f6d.ts.net/litellm/v1
MODEL_NAME=luna
MODEL_API_KEY=<worker-scoped-LiteLLM-key>
ALLOW_GENERATED_CODE=1
```

Do not use the LiteLLM master key as `MODEL_API_KEY`. Do not place any key in
Git. The worker should use the existing Caddy root CA rather than
`MODEL_TLS_INSECURE=1` for normal operation.

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
