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
`handle_path /litellm/*` strips the prefix before proxying to `litellm:4000`, so LiteLLM receives `/v1/...`. Caddy and LiteLLM share `caddy_proxy`.

It accepts requests only from Tailscale addresses and requires a worker-scoped
LiteLLM virtual key. Workers must trust Caddy's internal root certificate.
Containers attached to `caddy_proxy` use `http://litellm:4000/v1`; the host-local
gateway uses `http://127.0.0.1:4000/v1`.

`litellm` is a Docker-only hostname. A remote worker cannot resolve it or use
the VM loopback address; it must use the Tailnet-only Caddy hostname and its
worker-scoped key.

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

The worker reads `MODEL_API_KEY` from the environment. Ansible loads each
worker-specific virtual key from `litellm_key_source`, installs it at
`~/.config/litellm/worker-api-key`, and exports it when the worker starts.

Create separate restricted keys rather than sharing one key across laptops:

```bash
cd "$REPO_ROOT/Docker-Documents/litellm-service"
KEY_ALIAS=worker-02 WORKER_ID=worker-02 KEY_FILE=.worker-02-api-key ./create-luna-key.sh
```

Repeat with each worker ID and its matching `.worker-0N-api-key` file. The key
is restricted to the `luna` model; never commit the generated files.

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
worker-scoped key. HTTP 403 means the request did not arrive from the Tailnet;
HTTP 401 means LiteLLM rejected the key.

A remote worker uses `https://infra-lab-services.tail494f6d.ts.net/litellm/v1`.
The deployed Caddy container mounts `~/caddy-service/Caddyfile`; editing this
repository copy does not update the live route until that copy is deployed and
Caddy is reloaded. Test the unauthenticated health route from the worker:

```bash
curl -i --cacert "$HOME/.config/litellm/caddy-root.crt" \
  https://infra-lab-services.tail494f6d.ts.net/litellm/health/liveliness
```

Then test authentication without printing the worker key:

```bash
WORKER_KEY="$(tr -d '\r\n' < "$HOME/.config/litellm/worker-api-key")"
curl -sS -i --cacert "$HOME/.config/litellm/caddy-root.crt" \
  -H "Authorization: Bearer ${WORKER_KEY}" \
  https://infra-lab-services.tail494f6d.ts.net/litellm/v1/models
unset WORKER_KEY
```

A `401` from `/v1/models` means routing and TLS succeeded but LiteLLM rejected
the key. A `404`, `502`, or unexpected Proxmox HTML response points to the
active Caddy route or upstream network; DNS, TLS, and timeout errors point to
tailnet access, CA trust, or service availability. If `/v1/models` succeeds but
a completion reports context overflow, the API is reachable and the remaining
issue is the model/backend.

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
