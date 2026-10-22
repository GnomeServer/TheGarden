# LiteLLM implementation notes

The canonical LiteLLM project is stored at:

```text
TheGarden/Docker-Documents/litellm-service
```

Use [`README.md`](README.md) for the current configuration and operational
commands. The Compose project exposes the public model alias `luna`; remote
workers authenticate with worker-scoped LiteLLM virtual keys. The Caddy service
exposes the Tailnet-only worker URL at

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

Remote workers use the Tailnet-only Caddy endpoint above with their own
worker-scoped virtual key. Validate LiteLLM readiness, then test `/v1/models`
and `/v1/chat/completions` from a worker using the installed Caddy root
certificate. Do not copy Caddy's internal root key or disable TLS verification.
VM-local checks use `http://127.0.0.1:4000/v1`; containers on `caddy_proxy` use
`http://litellm:4000/v1`.

## Validation order

1. Check LiteLLM health.
2. Test `/v1/models` with the worker virtual key.
3. Test `/v1/chat/completions` locally through LiteLLM.
4. Test the Caddy endpoint from a tailnet device with the Caddy root CA.
5. Test from the worker with its CA bundle.
6. Inspect LiteLLM, Caddy, and backend logs for timeouts.

Do not create a second CA, copy `root.key`, expose PostgreSQL, or give the
LiteLLM master key to a worker.
