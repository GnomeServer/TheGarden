# Agent Manager

This Compose project deploys the first control-plane slice for agentic coding runs:

- `agent-manager`: FastAPI API, run-state persistence, and NATS JetStream publishing
- `postgres`: private PostgreSQL database for manager state
- `nats`: private NATS server with JetStream enabled and persistent storage

The manager does not execute repository code and does not call Forgejo or a model. Workers consume `agent.runs.created`. The manager consumes `agent.runs.started` on durable consumer `agent-manager-started` and `agent.runs.completed` on durable consumer `agent-manager-completions`. A start event moves only `queued` to `running` and stores `metadata.worker`. A completion event writes `metadata.result` and sets status to the worker's `completed` or `failed`, even if the run was already terminal. Those consumers redeliver unacknowledged messages after a restart; already-acked messages are not replayed. The worker protocol is in [`../../agent-worker/README.md`](../../agent-worker/README.md).

## Networks

The manager joins the existing networks used by the lab:

- `caddy_proxy` for Forgejo HTTP/API access and optional Caddy routing
- `open-webui_internal` for Ollama access
- `agent_manager_internal` for PostgreSQL and NATS

The manager does **not** join `forgejo_internal`; Forgejo's database network remains isolated.

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

`submit-test-run.txt` stores the most recent run ID in `.last-run-id`, which is ignored by Git. A run with no worker remains `queued`. A start event sets `running`. A completion event sets `completed` or `failed` and stores the worker payload under `metadata.result`. A later completion overwrites an earlier terminal status.

## Submit a run

The API token is required for all `/v1` endpoints:

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

`project_name` and `script_name` are optional. The manager keeps its UUID run
ID for queueing and status tracking; the worker uses the project slug as its
workspace directory. Script names must be filenames, not paths; `.py` is
appended when the extension is omitted.

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

## Optional Caddy route

The manager is not publicly routed by default. If an authenticated external API endpoint is needed, add this route to the Caddy site that serves the lab hostname:

```caddyfile
@agent_manager {
    path /agent-manager /agent-manager/*
}

handle @agent_manager {
    uri strip_prefix /agent-manager
    reverse_proxy agent-manager:8000
}
```

Keep the bearer token secret and prefer calling `http://agent-manager:8000` directly from an Open WebUI Function while both containers are on `caddy_proxy`.

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
WORKER_ID=worker-01
WORKER_ROLE=coder
WORKER_CONSUMER=agent-workers-coder
MODEL_NAME=luna
MODEL_API_KEY=<worker-scoped-LiteLLM-key>
ALLOW_GENERATED_CODE=1
```

Inventory IDs are `worker-01` through `worker-04`, not hostnames. Do not set
`MODEL_BASE_URL` to `http://litellm:4000/v1` on a laptop; that name is Docker
DNS. Do not use `https://infra-lab-services.tail494f6d.ts.net/litellm/v1`
either: the checked-in Caddyfile has no `/litellm/` route. VM-local checks use
`http://127.0.0.1:4000/v1`. A remote dynamic prompt has no routed model URL in
this repository. `metadata.script` does not need one.

Do not use the LiteLLM master key as `MODEL_API_KEY`. Do not place any key in
Git. `MODEL_TLS_INSECURE=1` is not a substitute for a missing Caddy route.

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
