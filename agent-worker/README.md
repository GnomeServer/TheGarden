# Agent worker smoke test

`worker.py` is a one-shot Agent Manager worker for the first remote-worker
smoke test. It waits for one `agent.runs.created` event, uses the run's `goal`
to request a Python script from the configured OpenAI-compatible model gateway,
executes the generated script, publishes `agent.runs.completed`, prints the
result, and exits.

The worker source is:

```text
/home/infra-lab-user/TheGarden/agent-worker/worker.py
```

The worker currently supports the `create-python-script` operation only. A
script can also be supplied directly as `metadata.script` for deterministic
pipeline tests, bypassing the model call.

> **Test-worker safety:** generated code runs as the worker operating-system
> user, directly on the worker host. This is not yet the sandbox described in
> the architecture documents. Use a disposable/test worker, keep the model
> prompt constrained, and do not provide production credentials or mount
> sensitive directories. `ALLOW_GENERATED_CODE=1` is an explicit opt-in.

## Network arrangement

The Agent Manager and NATS server run on `infra-lab-services`. The test
Compose override exposes NATS only on that VM's Tailscale address:

```text
NATS server: 100.94.49.45:4222
worker:      inkii (100.98.125.127)
```

This is a Tailscale path; no Internet router port-forward or inbound NAT rule
is needed. Do not bind the test port to `0.0.0.0` while NATS has no
authentication/TLS configured.

On the service VM, from `/home/infra-lab-user/agent-manager-service`, apply the
test-only override and verify the listener:

```bash
sudo docker compose \
  -f compose.yml -f compose.worker-test.yml \
  up -d --force-recreate nats

sudo ss -ltnp | grep ':4222'
```

The listener should show `100.94.49.45:4222`, not only
`127.0.0.1:4222`. From `inkii`, verify the path before starting Python:

```bash
nc -vz 100.94.49.45 4222
```

If the VM's Tailscale address changes, set `NATS_BIND_ADDRESS` to the new
address when running Compose. The override defaults to the current VM address.

## Install on `inkii`

Copy the worker and its dependency file to the worker, then use a virtual
environment:

```bash
mkdir -p ~/agent-worker
# Run this scp command on infra-lab-services, or copy the two files by another
# trusted method:
scp /home/infra-lab-user/TheGarden/agent-worker/{worker.py,requirements.txt} \
  inkii@100.98.125.127:~/agent-worker/

cd ~/agent-worker
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Configure the model gateway

The worker calls the LiteLLM OpenAI-compatible endpoint using the worker-scoped
virtual key, not the LiteLLM master key:

```bash
export MODEL_BASE_URL='https://infra-lab-services.tail494f6d.ts.net/litellm/v1'
export MODEL_NAME='luna'
export MODEL_API_KEY='<contents of the worker virtual key>'
```

Copy the virtual key to the worker through a secure method and do not commit or
paste it into the repository. Prefer installing the Caddy internal CA on the
worker so normal TLS verification succeeds. For a temporary lab test only, if
the worker does not trust that CA, use:

```bash
export MODEL_TLS_INSECURE=1
```

`MODEL_TLS_INSECURE=1` disables certificate verification for the model request
and should not be used for a persistent deployment.

## Run the worker

Start the worker **before** submitting the run:

```bash
cd ~/agent-worker
export NATS_URL='nats://100.94.49.45:4222'
export WORKER_ID='inkii'
export AGENT_WORKSPACE="$HOME/agent-workspace"
export ALLOW_GENERATED_CODE=1
.venv/bin/python worker.py
```

Optional limits are available for the smoke test:

```bash
export MODEL_TIMEOUT=120
export SCRIPT_TIMEOUT=30
export MAX_SCRIPT_CHARS=30000
export MAX_OUTPUT_CHARS=12000
```

The worker exits after one event. A fresh worker process and a fresh run are
needed for another test. The generated file is written to:

```text
~/agent-workspace/<run-id>/generated_agent.py
```

## Submit a dynamic prompt

From the Agent Manager VM, submit this only after the worker is waiting. The
metadata must match the worker exactly:

```bash
cd /home/infra-lab-user/agent-manager-service
read -r -s -p 'Agent Manager token: ' AGENT_MANAGER_API_TOKEN
printf '\n'
curl -fsS \
  -H "Authorization: Bearer ${AGENT_MANAGER_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:8090/v1/runs \
  -d '{
    "goal": "Create a dependency-free Python 3 script that generates 5,000 deterministic synthetic transaction records using a fixed random seed. Validate every record, write transactions.jsonl, write report.json with counts by status, total amount, average amount, and the five largest transactions, then print a concise summary.",
    "forgejo_repository": "owner/project",
    "base_ref": "main",
    "model": "luna",
    "metadata": {
      "worker_id": "inkii",
      "operation": "create-python-script"
    }
  }'
unset AGENT_MANAGER_API_TOKEN
```

The Manager stores and publishes the `goal`; the worker uses the goal for the
model request. The current Manager does not yet consume `agent.runs.completed`,
so the database run may remain `queued` even after the worker reports success.

For a deterministic test without a model call, add a `script` string to
`metadata`. The worker still requires `ALLOW_GENERATED_CODE=1` before it will
execute it:

```json
"metadata": {
  "worker_id": "inkii",
  "operation": "create-python-script",
  "script": "print('provided test script')"
}
```

If the worker prints `ConnectionRefusedError`, NATS is still bound to loopback
or the override was not applied. If it reports a model API error, check the
LiteLLM URL, virtual key, Caddy certificate trust, and `MODEL_NAME`. If it
reports that the operation or worker does not match, submit a fresh run with
the exact metadata above.
