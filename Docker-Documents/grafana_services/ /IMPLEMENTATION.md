# LiteLLM Gateway Implementation

This document records the LiteLLM gateway and virtual API-key work completed for the infrastructure lab.

## Goal

Provide Open WebUI and agent worker nodes with one OpenAI-compatible gateway instead of connecting each client directly to an inference worker.

The gateway currently exposes a stable model name:

```text
luna
```

LiteLLM handles client authentication and virtual-key permissions, while the configured backend remains the existing llama.cpp server.

## Resulting architecture

```text
Open WebUI container
  -> http://litellm:4000/v1
  -> LiteLLM
  -> luna model route
  -> http://llama:8080/v1
  -> llama.cpp inference server
```

Remote worker nodes use the Caddy/Tailscale endpoint:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

The Docker hostname `litellm` is only resolvable by containers attached to the `caddy_proxy` network. It is not a DNS name that Firefox or remote worker hosts can resolve.

## Files created

The deployment is located at:

```text
/home/infra-lab-user/litellm-service
```

| File | Purpose |
|---|---|
| `compose.yml` | LiteLLM and PostgreSQL Compose services |
| `config.yaml` | `luna` model route and LiteLLM settings |
| `.env.example` | Non-secret deployment template |
| `.env` | Local deployment secrets; mode `0600`; do not commit |
| `.gitignore` | Excludes secrets and the generated worker key |
| `create-luna-key.sh` | Creates and stores the worker virtual API key |
| `README.md` | Deployment and usage instructions |
| `IMPLEMENTATION.md` | This implementation record |

## Containers and persistence

### LiteLLM

```text
Container: litellm
Image:     ghcr.io/berriai/litellm:v1.103.0
Port:      4000
Host bind: 127.0.0.1:4000
```

The host bind is loopback-only. Remote access is provided through Caddy rather than directly publishing port `4000` to the LAN.

### PostgreSQL

```text
Container: litellm-db
Image:     postgres:16-alpine
Database:  litellm
Volume:    litellm_postgres_data
```

PostgreSQL stores LiteLLM virtual keys and usage information. PostgreSQL is attached only to the private `litellm_internal` network and is not published to the host.

### Docker networks

LiteLLM is attached to:

```text
litellm_internal  private LiteLLM/PostgreSQL network
caddy_proxy       shared application and reverse-proxy network
```

The external `caddy_proxy` network must exist before starting this Compose project.

## Model configuration

The gateway has one model group:

```yaml
model_list:
  - model_name: luna
    litellm_params:
      model: os.environ/LUNA_MODEL
      api_base: os.environ/LUNA_API_BASE
      api_key: os.environ/LUNA_BACKEND_API_KEY
```

The current local settings are:

```dotenv
LUNA_API_BASE=http://llama:8080/v1
LUNA_MODEL=openai/luna
LUNA_BACKEND_API_KEY=<llama.cpp backend key>
```

The public client-facing name is `luna`. The backend key is separate from the LiteLLM master key and the virtual worker key.

If the backend validates model IDs, set `LUNA_MODEL` to the exact provider/model value, for example:

```dotenv
LUNA_MODEL=openai//models/gemma-3-4b-it-Q4_K_M.gguf
```

Additional inference workers can be added later as additional `model_list` entries with the same `model_name: luna`. LiteLLM can then load-balance the route without changing client configuration.

## Authentication and key management

There are three different credentials:

| Credential | Used by | Purpose |
|---|---|---|
| `LITELLM_MASTER_KEY` | Gateway administrator only | Creates, inspects, and revokes virtual keys |
| `LUNA_BACKEND_API_KEY` | LiteLLM only | Authenticates to the llama.cpp/inference backend |
| `.luna-worker-api-key` | Open WebUI and agent workers | Calls the `luna` route through LiteLLM |

The master key must not be distributed to workers.

The worker virtual key is restricted to:

```text
Model: luna
Key type: llm_api
Key alias: luna-worker
```

The generated key is stored locally at:

```text
/home/infra-lab-user/litellm-service/.luna-worker-api-key
```

The file is excluded by `.gitignore` and has mode `0600`.

### Create or retrieve the worker key

```bash
cd /home/infra-lab-user/litellm-service
./create-luna-key.sh
```

If the key file already exists, the script does not create a duplicate. The script waits for LiteLLM's PostgreSQL-backed readiness endpoint before generating a key, preventing a startup race during database migration.

