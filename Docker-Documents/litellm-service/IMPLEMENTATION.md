# LiteLLM implementation notes

The canonical LiteLLM project is stored at:

```text
TheGarden/Docker-Documents/litellm-service
```

Use [`README.md`](README.md) for the current configuration and operational
commands. The Compose project uses the public model alias `luna`, a LiteLLM
virtual key for workers, and the Tailscale-only Caddy route:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

## Credential boundaries

- `LITELLM_MASTER_KEY` is for LiteLLM administration only.
- `LUNA_BACKEND_API_KEY` is used by LiteLLM to call the configured inference
  backend.
- `.luna-worker-api-key` is the worker-scoped virtual key used by workers and
  Open WebUI.

None of these secret values belong in Git. The local `.env` and generated key
file remain deployment-only files.

## Current request path

```text
Open WebUI or worker
  → Caddy /litellm route
  → LiteLLM :4000
  → configured Luna backend
```

Containers on `caddy_proxy` use `http://litellm:4000/v1`. Remote workers use
the Caddy/Tailscale hostname and must trust the existing Caddy root CA.

## Validation order

1. Check LiteLLM health.
2. Test `/v1/models` with the worker virtual key.
3. Test `/v1/chat/completions` locally through LiteLLM.
4. Test the same request through Caddy.
5. Test from the worker with its CA bundle.
6. Inspect LiteLLM, Caddy, and backend logs for timeouts.

Do not create a second CA, copy `root.key`, expose PostgreSQL, or give the
LiteLLM master key to a worker.
