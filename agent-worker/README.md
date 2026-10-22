# Agent worker

`worker.py` is the long-running Agent Manager queue consumer. It handles the
legacy `create-python-script` operation and the `mission` operation.

For `mission`, the worker launches the pinned OMP job image. The image runs the
OMP planner, which delegates to coder and reviewer task agents, then returns one
JSON result through `agent.runs.completed`. Ansible builds
`local/omp-worker:18.12.5` from `agent-worker/omp-image/`.

The legacy `create-python-script` operation remains available and is enabled by
the current Ansible worker defaults. It executes generated Python as the worker
OS user and is **not sandboxed**. Set `ALLOW_GENERATED_CODE=0` for a mission-only
manual worker configuration. Mission jobs run in the containerized OMP image,
which has no Docker socket and receives only its temporary workspace, mission
file, LiteLLM key, and Caddy root certificate.

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

An available worker pulls the next event. For normal OMP jobs, the manager sends
`operation: "mission"` with the planner/coder/reviewer counts in `metadata.crew`:

```json
{
  "worker_role": "coder",
  "operation": "mission",
  "crew": {
    "size": {"size": "small", "planners": 1, "coders": 1, "reviewers": 1}
  }
}
```

That object belongs inside the run's `metadata`, alongside a required
top-level `forgejo_repository`. The repository and base ref are context only;
the worker image does not clone or push them. Do not set `metadata.worker_id` on
the shared consumer.

## Mission OMP image

The NATS queue listener is `worker.py`; it launches the pinned OMP job image.
Inside the image, OMP runs the planner and delegates to the requested coder
and reviewer agents. Both agents block until their results are complete. The
planner performs an initial review and up to nine correction rounds (ten review
attempts total), but the worker's 600-second OMP timeout may stop the run sooner.
It returns success only after explicit reviewer approval.

The final JSON includes `reviewer_count`: the number of individual reviewer
verdicts marked `changes_requested` (zero means the first review approved).
Wrapper-generated failures use `null` when no valid final OMP result exists.
The planner reports this count; the wrapper validates its type but does not
independently reconstruct reviewer history.
The Agent Manager mission-status JSON exposes the same field.
The Agent Manager mission endpoint returns a signed artifact download link
rather than embedding product source. One file downloads directly; multiple
files are delivered as a ZIP. The worker removes its temporary workspace after
upload. Artifacts are retained for 30 days by default; signed links expire
after seven days.

The image's `worker-litellm/luna` model metadata declares a 16,384-token context
and a 4,096-token output limit to match the repository's default llama.cpp
`LLAMA_CONTEXT_SIZE`. If that backend setting changes, update these limits too;
OMP metadata cannot enlarge the server's context window.

Ansible copies the image sources to `~/agent-worker/omp-image/` and builds the
image on each worker. Rebuild manually after changing that directory:

```bash
docker build --pull --tag local/omp-worker:18.12.5 \
  "$HOME/agent-worker/omp-image"
```

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

Copy the worker and image build context through a trusted SSH channel:

```bash
WORKER='<worker-id>@<tailnet-user-IP>'
ssh "$WORKER" 'mkdir -p ~/agent-worker'
scp "$REPO_ROOT/agent-worker/worker.py" \
    "$REPO_ROOT/agent-worker/requirements.txt" "$WORKER:~/agent-worker/"
scp -r "$REPO_ROOT/agent-worker/omp-image" "$WORKER:~/agent-worker/"
ssh "$WORKER" \
    'docker build --pull --tag local/omp-worker:18.12.5 ~/agent-worker/omp-image'
```

The worker user needs Docker access, and the build needs internet access to
download the pinned OMP binary. Create the Python environment on the worker:

```bash
ssh "$WORKER" \
    'cd ~/agent-worker && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt'
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
export ALLOW_GENERATED_CODE=0
export MODEL_NAME='luna'
export MODEL_BASE_URL='https://infra-lab-services.tail494f6d.ts.net/litellm/v1'
export MODEL_CA_FILE="$HOME/.config/litellm/caddy-root.crt"
export OMP_WORKER_IMAGE='local/omp-worker:18.12.5'
export MODEL_API_KEY="$(tr -d '\r\n' < "$HOME/.config/litellm/worker-api-key")"
```

Do not use `LITELLM_MASTER_KEY` or the LiteLLM backend key as
`MODEL_API_KEY`. The public model name is `luna`; `openai/luna` is a LiteLLM
backend/provider configuration value, not a replacement worker credential.
`worker.py` reads `MODEL_API_KEY` from the environment and passes it to the
mission container. The OMP entrypoint exposes it there as `LITELLM_API_KEY`.
The host worker does not read the key file itself; the Ansible start script
loads it into the environment.

The Caddy route is available at:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

Set `worker_model_base_url` to the Caddy `/litellm/v1` endpoint. Ansible
installs the public Caddy root certificate at
`~/.config/litellm/caddy-root.crt`; the worker mounts it into the OMP image as
`NODE_EXTRA_CA_CERTS`. Keep `MODEL_TLS_INSECURE=0`. Never copy `root.key`.

The image emits sanitized `OMP_PROGRESS` lines for planner, coder, and reviewer
activity. `worker.py` passes these to its logs; the Manager mission receipt
still reports the final result rather than a live progress stream.

Docker bridge containers may not inherit Tailscale MagicDNS. The worker maps
`infra-lab-services.tail494f6d.ts.net` to the VM's Tailscale IP `100.94.49.45`
with `--add-host`, preserving the URL hostname for TLS/SNI without using host
networking. Update the mapping in `worker.py` and the smoke command if that IP
changes.

## Start the worker

The worker is long-running and should remain in the foreground while testing:

```bash
cd ~/agent-worker
.venv/bin/python -u worker.py 2>&1 | tee -a ~/agent-worker/worker.log
```

An idle worker should not print an error for an empty queue. The pull timeout is
caught and the worker waits for the next event. A worker connection error is
different and should be investigated rather than hidden.

Legacy `create-python-script` limits (not used by mission jobs):

```bash
export MODEL_TIMEOUT=120
export SCRIPT_TIMEOUT=300
export MAX_SCRIPT_CHARS=30000
export MAX_OUTPUT_CHARS=12000
```

## Legacy script workspace behavior

The worker code default is disabled, but the current Ansible group setting is
`allow_generated_code: true`. These workspace rules apply when it is enabled.

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

## Legacy host-side execution contract

This applies only to legacy `create-python-script`, which current Ansible
configuration enables. Generated Python runs as the worker OS user without a
sandbox. The model prompt requires scripts to:

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

## Mission event flow

Open WebUI submits missions to the Agent Manager, which publishes
`agent.runs.created`. The worker reports `agent.runs.status` and
`agent.runs.completed`; the Manager updates the mission receipt polled by the
Pipe. Do not publish a normal pooled mission with `metadata.worker_id`.

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
