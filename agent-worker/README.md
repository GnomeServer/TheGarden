# Agent worker

`worker.py` is the long-running Agent Manager queue consumer. It consumes
`agent.runs.created` from NATS JetStream, publishes run status on
`agent.runs.status` and `agent.runs.started`, then publishes one
`agent.runs.completed` event. It also sends `agent.workers.heartbeat` and
answers `agent.workers.health.<role>`. The Manager stamps queued runs with an
expiry; expired deliveries are rejected before model work starts.

For `mission`, the worker launches the pinned OMP job image. Its planner
coordinates the requested coder and reviewer agents and accepts a result only
after explicit reviewer approval. The isolated image receives a temporary
workspace, mission file, a short-lived model key obtained from Keybroker for the
run, and the trusted Caddy certificate; it has no Docker socket. The host worker
validates the artifact manifest, packages every result as a ZIP, including
single-file results, and uploads it over verified TLS to Agent Manager using a
run-scoped capability. Agent Manager stores the bytes in PostgreSQL and returns
a signed download link; the temporary OMP workspace is removed after upload.
The worker does not clone or push the configured Forgejo repository, and
mission artifacts are not executed.

The separate legacy `create-python-script` operation remains disabled by
default. If explicitly enabled, it executes generated Python unsandboxed as the
worker OS user. Keep `ALLOW_GENERATED_CODE=0`.

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

The worker normalizes those counts into an explicit `crew_plan` object in
`/mission.json`, using `metadata.crew.size` first and the direct role counts in
`metadata.mission` as a compatibility fallback. The OMP image requires this
explicit plan before starting the mission.

The host also passes `OMP_EXPECTED_MISSION_ID` directly to Docker, independently
of the mounted JSON. The image requires that identity and rejects a mission
file naming a different mission before starting OMP. Missing, unreadable, or
invalid mission files report failure under the host-supplied identity.

## Mission OMP image

The NATS queue listener is `worker.py`; it launches the pinned OMP job image.
Inside the image, OMP runs the planner and delegates to the requested coder
and reviewer agents. Both agents block until their results are complete.
The planner performs an initial review and up to nine correction rounds (ten
review attempts total). OMP allows up to 1,200 seconds (20 minutes) internally.
The worker allows 60 additional seconds for the container to shut down and
output to be collected before it marks the run failed. If attempt ten is still
rejected, the mission enters `needs_input` and asks the user one specific
clarification question instead of failing or cancelling. A clarification
resumes the same mission from its worker checkpoint with `reviewer_count` reset
to zero for the new review cycle.

`reviewer_count` counts individual reviewer verdicts marked `changes_requested`;
it is not the attempt number. Wrapper-generated failures use `null` when no
valid final OMP result exists. The Agent Manager mission-status JSON exposes
this count and the clarification question.

The planner, coder, and reviewer can research public sources with `web_search`
and read public HTTPS pages with `read`. Each temporary OMP home pins
`modelRoles.web` to `web/duckduckgo`, sets an empty
`retry.fallbackChains.web`, and bounds search transport to 30 seconds. Search
needs no additional API key and does not fall through to model-backed search.
Search throttling and unavailable sources remain real failures, not permission
to invent evidence. The crew requires dated source links and an access date for
current-information claims, with independent reviewer checks. Undated archive
pages, access dates, and dates on unrelated entries do not establish current
circulation. Specify a date window when freshness is important.

Research restrictions are agent policy, not an enforced egress boundary.
The existing container sandbox and service firewall are unchanged; arbitrary
shell execution and environment-held model credentials still require trusted
missions. The prompts forbid shell-based networking, private/authenticated
sources, sending secrets in searches, and following retrieved instructions.
Do not describe this change as network or credential isolation.

For open-ended examples, the crew prefers benign material. A provider
content-filter stop remains a failed mission: the crew must not retry the
blocked text, bypass filtering, or treat it as reviewer correction feedback.
Observed assistant/child errors now survive valid planner failure JSON in a
bounded, redacted `error` string and structured `provider_error` metadata.
The Manager already persists this result and exposes `error` in mission status.
Provider codes and request/response IDs are included only when observed; normal
prose mentioning a filter is not provider evidence. Only the latest observed
error is retained, not a complete transcript or the rejected generation.

For `needs_input`, the worker retains a bounded checkpoint under
`~/agent-workspace/.pending-missions/` until the mission resumes or is explicitly
cancelled. Final success still requires explicit reviewer approval and a
complete artifact manifest. On success, the worker uploads the ZIP and removes
its temporary workspace. Artifacts are retained for 30 days by default; signed
links expire after seven days.

