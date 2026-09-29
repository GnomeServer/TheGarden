# LiteLLM gateway and virtual keys

This Compose project runs LiteLLM as the shared OpenAI-compatible gateway for the lab. It gives Open WebUI and remote agent workers one endpoint while keeping the inference backend behind a virtual key and a stable model name: `luna`.

## Services

- `litellm` — LiteLLM proxy on port `4000`
- `db` — PostgreSQL for virtual keys, users, teams, and spend data
- `luna` — the configured model route; it currently points at the local `llama` container
- `caddy_proxy` — shared with Caddy, Open WebUI, and llama.cpp
- `litellm_internal` — private network between LiteLLM and PostgreSQL

The host port is bound to `127.0.0.1` only. Remote workers reach the gateway through the Tailscale-only Caddy endpoint:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

Containers already attached to `caddy_proxy` use:

```text
http://litellm:4000/v1
```

`litellm` is a Docker-only DNS name. It will not resolve in Firefox, on the VM shell outside a container, or on a remote worker. Use `http://127.0.0.1:4000/v1` from the gateway host, and use the Caddy/Tailscale URL shown above from browsers and remote workers.

The direct llama.cpp endpoint remains available for diagnostics, but clients should use LiteLLM when they need model routing, per-worker credentials, or usage tracking.

## Configure

The local `.env` has been created with generated LiteLLM/PostgreSQL secrets and the current llama.cpp backend key. Keep it private. For a new deployment, use:

```bash
cd /home/infra-lab-user/litellm-service
cp .env.example .env
chmod 600 .env
```

Set at least these values:

```dotenv
POSTGRES_PASSWORD=<long-random-password>
LITELLM_MASTER_KEY=sk-<long-random-admin-key>
LITELLM_SALT_KEY=<long-random-value-that-will-not-be-rotated>
LUNA_API_BASE=http://llama:8080/v1
LUNA_MODEL=openai/luna
LUNA_BACKEND_API_KEY=<API key accepted by the Luna/llama worker>
```

`LUNA_MODEL` is the LiteLLM provider/model value sent to the backend. The public name presented to clients is always `luna`. For a backend that validates the model ID, use its exact model ID with the `openai/` provider prefix. For the current llama.cpp model, that would be similar to:

```dotenv
LUNA_MODEL=openai//models/gemma-3-4b-it-Q4_K_M.gguf
```

If Luna is a remote worker, replace `LUNA_API_BASE` with its Tailscale or private-network OpenAI-compatible URL. Do not expose a worker's inference port to the public Internet.

## Start the stack

Caddy must be running first so that the external `caddy_proxy` network exists. The model backend must also be running and attached to that network when using the local defaults.

```bash
cd /home/infra-lab-user/caddy-service
sudo docker compose up -d caddy

cd /home/infra-lab-user/llama-service
sudo docker compose up -d

cd /home/infra-lab-user/litellm-service
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

Check readiness:

```bash
curl -sS http://127.0.0.1:4000/health/liveliness
```

Check the containers and logs:

```bash
sudo docker compose ps
sudo docker compose logs --tail=100 litellm
sudo docker compose logs --tail=100 db
```

The PostgreSQL volume is named `litellm_postgres_data`. It contains the virtual-key database and must be backed up with the `.env` values, especially `LITELLM_SALT_KEY`.

## Create the Luna virtual API key

After LiteLLM is healthy, run the included provisioning script on the gateway host:

```bash
cd /home/infra-lab-user/litellm-service
./create-luna-key.sh
```

The script calls `/key/generate` with the master key, restricts the new virtual key to the `luna` model, and saves the returned secret with mode `0600` at:

```text
/home/infra-lab-user/litellm-service/.luna-worker-api-key
```

The master key is for administration only. Do not give it to workers. Copy the generated virtual key to each worker's secret store or environment using a secure channel. The script will not create duplicate keys unless `ROTATE=1` is explicitly set.

To use a non-local or externally routed LiteLLM address for administration:

```bash
# Prefer the local URL above. If using Caddy's internal certificate from a
# host that does not trust the Caddy CA, add LITELLM_INSECURE_TLS=1 only for
# this one provisioning call.
LITELLM_INSECURE_TLS=1 \
LITELLM_ADMIN_URL=https://infra-lab-services.tail494f6d.ts.net/litellm \
  ./create-luna-key.sh
```

For manual key management, LiteLLM's administrative endpoints require the master key:

```bash
# List the information for a virtual key. Do not put keys in shell history.
curl -sS \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  "http://127.0.0.1:4000/key/info?key=$(cat .luna-worker-api-key)"

# Revoke it when rotating or retiring a worker credential.
curl -sS -X POST \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"keys":["<virtual-key>"]}' \
  http://127.0.0.1:4000/key/delete
```

Load the master key into the shell without printing it:

```bash
set -a
. ./.env
set +a
```

## Test the gateway

Use the virtual key, not the master key, for model calls. From the gateway host:

```bash
cd /home/infra-lab-user/litellm-service
set -a
. ./.env
set +a
LUNA_KEY="$(cat .luna-worker-api-key)"

curl -sS \
  -H "Authorization: Bearer ${LUNA_KEY}" \
  https://infra-lab-services.tail494f6d.ts.net/litellm/v1/models

curl -k -sS \
  -H "Authorization: Bearer ${LUNA_KEY}" \
  -H 'Content-Type: application/json' \
  https://infra-lab-services.tail494f6d.ts.net/litellm/v1/chat/completions \
  -d '{
    "model": "luna",
    "messages": [{"role": "user", "content": "Reply with exactly OK"}],
    "max_tokens": 16,
    "temperature": 0
  }'
```

For a client on the shared Docker network, use `http://litellm:4000/v1` instead of the external URL. The model name remains `luna`. Do not paste the Docker hostname into Firefox; it is only resolvable by containers on `caddy_proxy`.

## Client configuration

### Open WebUI

In **Admin Panel → Settings → Connections → OpenAI API Connections**, add:

```text
Base URL: http://litellm:4000/v1
API key:  contents of .luna-worker-api-key
```

Select `luna` after the connection is saved. Open WebUI and LiteLLM are both attached to `caddy_proxy`.

### Agent workers

Set the worker's OpenAI-compatible client configuration to:

```dotenv
MODEL_BASE_URL=https://infra-lab-services.tail494f6d.ts.net/litellm/v1
MODEL_API_KEY=<contents of .luna-worker-api-key>
MODEL_NAME=luna
```

The virtual key is intentionally shared only by the model-consuming worker pool. Create separate virtual keys with different `models`, budgets, or aliases when a service should have narrower access.

## Add another inference worker

Add another `model_list` entry to `config.yaml` using the same `model_name: luna` and a different `api_base`/backend key. LiteLLM will treat the entries as one model group and can route requests between them. Then recreate the proxy:

```bash
sudo docker compose up -d --force-recreate litellm
```

Keep the public model name and worker virtual key unchanged when adding backends. Only the gateway configuration needs to change.

## Security and operations

- `LITELLM_MASTER_KEY` can create, inspect, and revoke keys; store it only on the gateway host.
- `LITELLM_SALT_KEY` protects provider credentials stored in PostgreSQL and must be retained for restores. Do not rotate it casually.
- The Caddy route is on the private Tailscale hostname. Keep the Tailscale ACL restricted to administrators and approved workers.
- The host port is loopback-only and PostgreSQL is not published to the host.
- Back up the `litellm_postgres_data` volume and the deployment `.env` file separately from worker credentials.
- Rotate the Luna virtual key if it is copied to an untrusted machine; revoke the old key through `/key/delete`.
