# LiteLLM implementation notes

The canonical LiteLLM project is stored at:

```text
TheGarden/Docker-Documents/litellm-service
```

Use [`README.md`](README.md) for the current configuration and operational
commands. The Compose project uses the public model alias `luna`, a LiteLLM
virtual key. The checked-in Caddyfile exposes the worker URL at

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

The route is restricted to Tailscale source addresses and authenticates through
worker-scoped LiteLLM virtual keys. Workers trust Caddy's internal CA.

## Credential boundaries

- `LITELLM_MASTER_KEY` is for LiteLLM administration only.
- `LUNA_BACKEND_API_KEY` is used by LiteLLM to call the configured inference
  backend.
`create-luna-key.sh` defaults to one generic worker key. Set `KEY_ALIAS`,
`WORKER_ID`, and `KEY_FILE` to create one `luna`-restricted key for each worker;
Ansible inventory points `litellm_key_source` at those private files.

None of these secret values belong in Git. The local `.env` and generated key
file remain deployment-only files.

## Current request path

```text
Open WebUI container
  → http://litellm:4000/v1 on caddy_proxy
  → LiteLLM
  → configured Luna backend
```

Remote workers use the Caddy endpoint with their own virtual key. Validate
LiteLLM readiness, then test `/v1/models` and `/v1/chat/completions` from each
worker using the installed Caddy root certificate. Do not use Caddy's internal
root key on workers, and do not disable TLS verification in persistent
configuration.

Do not create a second CA, copy `root.key`, expose PostgreSQL, or give the
LiteLLM master key to a worker.
