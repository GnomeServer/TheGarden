# Keybroker service

Keybroker creates short-lived, worker-scoped LiteLLM virtual keys and owns the
LiteLLM master key used for virtual-key administration. It stores session
metadata and LiteLLM token identifiers/hashes in PostgreSQL, never the virtual
key itself. The raw LiteLLM key is returned only in the successful mint
response, with `Cache-Control: no-store`; it is not written to logs, database
records, or NATS.

## Deployment

The Compose project has a private PostgreSQL service and a Keybroker service on
the external `caddy_proxy` network. Neither service publishes a host port. The
Keybroker container joins `caddy_proxy` so Caddy and API Guard can reach it;
Postgres is attached only to the private `keybroker_internal` network.

Create a local environment file and protect it:

```bash
cd Docker-Documents/keybroker-service
cp .env.example .env
chmod 600 .env
```

Set distinct high-entropy values for `POSTGRES_PASSWORD`,
`KEYBROKER_DASHBOARD_TOKEN`, and `KEYBROKER_PROXY_TOKEN`. Use the existing
LiteLLM master key for `KEYBROKER_LITELLM_MASTER_KEY` and the same exposure
webhook secret configured for API Guard. Never put any of these values in
worker configuration, mission input, NATS, or source control. The master key
is consumed only by Keybroker for LiteLLM key and spend administration.

After the `caddy_proxy` network, LiteLLM, and API Guard are available:

```bash
docker compose up -d --build
docker compose ps
```

The required Caddy route is Tailnet-only at
`https://infra-lab-services.tail494f6d.ts.net/keybroker`; it strips
`/keybroker`, proxies to `keybroker:8000`, and injects
`X-Darkfactory-Client-IP` with the actual remote host plus
`X-Darkfactory-Proxy-Token` with the configured proxy token. Keybroker checks
both headers and maps the IP to a worker server-side. It does not accept
`worker_id` in request bodies or trust worker-supplied identity. Do not expose
the service to non-Caddy clients. API Guard uses `http://keybroker:8000` over
Docker networking and authenticates admin routes with
`Authorization: Bearer $KEYBROKER_DASHBOARD_TOKEN`.

Current enrolled worker mappings are fixed to the four entries from
`Ansible/inventory/hosts.yml`:

| Worker | Worker ID | Tailnet IP |
| --- | --- | --- |
| Inkii | `worker-02` | `100.98.125.127` |
| Naruto | `worker-04` | `100.88.92.78` |
| Raphael | `worker-01` | `100.116.35.103` |
| Donatello | `worker-03` | `100.94.145.104` |

Sammy and Michelangelo are not enrolled. Add them only after explicit owner
opt-in and configuring their SSH identity and worker enrollment.

`GET /v1/worker/identity` verifies the Caddy-injected token and source-IP
mapping without minting a key. Ansible calls it from each worker and checks
the returned `worker_id` before it removes any legacy static key file.

## Session policy and worker API

Session requests are authenticated using only Caddy's trusted headers. The
server ignores mission-provided model, budget, duration, and worker identity;
those fields are rejected if present. `operation` is accepted to satisfy the
worker request contract but is not stored or passed to LiteLLM.

Default settings are `luna`, a `$25` per-session max budget, and a 3600-second
key lifetime. They can be changed using `KEYBROKER_DEFAULT_MODEL`,
`KEYBROKER_ALLOWED_MODELS`, `KEYBROKER_SESSION_MAX_BUDGET_USD`, and
`KEYBROKER_SESSION_TTL_SECONDS`; the selected model must remain in the
server-side allowlist and a session budget cannot exceed the administrator
budget ceiling. The global LiteLLM `$25,000/365d` limit remains independent.
The returned model base URL defaults to
`https://infra-lab-services.tail494f6d.ts.net/litellm/v1`.

Session records enforce uniqueness for each `(worker_id, run_id)`. A duplicate
mint returns HTTP 409 and does not return another key; it attempts to revoke
the existing session key so a retry after a lost response fails closed. A
background reaper checks expired, unrevoked sessions every
`KEYBROKER_REAPER_INTERVAL_SECONDS` (default 60), deletes their LiteLLM keys,
and marks the records revoked. LiteLLM's duration TTL remains a backstop.

### Mint a session

`POST /v1/sessions`

```json
{"run_id":"run-123","operation":"coding"}
```

Success returns `session_id`, one-time `key`, `key_id`, `key_alias`,
`expires_at`, `model_base_url`, and `model`. Keep the key only in worker
process memory for that run. Do not place it in logs, artifacts, persisted
configuration, or NATS. Worker-side output scanning/redaction and a sanitized
run-result warning are responsibilities of the worker integration.


### Revoke a session

`DELETE /v1/sessions/{session_id}`

The session must belong to the authenticated worker. Revocation is idempotent;
LiteLLM is asked to delete the token before the record is marked revoked.

### Report exposure

`POST /v1/sessions/{session_id}/exposure`

```json
{
  "event_id":"event-123",
  "reason":"credential_pattern_in_output",
  "session_key_match":true
}
```

Worker reason values `session_key_echo_detected` and
`credential_pattern_detected` are mapped to API Guard's
`configured_credential_echo` and `credential_pattern_in_output` categories.
A matched current-session key is revoked before event delivery when LiteLLM is
available. Keybroker persists a safe-only event in its PostgreSQL outbox before
trying API Guard; if API Guard is unavailable the event is retried by the
maintenance loop with backoff. The response still sets `stop_run: true` and
reports `notification_pending: true` until API Guard accepts it. Workers must
stop locally as soon as they detect an exposure and must not wait for a
successful broker response to contain it. For an unassociated key-like
pattern, Keybroker does not revoke and sends `key_id: null` to avoid
implicating an unrelated LiteLLM key. The worker must redact the pattern and
stop later model calls for that run.
Provider/master-key exposure is not revoked here and requires human escalation
and rotation. The signed HMAC-SHA256 API Guard event contains only the event,
safe key alias/ID when matched, reason, timestamp, worker ID, and session
ID—never a credential, prompt, or model output. Event delivery is at-least-once;
API Guard deduplicates by event ID.

## API Guard administration

All routes below require the dashboard bearer token:

- `GET /v1/admin/keys` — safe key inventory rows; raw key fields are removed.
- `GET /v1/admin/spend-logs?start=...&end=...&page=1&page_size=1000` — paginated
  aggregate spend fields only; prompt/output and raw-key fields are removed.
- `POST /v1/admin/keys/generate` — accepts `alias`, `models`,
  `max_budget_usd`, optional `budget_duration` and `duration`; models and
  budget are checked against server policy. The generated key is returned once
  and is not stored.
- `POST /v1/admin/keys/revoke` — accepts `{ "key_id": "..." }` and revokes
  the LiteLLM key.

Generated keys are the only responses other than session mint that contain a
raw virtual key. Both mint and generation responses disable HTTP caching.

## Health and tests

- `GET /healthz` reports process liveness.
- `GET /readyz` also checks PostgreSQL connectivity and is used by Compose.

Tests use a temporary SQLite database and stub LiteLLM/API Guard network calls;
they do not require a running stack or contain real credentials. Install the
pinned test dependencies and run them from this directory:

```bash
python -m pip install -r requirements-test.txt
pytest -q
```