For an artifact modification, the Manager creates a new child mission and
supplies its parent ZIP as a signed, TLS-verified worker download. The worker
checks its size and checksum, rejects unsafe ZIP paths, and extracts it into
the new `/work` before launching OMP. The original mission and ZIP stay
unchanged; the child mission uploads its own reviewed ZIP.

The Open WebUI Stop action requests cancellation for the active mission. The
Manager marks queued runs `cancelled` immediately and running runs
`cancel_requested`; the worker listens for cancellation events, polls the
run-scoped control endpoint, stops the named OMP container, and reports
`cancelled`. A disconnected worker checks the stored state before starting a
queued mission after reconnecting.

The image's `worker-litellm/luna` model metadata declares a 4,096-token context
and a 2,048-token output limit, matching the repository's default llama.cpp
`LLAMA_CONTEXT_SIZE`. This client-side metadata does not change the backend
context size.

Ansible copies the image sources to `~/agent-worker/omp-image/` and builds the
image on each worker. Rebuild manually after changing that directory:

```bash
docker build --pull --tag local/omp-worker:18.12.5 \
  "$HOME/agent-worker/omp-image"
```

For an existing running worker, deploy and restart the updated **host worker
first**, while the old image is still installed; only then build/promote the
new image. The old image ignores the additional identity environment variable,
but the new image requires it. Alternatively, drain and stop the worker before
the complete source/image rollout, including an Ansible deployment, and start
it only after both are installed. Do not run an old host worker with the new
image. Preserve the previous image under a rollback tag before replacing the
active tag; restore the old image before rolling back the host source.

The host queries the active Docker daemon's security options before each
mission. Rootful Docker runs the container with the worker's host UID/GID.
Rootless Docker instead uses container `0:0`, which maps to the unprivileged
daemon owner's host identity. Reusing the host's nonzero numeric UID inside a
rootless daemon maps to a subordinate host UID and cannot read a `0600`
mission file or write the private workspace. Keep the worker and rootless
daemon under the same host account. This does not grant host root access:
read-only root, dropped capabilities, no-new-privileges, resource limits, and
owner-only host file permissions remain enforced.

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

The Ansible role is the preferred installer. It copies `worker.py`, the
requirements, and the OMP image build context; configures the Keybroker endpoint
and Caddy root certificate; builds the pinned image; and manages
`dark-factory-agent-worker` with systemd. It does not install a static LiteLLM
virtual key on workers. Relevant source, environment, certificate, and image
changes restart the service. The one-time cutover stops legacy cron-started
workers. For a manual install, copy the worker and image build context through
a trusted SSH channel:

```bash
WORKER='<worker-id>@<tailnet-user-IP>'
ssh "$WORKER" 'mkdir -p ~/agent-worker'
scp "$REPO_ROOT/agent-worker/worker.py" \
    "$REPO_ROOT/agent-worker/requirements.txt" "$WORKER:~/agent-worker/"
scp -r "$REPO_ROOT/agent-worker/omp-image" "$WORKER:~/agent-worker/omp-image"
ssh "$WORKER" \
    'docker build --pull --tag local/omp-worker:18.12.5 ~/agent-worker/omp-image'
```

The worker user needs Docker access, and the image build needs internet access
to download the pinned OMP binary. Create the Python environment on the worker:

```bash
ssh "$WORKER" \
    'cd ~/agent-worker && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt'
```

Do not copy `.env` files, private keys, LiteLLM master keys, or API tokens from
the repository.

## Worker environment