### Check key permissions

```bash
stat -c '%a %n' /home/infra-lab-user/litellm-service/.luna-worker-api-key
```

Expected mode:

```text
600 /home/infra-lab-user/litellm-service/.luna-worker-api-key
```

### Revoke a key

Load the master key without printing it:

```bash
cd /home/infra-lab-user/litellm-service
set -a
. ./.env
set +a
```

Then revoke the old virtual key:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"keys":["<virtual-key>"]}' \
  http://127.0.0.1:4000/key/delete
```

Do not place the actual key value in source files or documentation.

## Caddy routing

The following route was added to `/home/infra-lab-user/caddy-service/Caddyfile`:

```caddyfile
@litellm {
    path /litellm /litellm/*
}

handle @litellm {
    uri strip_prefix /litellm
    reverse_proxy litellm:4000
}
```

Therefore:

```text
External: https://infra-lab-services.tail494f6d.ts.net/litellm/v1
Internal: http://litellm:4000/v1
```

The `/litellm` prefix is stripped before the request reaches LiteLLM.

## Client configuration

### Open WebUI

In Open WebUI:

```text
Admin Panel
  -> Settings
  -> Connections
  -> OpenAI API Connections
```

Use:

```text
Base URL: http://litellm:4000/v1
API key:  contents of .luna-worker-api-key
Model:    luna
```

The internal URL is correct in Open WebUI because the Open WebUI container and LiteLLM container share `caddy_proxy`.

Do not open `http://litellm:4000/v1` directly in Firefox. Firefox runs outside Docker and cannot resolve the Docker service name.

### Remote agent workers

Use the external URL from worker nodes:

```dotenv
MODEL_BASE_URL=https://infra-lab-services.tail494f6d.ts.net/litellm/v1
MODEL_API_KEY=<contents of .luna-worker-api-key>
MODEL_NAME=luna
```

The workers must have Tailscale access to the gateway hostname. The Tailscale ACL should allow approved workers to reach the gateway.

### Agent manager

The agent-manager configuration was changed to default to:

```text
http://litellm:4000/v1
```

The relevant settings are:

```dotenv
MODEL_BASE_URL=http://litellm:4000/v1
MODEL_API_KEY=<worker virtual key>
```

The initial agent-manager API does not yet make model calls, but its future model-client default now points to LiteLLM rather than directly to llama.cpp.

## Deployment commands

Start Caddy first so the external Docker network exists:

```bash
cd /home/infra-lab-user/caddy-service
sudo docker compose up -d --force-recreate caddy
```

Start the inference backend if using the local default:

```bash
cd /home/infra-lab-user/llama-service
sudo docker compose up -d
```

Start LiteLLM and PostgreSQL:

```bash
cd /home/infra-lab-user/litellm-service
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

Check LiteLLM readiness:

```bash
curl -sS http://127.0.0.1:4000/health/readiness
```

Expected response includes:

```json
{"status":"healthy","db":"connected"}
```

Check logs:

```bash
sudo docker compose logs --tail=100 litellm
sudo docker compose logs --tail=100 db
```

## Verification performed

The following checks completed successfully:

- LiteLLM Compose configuration validation
- Caddy, Open WebUI, and agent-manager Compose configuration validation
- Shell syntax validation for `create-luna-key.sh`
- LiteLLM liveness check
- LiteLLM PostgreSQL readiness check
- Virtual key creation
- Authenticated `/v1/models` request returning the `luna` model
- Authenticated chat completion through LiteLLM returning `OK`
- Authenticated external request through the Caddy/Tailscale route

Example external verification:

```bash
cd /home/infra-lab-user/litellm-service
KEY="$(cat .luna-worker-api-key)"

curl -k \
  -H "Authorization: Bearer ${KEY}" \
  https://infra-lab-services.tail494f6d.ts.net/litellm/v1/models
```

## Backups and secret retention

Back up:

```text
litellm_postgres_data
/home/infra-lab-user/litellm-service/.env
```

Retain `LITELLM_SALT_KEY` with the backup. It is used to encrypt provider credentials stored in the LiteLLM database and must not be casually changed after deployment.

Keep these files private:

```text
/home/infra-lab-user/litellm-service/.env
/home/infra-lab-user/litellm-service/.luna-worker-api-key
```
