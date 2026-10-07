# Agent worker

`worker.py` is a long-running Agent Manager worker. It consumes
`agent.runs.created` events from NATS JetStream, asks the configured
OpenAI-compatible gateway to generate a Python script, executes that script in
a per-run workspace, and publishes one `agent.runs.completed` event.

The worker currently supports the `create-python-script` operation. A script
may also be supplied as `metadata.script` for deterministic pipeline tests,
bypassing the model call.

> **Test-worker safety:** generated code runs as the worker operating-system
> user. This is not a sandbox. Use a disposable/test worker, do not provide
> production credentials, and keep `ALLOW_GENERATED_CODE=1` disabled except for
> controlled tests.

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
worker_id       unique identity, for example inkii or donatello
worker_role     capability label, normally coder
worker_consumer shared JetStream consumer, normally agent-workers-coder
```

An available worker pulls the next event. The Open WebUI Pipe should not choose
a worker for normal jobs. It should submit role metadata:

```json
{
  "worker_role": "coder",
  "operation": "create-python-script"
}
```

A specific `worker_id` may be included for an intentional targeted test, but it
should not be used for pooled work.

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

Copy the source files through a trusted SSH channel:

```bash
mkdir -p ~/agent-worker

scp "$REPO_ROOT/agent-worker/worker.py" \
    "$REPO_ROOT/agent-worker/requirements.txt" \
    <worker-id>@<tailnet-user-IP>:~/agent-worker/
```

Create the environment once:

```bash
cd ~/agent-worker
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
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
export AGENT_WORKSPACE="$HOME/agent-workspace"
export ALLOW_GENERATED_CODE=1
export WORKER_VERSION='0.2.0'
export WORKER_HEARTBEAT_INTERVAL=15
export WORKER_CAPABILITIES='python-script'
```

The model gateway uses the worker-scoped LiteLLM virtual key:

```bash
export MODEL_BASE_URL='https://infra-lab-services.tail494f6d.ts.net/litellm/v1'
export MODEL_NAME='luna'
export MODEL_API_KEY="$(cat ~/.config/agent-worker/luna-worker-api-key)"
```

Do not use `LITELLM_MASTER_KEY` or the LiteLLM backend key as
`MODEL_API_KEY`. The public model name is `luna`; `openai/luna` is a LiteLLM
backend/provider configuration value, not a replacement worker credential.

For the internal Caddy CA, prefer a trusted CA bundle:

```bash
export SSL_CERT_FILE="$HOME/.pi/agent/certs/caddy-local-root.crt"
export MODEL_TLS_INSECURE=0
```

`MODEL_TLS_INSECURE=1` is only a temporary diagnostic bypass and must not be
used for a persistent deployment. The URL must use the Caddy hostname, not the
Tailscale IP, because the certificate is issued for the hostname.

## Start the worker

The worker is long-running and should remain in the foreground while testing:

```bash
cd ~/agent-worker
.venv/bin/python -u worker.py 2>&1 | tee -a ~/agent-worker/worker.log
```

An idle worker should not print an error for an empty queue. The pull timeout is
caught and the worker waits for the next event. A worker connection error is
different and should be investigated rather than hidden.

Stop a foreground test worker with `Ctrl+C`. A normal stop prints
`worker stopped`, closes its JetStream subscription and NATS connection, and
exits without a traceback. A traceback on ordinary `Ctrl+C` is a worker
shutdown bug, not a task failure.

The worker publishes `agent.workers.heartbeat` immediately after connecting,
every `WORKER_HEARTBEAT_INTERVAL` seconds, and whenever it changes between
`idle` and `busy`. It also publishes `agent.runs.status` before execution so
the Manager can move a queued run to `running`. Keep the heartbeat interval
below the Manager's offline threshold (45 seconds by default). Every event has
a unique `event_id`; do not put credentials or model input in heartbeat fields.

Agent Manager preserves the live infrastructure admission check:
`agent.workers.health.<role>` is a Core NATS request/reply subject. An enabled
worker responds with its ID, role, and hostname; disabled workers and workers
not matching an explicit target stay silent. Start an execution-enabled test
worker before submitting the smoke run, or the API returns `503
NO_WORKER_AVAILABLE` without queueing it. A health reply reports reachability,
not a reserved worker slot; busy workers may still respond. Durable queue
consumers retain `DeliverPolicy.ALL` so accepted work can replay after restart.

The captured change does not implement a process lock, leases, fencing,
mid-run cancellation, or exactly-once execution. Targeted runs are still
intended for a single-worker test; a shared pull queue does not route a message
to a particular worker. Use role-only metadata for ordinary pooled work.


Optional limits:

```bash
export MODEL_TIMEOUT=120
export SCRIPT_TIMEOUT=300
export MAX_SCRIPT_CHARS=30000
export MAX_OUTPUT_CHARS=12000
```

The generated file is written to:

```text
~/agent-workspace/<run-id>/generated_agent.py
```

## Generated-script contract

Generated code is executed directly by the worker user. The model prompt
requires scripts to:

- use only the Python standard library already available on the worker;
- operate only in the current run directory;
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

The Pipe or a test client should omit `worker_id` for pooled work:

```json
{
  "goal": "Create a dependency-free Python script that writes a report.json file.",
  "model": "luna",
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
  "metadata": {
    "worker_role": "coder",
    "operation": "create-python-script",
    "script": "print('provided test script')"
  }
}
```

The Manager stores the run, publishes `agent.runs.created`, and consumes the
worker's `agent.runs.completed` event. The worker result is stored under
`metadata.result`.

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

A `nats.errors.TimeoutError` from `subscription.fetch()` after the configured
wait is normal when no job is available. The worker catches it and continues.
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
