# Agent Manager

This Compose project deploys the first control-plane slice for agentic coding runs:

- `agent-manager`: FastAPI API, run-state persistence, and NATS JetStream publishing
- `postgres`: private PostgreSQL database for manager state
- `nats`: private NATS server with JetStream enabled and persistent storage

The manager does not execute repository code. Workers consume the durable NATS events and publish completion events. The manager now consumes `agent.runs.completed` through a durable JetStream consumer and updates the run status and result metadata. The current one-shot dynamic smoke-test worker is documented in [`../../agent-worker/README.md`](../../agent-worker/README.md).

## Networks

The manager joins the existing networks used by the lab:

- `caddy_proxy` for Forgejo HTTP/API access and optional Caddy routing
- `open-webui_internal` for Ollama access
- `agent_manager_internal` for PostgreSQL and NATS

The manager does **not** join `forgejo_internal`; Forgejo's database network remains isolated.

Caddy and the Open WebUI Compose project must already be running so that the two external networks exist:

```bash
cd /home/infra-lab-user/caddy-service
sudo docker compose up -d caddy

cd /home/infra-lab-user/open-webui-service
sudo docker compose up -d
```

## Configure and start

```bash
cd /home/infra-lab-user/agent-manager-service
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

`submit-test-run.txt` stores the most recent run ID in `.last-run-id`, which is ignored by Git. A run submitted without a matching worker remains `queued`. When a worker publishes `agent.runs.completed`, the manager updates the database status and stores the worker result under `metadata.result`.

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

For the worker smoke test, include matching metadata in the request:

```json
{
  "worker_id": "inkii",
  "operation": "create-python-script"
}
```

The worker must be waiting before the request is submitted because its current
consumer is one-shot and starts at new events. The stream is named `AGENT_RUNS`
and is stored in the `nats_data` volume. The manager's durable completion consumer is named `agent-manager-completions` and replays retained completion events after a restart.

## Existing service endpoints

The defaults match the current Docker networks:

```text
Forgejo API:  http://forgejo:3000/api/v1
llama.cpp:    http://llama:8080/v1
Ollama:       http://ollama:11434
```

`MODEL_BASE_URL`, `MODEL_API_KEY`, `FORGEJO_API_URL`, and `FORGEJO_TOKEN` are included in the environment for the next planner/orchestrator implementation. The initial API does not call those services yet.

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

## Backups

Back up both persistent volumes for a complete manager restore:

```text
agent-manager_postgres_data
agent-manager_nats_data
```

PostgreSQL is the authoritative run-state store. NATS JetStream is durable work delivery; retaining it allows queued events to survive a NATS restart.