Each worker needs a unique `WORKER_ID`. Workers with the same capability use
the same `WORKER_ROLE` and `WORKER_CONSUMER` values:
The runtime fallback is `worker-01` for single-worker local development only.
Every deployed worker must set its assigned canonical inventory ID explicitly;
do not rely on the fallback on a host that is not worker-01.

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
export KEYBROKER_URL='https://infra-lab-services.tail494f6d.ts.net/keybroker'
```

For model-backed mission runs, the worker obtains a short-lived session key from
Keybroker and passes it only to the OMP job for model requests, then revokes the
session when processing ends. It does not read or store a static `MODEL_API_KEY`
or LiteLLM master key. The Keybroker server controls the model alias, budget,
and session TTL; mission input cannot override these policies.

The Ansible-managed systemd unit denies all network destinations for the worker
service and its child processes except the control-plane VM at
`100.94.49.45/32`. Ansible pins `infra-lab-services.tail494f6d.ts.net` to that
IP in `/etc/hosts`, preserving HTTPS SNI and certificate validation without
allowing DNS resolver traffic. The rule is per-service, activates only after
deployment, and does not restrict unrelated host processes or monitor flows.

The worker scans prompts and model responses for an exact session-key echo and
recognized OpenAI, OpenRouter, Anthropic, and Google key formats. A detected
value is withheld from model calls and run outputs; the worker stops further
calls, submits a secret-free exposure event, revokes the session, and returns
a sanitized `security_notice`. These pattern checks are a safeguard for the
listed formats, not a guarantee against arbitrary secret exposure or other
forms of exfiltration. The API Guard dashboard is the administrator alert
surface.

The exposure report distinguishes an exact current-session key echo from a
provider-key pattern. Pattern-only matches are flagged for investigation and
do not revoke unrelated provider credentials; per-run cleanup still revokes
the worker's own session key.

The Tailnet-only Caddy endpoint is:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

Ansible installs the public Caddy root certificate both in the worker's trust
store and at `~/.config/litellm/caddy-root.crt`. The OMP image receives the
latter as `NODE_EXTRA_CA_CERTS`; artifact upload uses the same CA explicitly.
Keep `MODEL_TLS_INSECURE=0`. Never copy Caddy's `root.key`.

For a manual install, trust the Caddy root certificate:

```bash
sudo install -m 0644 root.crt /usr/local/share/ca-certificates/thegarden-caddy-root.crt
sudo update-ca-certificates
```

The image emits sanitized `OMP_PROGRESS` lines for planner, coder, and reviewer
activity. Docker bridge containers may not inherit Tailscale MagicDNS, so the
worker maps `infra-lab-services.tail494f6d.ts.net` to the VM's Tailscale IP
`100.94.49.45` with `--add-host`, preserving the URL hostname for TLS/SNI
without host networking. Update the mapping if that IP changes.

Ansible-managed workers run as the `dark-factory-agent-worker` systemd service.
Inspect the service and its logs with:

```bash
sudo systemctl status dark-factory-agent-worker
sudo journalctl -u dark-factory-agent-worker -f
```

For a one-off manual test only, stop the service first to avoid launching a
second queue consumer:

```bash
sudo systemctl stop dark-factory-agent-worker
cd ~/agent-worker
.venv/bin/python -u worker.py
```

Legacy `create-python-script` limits (not used by mission jobs):

```bash
export MODEL_TIMEOUT=120
export SCRIPT_TIMEOUT=300
export MAX_SCRIPT_CHARS=30000
export MAX_OUTPUT_CHARS=12000
```

## Legacy script workspace behavior

The worker code and current Ansible group setting disable this operation
(`allow_generated_code: false`). These workspace rules apply only if it is
explicitly enabled.

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

This applies only to legacy `create-python-script`. The current Ansible
configuration disables it; if explicitly enabled, generated Python runs as the
worker OS user without a sandbox. The model prompt requires scripts to:

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

### Mission startup failure or mismatched identity

A wrapper startup failure is not evidence that inference ran. Inspect the
failure's `error` alongside its summary: a missing or unreadable `/mission.json`
must retain the original file error and the expected queued mission ID.
Results explicitly naming another mission (including the old `unknown`
startup ID) remain rejected. Their bounded diagnostic is retained with
session keys, run capabilities, recognized provider credentials, and unsafe
control characters removed; their artifacts and status are not accepted.
Check the bind source, worker UID/GID permissions, and Docker daemon's
filesystem view on the affected host rather than weakening identity checks.

On 2026-11-03, the identity fix was initially deployed to `worker-03`
(`bgurrol4@100.94.145.104`). The host worker restarted after a fresh idle
heartbeat, no active mission container, and empty pending/ack-pending queues.
The rebuilt `local/omp-worker:18.12.5-identity-20261103` image was promoted to
`local/omp-worker:18.12.5`; the preceding image remains tagged
`local/omp-worker:18.12.5-before-identity-20261103`. Actual image checks proved
missing-file failure identity preservation, successful loading of a valid
read-only mission mount, and rejection of a mismatched mounted mission. These
loader checks did not exercise model inference.

Later that day, authorized SSH access to `worker-05`
(`dow_admin@100.79.110.117`) became available. A no-network container probe
confirmed rootless Docker was exposing the `0600` mission file as container
UID 0, while the host launched OMP as container UID 1000. The old loader
reported `PermissionError` with mission ID `unknown`, masking the cause.

Sammy now runs the rootless-aware host worker and the matching identity-aware
image, tagged `local/omp-worker:18.12.5-rootless-20261103` and promoted to
`local/omp-worker:18.12.5`. Its previous image is retained as
`local/omp-worker:18.12.5-before-rootless-20261103`; source backups end in
`.before-rootless-20261103`. Mission
`72edc317-9b94-4fcd-8fa5-63235c345efb` completed on `worker-05`; its downloaded
ZIP passed checksum and exact-content checks. Sammy's temporary NATS
quarantine and systemd unit were removed after verification. Other workers
were not updated by this rootless repair.

### Research and provider-diagnostic repair (2026-11-03)

Mission `0d73a61e-f9d8-4fd1-87ce-537490825a06` failed while the coder tried
to quote the Navy Seal copypasta. LiteLLM recorded truncated `write` arguments;
the worker reported a content-filter stop. The image now permits source-backed
research and preserves observed runtime error details.

The updated image is deployed on Donatello as
`local/omp-worker:18.12.5-research-20261103`, promoted to
`local/omp-worker:18.12.5`; rollback image:
`local/omp-worker:18.12.5-before-research-20261103`. Host worker code and
firewall rules were not changed. Other workers were not updated: Inkii,
Naruto, and Michelangelo rejected SSH authentication, Raphael timed out, and
Sammy became unreachable during staging.

Verification: 28 entrypoint tests passed locally; 56 additional worker tests
passed using Donatello's dependency-equipped Python, with three NATS integration
tests skipped. Public DuckDuckGo search succeeded in the pinned image. A
standalone final-image mission on the control-plane host used the real model,
created a benign sourced Markdown artifact, received one reviewer rejection,
then passed independent review. Its evidence dated May–July 2026, not proof of
ongoing circulation on November 3. This smoke did not exercise Manager upload
or Open WebUI delivery. All temporary model keys were revoked.

The earlier Donatello smoke timed out after starting its coder. Subsequent
worker-identity requests returned HTTP 403 from the control plane. Diagnose
that separate access problem before treating a new queued mission as an
end-to-end deployment check; do not relax access controls to make the smoke pass.

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

Keep the maximum OMP runtime at 1200 seconds and mission `ack_wait` at 1500
seconds. Worker startup upgrades existing role and resume consumers to this
window without lowering a longer configured value. Stop stale worker processes
before testing:

```bash
pgrep -af worker.py
```

### Worker exits while upgrading the acknowledgement window

The pinned `nats-py==2.9.0` updates an existing durable through
`js.add_consumer(stream, config=existing_config)`, not `update_consumer`.
Startup preserves the existing consumer configuration and delivery state while
extending `ack_wait`. An older worker that reports
`NATS client cannot update the mission consumer ack_wait` needs the corrected
worker source deployed before restarting:

```bash
sudo systemctl reset-failed dark-factory-agent-worker
sudo systemctl restart dark-factory-agent-worker
sudo journalctl -u dark-factory-agent-worker -n 50 --no-pager
```

Verify fresh manager heartbeats and queue consumption, not only a successful
systemd start. Run the consumer migration regression cases against a disposable
JetStream-enabled broker with the pinned worker requirements installed:

```bash
cd agent-worker
NATS_TEST_URL=nats://127.0.0.1:4222 \
  python -m unittest discover -s tests -p test_worker_execution.py
```

Without `NATS_TEST_URL`, the broker integration cases are skipped. They verify
that extending the window retains pending deliveries, preserves longer windows,
and leaves missing consumers for normal subscription creation.

### TLS certificate failure

Extract the existing Caddy root certificate from the running Caddy container
and copy only `root.crt` to the worker. The worker calls Keybroker through
`KEYBROKER_URL`; Keybroker supplies the model endpoint for each session. Never
copy `root.key` and do not generate a second CA for the same Caddy deployment.

### Keybroker, LiteLLM, or model timeout

Check Keybroker availability at the configured `KEYBROKER_URL`. The worker
fails closed if it cannot create a session and never falls back to a stored
LiteLLM key. If session creation succeeds but model calls time out, inspect
Keybroker, Caddy/TLS, and LiteLLM logs on their respective hosts.

### Generated module or GUI failure

Inspect the generated source:

```bash
grep -nE '^(import|from)|input\(|tkinter' \
  ~/agent-workspace/<run-id>/generated_agent.py
```

Do not install arbitrary packages at runtime. Change the request to require a
standard-library, headless, file-based result, or provision an approved pinned
dependency before running the job.
