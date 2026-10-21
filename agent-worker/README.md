# Agent worker

`worker.py` is a long-running Agent Manager worker. It consumes
`agent.runs.created` from NATS JetStream, publishes `agent.runs.status` (and the
same payload on `agent.runs.started`), then publishes one `agent.runs.completed`
event. It also publishes `agent.workers.heartbeat` and answers
`agent.workers.health.<role>`. The mission path calls the configured
OpenAI-compatible gateway for planning, coding, and review. Mission artifacts
are saved under
`AGENT_WORKSPACE/missions/<project-slug>/<mission-id>/` and returned to the
Agent Manager. The worker does not execute those generated files or modify the
configured Forgejo repository.

The worker supports two operations:

- `mission` uses one model-backed planner, the requested coder count, and
  independent model-backed reviewers with up to three revision attempts.
  A mission requires a reachable `MODEL_BASE_URL` and a worker-scoped
  `MODEL_API_KEY`. No hardcoded candidate artifacts or hello-only fallback.
- `create-python-script` generates or accepts one Python script, then runs it
  directly as the worker operating-system user. This operation is not sandboxed
  and remains disabled unless explicitly enabled.

The Manager stamps each queued event with an expiry. The worker rejects an
expired delivery and acknowledges it without starting model calls.

> **Worker safety:** keep `ALLOW_GENERATED_CODE=0`. Mission output is written as
> text artifacts and is not executed. The separate `create-python-script`
> operation is unsandboxed and should only be enabled on a disposable worker.

## Repository layout

Run repository commands from the checkout root:

```bash
export REPO_ROOT=/path/to/TheGarden
```

The worker source and dependency file are:

```text
$REPO_ROOT/agent-worker/worker.py
$REPO_ROOT/agent-worker/requirements.txt
```

The Agent Manager Compose project is stored in the repository at:

```text
$REPO_ROOT/Docker-Documents/agent-manager-service
```

The worker itself is copied to a remote worker directory such as:

```text
~/agent-worker
```

That remote runtime directory is not a Git checkout and should contain no
committed secrets.

## Worker-pool behavior

Workers use a shared durable pull consumer for load balancing. Every worker
that provides the same role uses the same consumer name:

```text
worker_id       inventory id, not the hostname: worker-01 raphael, worker-02 inkii, worker-03 donatello, worker-04 naruto
worker_role     capability label, normally coder
worker_consumer shared JetStream consumer, normally agent-workers-coder
```

An available worker pulls the next event. The Open WebUI Pipe should not choose
a worker for normal jobs. It should submit this metadata, not a top-level
`worker_role` field:

```json
{
  "worker_role": "coder",
  "operation": "create-python-script"
}
```

That object belongs inside the run's `metadata`, together with a required
`forgejo_repository`. Do not set `metadata.worker_id` on the shared consumer.
A mismatch raises before publish or ack, and `main` does not catch it, so the
worker process exits and JetStream redelivers the unacked message.

## NATS network

The Agent Manager and NATS server run on the Docker service host. The test
Compose override publishes NATS only on that host's Tailscale address:

```text
NATS server: 100.94.49.45:4222
```

This is a Tailscale-only path. Do not add a public router rule or bind the test
port to `0.0.0.0` while NATS has no authentication/TLS configuration.

From the repository checkout on the service host, start NATS with the worker
override:

```bash
cd "$REPO_ROOT/Docker-Documents/agent-manager-service"

sudo docker compose \
  -f compose.yml \
  -f compose.worker-test.yml \
  up -d --force-recreate nats

sudo ss -ltnp | grep ':4222'
```

The listener should include `100.94.49.45:4222`, not only
`127.0.0.1:4222`. Test the path from a worker before starting Python:

```bash
nc -vz 100.94.49.45 4222
```

A raw NATS handshake should begin with `INFO`. `Connection refused` means the
Compose port override is not active or NATS is not listening. An
`empty response from server when expecting INFO message` is a connection or
endpoint problem, not an empty JetStream queue.

