# LiteLLM gateway and virtual keys

This Compose project runs LiteLLM as the shared OpenAI-compatible gateway for
Open WebUI and the remote worker pool. The public model name is `luna`.

## Repository layout

From the repository root:

```bash
export REPO_ROOT=/path/to/TheGarden
cd "$REPO_ROOT/Docker-Documents/litellm-service"
```

The local `.env` and `.luna-worker-api-key` files are deployment secrets. They
are not committed to Git.

## Endpoints

The Caddy service exposes a Tailnet-only LiteLLM worker route:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

It accepts requests only from Tailscale addresses and requires a worker-scoped
LiteLLM virtual key. Workers must trust Caddy's internal root certificate.
Containers attached to `caddy_proxy` use `http://litellm:4000/v1`; the host-local
gateway uses `http://127.0.0.1:4000/v1`.

`litellm` is a Docker-only hostname. A remote worker cannot resolve it or use
the VM loopback address; it must use the Caddy hostname and worker key.

## Configuration

Copy the non-secret template and create a private `.env`:

```bash
cd "$REPO_ROOT/Docker-Documents/litellm-service"
cp .env.example .env
chmod 600 .env
```

The important settings are:

```dotenv
LITELLM_MASTER_KEY=<administration-only-key>
LITELLM_SALT_KEY=<stable-encryption-salt>
LUNA_API_BASE=http://llama:8080/v1
LUNA_MODEL=openai/luna
LUNA_BACKEND_API_KEY=<key-accepted-by-the-inference-backend>
```

`LUNA_MODEL` and `LUNA_API_BASE` describe the backend route. The public model
name presented to clients remains `luna`.

The gateway config enforces a shared `$25,000` spend ceiling over `365d` across
all LiteLLM traffic. Budget enforcement depends on the PostgreSQL database being
available. The separate
[`API Guard dashboard`](../api-guard-dashboard/README.md) compares rolling
365-day spend with that limit and shows `$2,083.33` per month as a pacing
target; monthly pacing is not an additional hard cap. Keep the matching
`API_GUARD_ANNUAL_BUDGET_USD` value at `25000` unless the LiteLLM config changes
with it.

The ceiling only covers calls sent through this proxy. Set an independent
upstream provider spend limit and alert (for example, on OpenRouter) and do not
give the upstream provider key to workers or OMP. API Guard manages LiteLLM
virtual keys; upstream provider credentials remain in LiteLLM's private
deployment environment and must be rotated there.

Ansible-managed Dark Factory agent workers obtain model-scoped keys from
Keybroker for each run; the worker role no longer reads `MODEL_API_KEY` or
installs `~/.config/litellm/worker-api-key`. After the Keybroker cutover is
verified on a worker, revoke its legacy LiteLLM key in API Guard. Do not use the
static-key instructions below for enrolled agent workers.

`create-luna-key.sh` remains for manual clients that have not migrated to
Keybroker. Create a separately restricted key per such client and keep its
secret local:

```bash
cd "$REPO_ROOT/Docker-Documents/litellm-service"
KEY_ALIAS=manual-client KEY_FILE=.manual-client-api-key ./create-luna-key.sh
```

Do not distribute those keys to the four Ansible-managed agent workers. The
worker-role cutover removes the old `~/.config/litellm/worker-api-key` file but
does not revoke the corresponding key in LiteLLM.
## Start the stack

Caddy and the inference backend must be running first so the external
`caddy_proxy` network exists:

```bash
cd "$REPO_ROOT/Docker-Documents/caddy_service"
sudo docker compose up -d caddy

cd "$REPO_ROOT/Docker-Documents/llama_services"
sudo docker compose up -d

cd "$REPO_ROOT/Docker-Documents/litellm-service"
sudo docker compose config --quiet
sudo docker compose up -d
sudo docker compose ps
```

Check readiness and logs:

```bash
curl -sS http://127.0.0.1:4000/health/liveliness
sudo docker compose logs --tail=100 litellm
sudo docker compose logs --tail=100 db
```

## Create the worker virtual key

After LiteLLM is healthy, run the provisioning script from this project:

```bash
cd "$REPO_ROOT/Docker-Documents/litellm-service"
./create-luna-key.sh
```

The script uses the master key to create a virtual key restricted to `luna`.
It saves the secret locally with mode `0600` in `.luna-worker-api-key`.

The master key is for administration only. Never copy it to workers or commit
it to Git.

## Test the gateway

Use the virtual key for model requests:

```bash
cd "$REPO_ROOT/Docker-Documents/litellm-service"
LUNA_KEY="$(cat .luna-worker-api-key)"

curl -sS \
  -H "Authorization: Bearer ${LUNA_KEY}" \
  "http://127.0.0.1:4000/v1/models"

curl -sS \
  -H "Authorization: Bearer ${LUNA_KEY}" \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:4000/v1/chat/completions \
  -d '{
    "model": "luna",
    "messages": [{"role": "user", "content": "Reply with exactly OK"}],
    "max_tokens": 16,
    "temperature": 0
  }'
```

The Caddy worker route accepts only Tailscale source addresses and requires a
worker-scoped key. HTTP 403 indicates the request did not arrive from the
Tailnet; HTTP 401 indicates LiteLLM rejected the key.

A `401` response confirms that TLS and routing work but authentication failed.
A timeout or model error requires inspecting the LiteLLM and backend logs.

## Open WebUI

In **Admin Panel → Settings → Connections → OpenAI API Connections**, use:

```text
Base URL: http://litellm:4000/v1
API key:  worker virtual key
Model:    luna
```

Open WebUI and LiteLLM are both attached to `caddy_proxy`.

## Worker pool

Each worker keeps a unique identity but shares the consumer for its role:

```text
WORKER_ID=worker-02
WORKER_ROLE=coder
WORKER_CONSUMER=agent-workers-coder
```

The Open WebUI Pipe submits `worker_role: coder` and does not select a worker
ID. JetStream assigns each event to an available worker.

## Troubleshooting

### LiteLLM health works but completions time out

Test `chat/completions` locally first. If the local request times out, inspect
the LiteLLM-to-backend route, `LUNA_API_BASE`, `LUNA_MODEL`, and backend logs.
If local requests work but the worker route fails, check Caddy, the worker's
trusted Caddy root certificate, and the worker-scoped virtual key.

### Certificate verification fails

Use the existing Caddy root certificate. Do not create another CA or copy
`root.key`. The URL must use
`infra-lab-services.tail494f6d.ts.net`, not the Tailscale IP, because the
certificate is issued for the hostname.

### Authentication fails

The worker and Open WebUI use the LiteLLM virtual key. The LiteLLM master key
is only for `/key/*` administration. The backend key is only used by LiteLLM
to call the configured inference backend.

### Model not found

The public client model is `luna`. Check the backend-specific `LUNA_MODEL`
value and compare it with the model ID returned by the backend `/v1/models`
endpoint.

### Logs

```bash
cd "$REPO_ROOT/Docker-Documents/litellm-service"
sudo docker compose logs --follow --tail=200 litellm
```
