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

The route is restricted to Tailscale source addresses. Enrolled remote agent
workers use Keybroker to obtain a short-lived, model-scoped LiteLLM virtual key
per run; they do not store a static virtual key. Keybroker holds the LiteLLM
master key. Workers trust Caddy's internal CA.

## Credential boundaries

- `LITELLM_MASTER_KEY` is for LiteLLM administration only.
- `LUNA_BACKEND_API_KEY` is used by LiteLLM to call the configured inference
  backend.

Ansible-managed worker inventory no longer provisions `litellm_key_source`.
The role removes each legacy local key file after Keybroker readiness and
stops the old worker process. It does **not** revoke the old LiteLLM virtual
key; an administrator must revoke that key after verifying the worker's new
session-key flow.

`create-luna-key.sh` remains for manual clients outside the enrolled worker
role. Use one restricted key per client, never commit it, and do not treat that
manual workflow as Keybroker-backed rotation.

None of these secret values belong in Git. The local `.env` and generated key
file remain deployment-only files.

## Current request path

```text
Open WebUI container
  → http://litellm:4000/v1 on caddy_proxy
  → LiteLLM
  → configured Luna backend
```

Remote worker requests reach the Tailnet-only Caddy endpoint with a per-run
virtual key returned by Keybroker. Validate LiteLLM readiness, then test
`/v1/models` and `/v1/chat/completions` from an enrolled worker using the
Keybroker-issued key for that run and the installed Caddy root certificate. Do
not use Caddy's internal root key on workers, and do not disable TLS
verification in persistent configuration. VM-local checks use
`http://127.0.0.1:4000/v1`; containers on `caddy_proxy` use
`http://litellm:4000/v1`.

## Validation order

1. Check LiteLLM health.
2. Test `/v1/models` with the Keybroker-issued key for an enrolled worker run.
3. Test `/v1/chat/completions` locally through LiteLLM.
4. Test the Caddy endpoint from a tailnet device with the Caddy root CA.
5. Test from the worker with its CA bundle and Keybroker session.
6. Inspect LiteLLM, Caddy, and backend logs for timeouts.

Do not create a second CA, copy `root.key`, expose PostgreSQL, or give the
LiteLLM master key to a worker.
