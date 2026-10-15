# LiteLLM implementation notes

The canonical LiteLLM project is stored at:

```text
TheGarden/Docker-Documents/litellm-service
```

Use [`README.md`](README.md) for the current configuration and operational
commands. The Compose project uses the public model alias `luna`, a LiteLLM
virtual key. The checked-in Caddyfile does not expose this URL:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

## Credential boundaries

- `LITELLM_MASTER_KEY` is for LiteLLM administration only.
- `LUNA_BACKEND_API_KEY` is used by LiteLLM to call the configured inference
  backend.
- `.luna-worker-api-key` is the single virtual key written by `create-luna-key.sh`. Ansible inventory expects separate `.worker-0N-api-key` files, which that script does not create.

None of these secret values belong in Git. The local `.env` and generated key
file remain deployment-only files.

## Current request path

```text
Open WebUI container
  → http://litellm:4000/v1 on caddy_proxy
  → LiteLLM
  → configured Luna backend
```

Remote workers do not have a Caddy path to LiteLLM. VM-local checks use `http://127.0.0.1:4000/v1`. Do not treat a Caddy `/litellm` test as a current success criterion.

## Validation order

1. Check LiteLLM health.
2. Test `/v1/models` with the worker virtual key.
3. Test `/v1/chat/completions` locally through LiteLLM.
4. Do not expect the Caddy `/litellm` request to succeed. That route is absent.
5. Test from the worker with its CA bundle.
6. Inspect LiteLLM, Caddy, and backend logs for timeouts.

Do not create a second CA, copy `root.key`, expose PostgreSQL, or give the
LiteLLM master key to a worker.
