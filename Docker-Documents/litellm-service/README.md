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

Remote workers use the Tailscale-only Caddy route:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

Containers attached to `caddy_proxy` use:

```text
http://litellm:4000/v1
```

The host-local gateway uses:

```text
http://127.0.0.1:4000/v1
```

`litellm` is a Docker-only hostname. It does not resolve on a remote worker or
in a normal browser.

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

The worker must use the worker-scoped LiteLLM virtual key, not
`LITELLM_MASTER_KEY` or `LUNA_BACKEND_API_KEY`:

```dotenv
MODEL_BASE_URL=https://infra-lab-services.tail494f6d.ts.net/litellm/v1
MODEL_API_KEY=<contents of the worker virtual key>
MODEL_NAME=luna
```

The model key is installed on a worker through a secure channel, for example:

```text
~/.config/agent-worker/luna-worker-api-key
```

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

For a remote worker, use the Caddy hostname and the existing Caddy root CA:

```bash
curl --cacert "$SSL_CERT_FILE" \
  -H "Authorization: Bearer ${MODEL_API_KEY}" \
  "${MODEL_BASE_URL}/models"
```

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
WORKER_ID=inkii
WORKER_ROLE=coder
WORKER_CONSUMER=agent-workers-coder
```

The Open WebUI Pipe submits `worker_role: coder` and does not select a worker
ID. JetStream assigns each event to an available worker.

## Troubleshooting

### LiteLLM health works but completions time out

Test `chat/completions` locally first. If the local request times out, inspect
the LiteLLM-to-backend route, `LUNA_API_BASE`, `LUNA_MODEL`, and the backend
logs. If local works but the Caddy URL fails, inspect Caddy, certificate trust,
and the `/litellm` route.

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