## Install or update a worker

The Ansible role is the preferred installer; it copies `worker.py`,
`mission.py`, and requirements; installs the worker-scoped LiteLLM key and
Caddy root certificate; writes `worker.env` and `start-worker.sh`; and starts
the worker now and at reboot. For a manual install, copy the worker source:

```bash
WORKER='<worker-id>@<tailnet-user-IP>'
ssh "$WORKER" 'mkdir -p ~/agent-worker'
scp "$REPO_ROOT/agent-worker/worker.py" \
    "$REPO_ROOT/agent-worker/mission.py" \
    "$REPO_ROOT/agent-worker/requirements.txt" \
    "$WORKER:~/agent-worker/"
```

Create the Python environment on the worker:

```bash
ssh "$WORKER" 'cd ~/agent-worker &&
  python3 -m venv .venv &&
  .venv/bin/python -m pip install -r requirements.txt'
```

Do not copy `.env` files, private keys, LiteLLM master keys, or API tokens from
the repository.

## Worker environment

Each worker needs a unique `WORKER_ID`. Workers with the same capability use
the same `WORKER_ROLE` and `WORKER_CONSUMER` values:

```bash
export NATS_URL='nats://100.94.49.45:4222'
export WORKER_ID='<worker-id>'
export WORKER_ROLE='coder'
export WORKER_CONSUMER='agent-workers-coder'
export ALLOW_GENERATED_CODE=0
export MODEL_BASE_URL='https://infra-lab-services.tail494f6d.ts.net/litellm/v1'
export MODEL_NAME='luna'
export MODEL_API_KEY="$(tr -d '\r\n' < "$HOME/.config/litellm/worker-api-key")"
```

Mission requests use the worker-scoped LiteLLM virtual key. Do not use
`LITELLM_MASTER_KEY` or the LiteLLM backend key as `MODEL_API_KEY`. The model
name is `luna`; `openai/luna` is a backend configuration value.

For manual installs, trust the Caddy internal root certificate:

```bash
sudo install -m 0644 root.crt /usr/local/share/ca-certificates/thegarden-caddy-root.crt
sudo update-ca-certificates
```

Never copy Caddy's `root.key`. Keep `MODEL_TLS_INSECURE=0`.

Mission artifacts are saved in the worker workspace and returned through
Open WebUI. This implementation does not clone or push the configured
Forgejo repository.

## Start the worker

The worker is long-running and should remain in the foreground while testing:

```bash
cd ~/agent-worker
.venv/bin/python -u worker.py 2>&1 | tee -a ~/agent-worker/worker.log
```

An idle worker should not print an error for an empty queue. The pull timeout is
caught and the worker waits for the next event. A worker connection error is
different and should be investigated rather than hidden.

Optional limits:

```bash
export MODEL_TIMEOUT=120
export SCRIPT_TIMEOUT=300
export MAX_SCRIPT_CHARS=30000
export MAX_OUTPUT_CHARS=12000
```

The UUID remains the internal run ID for queueing and status tracking. When a
project name is supplied, its filesystem-safe slug replaces the run ID as the
worker's workspace directory. The script is saved directly in that directory:

```text
~/agent-workspace/<project-slug>/<script-name>.py
```

Without a project name, direct API callers retain the UUID workspace path:

```text
~/agent-workspace/<run-id>/<script-name>.py
```

If no script name is provided, the worker uses `generated_agent.py`. Existing
scripts are not overwritten; duplicates get `_2`, `_3`, and so on. Scripts
execute in that workspace directory, so their output artifacts are written there.

## Generated-script contract

Generated code is executed directly by the worker user. The model prompt
requires scripts to:

- use only the Python standard library already available on the worker;
- operate only in the current workspace directory;
- avoid network, shell, subprocess, and secret access;
- avoid `input()`, stdin, follow-up questions, and GUI modules such as
  `tkinter`;
- finish within the configured timeout;
- print a concise final result and exit.

`stdin` is connected to `DEVNULL`. A generated script that calls `input()` will
receive `EOFError` and be reported as failed. Interactive human-in-the-loop
execution is not implemented yet; it would require a `waiting_for_input` state,
a worker protocol for resuming a process, and Pipe support.

Do not allow generated code to run `pip install`, `apt`, or another package
manager. If a dependency is intentionally supported, install a pinned version
in the worker image or virtual environment through Ansible before running jobs.
`tkinter`, when required, is an operating-system package such as
`python3-tk` and also requires a display for most GUI operations; it is not a
suitable dependency for the current headless worker.

## Submit a pooled run


The Pipe or a test client must include `forgejo_repository`. Omit
`worker_id`. Put the role and operation in `metadata`:

```json
{
  "goal": "Create a dependency-free Python script that writes a report.json file.",
  "forgejo_repository": "owner/project",
  "model": "luna",
  "project_name": "quarterly-audit",
  "script_name": "audit_report.py",
  "metadata": {
    "worker_role": "coder",
    "operation": "create-python-script"
  }
}
```

For a deterministic test, provide a script string:

```json
{
  "goal": "Run the supplied test script",
  "forgejo_repository": "owner/project",
  "metadata": {
    "worker_role": "coder",
    "operation": "create-python-script",
    "script": "print('provided test script')"
  }
}
```

The Manager stores the run and publishes `agent.runs.created`. It consumes
`agent.runs.status` (`status: running`) and `agent.runs.completed`
(`metadata.result`).

## Troubleshooting

### NATS connection refused

On the service host, use both Compose files and check the published port:

```bash
cd "$REPO_ROOT/Docker-Documents/agent-manager-service"
sudo docker compose -f compose.yml -f compose.worker-test.yml ps nats
sudo ss -ltnp | grep ':4222'
```

In the same shell that starts the worker, verify:

```bash
printf 'NATS_URL=%s\n' "$NATS_URL"
```

It must be `nats://100.94.49.45:4222`, not the default local URL
`nats://127.0.0.1:4222`.

### Empty queue timeout

A `nats.errors.TimeoutError` from `subscription.fetch()` after the hardcoded
120-second wait is normal when no job is available. The worker catches it and continues.
It should not be printed as a failure.

### Empty response while expecting NATS INFO

This happens during the NATS protocol handshake and is not an empty queue. Test
the endpoint with `nc` or a small socket script. Confirm that the worker is
using the same URL that returned an `INFO` line.

### Duplicate completed and failed results

A single delivery should produce one completion event. Check the `run_id`,
`worker_id`, and `hostname` in both events. Duplicate results usually mean an
old worker process is still subscribed, different workers are using different
consumer names, or the job exceeded the JetStream acknowledgement window.

Keep the maximum model and script time below `ack_wait` (currently 900 seconds)
or increase `ack_wait` when intentionally allowing longer jobs. Stop stale
worker processes before testing:

```bash
pgrep -af worker.py
```

### TLS certificate failure

Extract the existing Caddy root certificate from the running Caddy container,
copy only `root.crt` to the worker, and use the Caddy hostname in
`MODEL_BASE_URL`. Never copy `root.key` and do not generate a second CA for the
same Caddy deployment.

### LiteLLM or model timeout

Test the LiteLLM `/v1/models` and `/v1/chat/completions` endpoints with the
worker virtual key. If the local gateway request works but the external request
fails, investigate Caddy/TLS. If both requests work but the worker times out,
compare `MODEL_TIMEOUT` with LiteLLM's backend timeout and inspect the LiteLLM
logs.

### Generated module or GUI failure

Inspect the generated source:

```bash
grep -nE '^(import|from)|input\(|tkinter' \
  ~/agent-workspace/<run-id>/generated_agent.py
```

Do not install arbitrary packages at runtime. Change the request to require a
standard-library, headless, file-based result, or provision an approved pinned
dependency before running the job.
