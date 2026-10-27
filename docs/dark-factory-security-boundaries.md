# Dark Factory security boundaries and incident response

This document separates implemented safeguards from controls that still need
operator setup. API Guard is an administrator dashboard; Keybroker and the
worker runtime enforce the per-run credential flow. Neither is a complete
endpoint-security or model-escape detection system.

## Remote workers and credential lifecycle

The Keybroker trusted-source allowlist includes the six designated agent hosts:

| Host | Worker ID | Tailnet IP |
| --- | --- | --- |
| Inkii | `worker-02` | `100.98.125.127` |
| Naruto | `worker-04` | `100.88.92.78` |
| Raphael | `worker-01` | `100.116.35.103` |
| Donatello | `worker-03` | `100.94.145.104` |
| Sammy | `worker-05` | `100.79.110.117` |
| Michelangelo | `worker-06` | `100.70.246.104` |

The checked-in Ansible inventory now contains all six designated hosts,
including Sammy (`dell-drd17c4`, SSH user `dow_admin`) and Michelangelo (SSH
user `dow_admin`). Inventory presence is configuration only: the worker
runtime and egress policy take effect only after a successful Ansible run and
host-side verification.

The separate `Worker-Nodes/Pi-LiteLLM-Worker-setup.md` procedure still
describes a manually installed static-key client. If that Pi setup is active,
it is outside the six-host Keybroker allowlist and six-host Ansible inventory
and must be migrated or treated as a legacy long-lived-key exception.

For enrolled workers, Ansible verifies Keybroker over HTTPS, stops the legacy
worker process, removes the old local `worker-api-key` file, and installs a
systemd-supervised worker. Each run requests a model-scoped LiteLLM virtual key
with a server-owned budget and one-hour TTL. The worker holds that key in
process memory only, revokes it at run completion, and Keybroker's reaper
revokes expired sessions after crashes. The TTL is the backstop, not a promise
that a crashed process's key disappears immediately. Keybroker stores only
safe session metadata and LiteLLM token identifiers/hashes; LiteLLM stores the
upstream OpenAI provider credential.

The Ansible cutover deletes the worker's local key file; it does **not** revoke
the corresponding legacy LiteLLM key. After each worker passes a session-key
smoke check, an administrator must revoke the old key in API Guard. Until then,
that old key remains usable if copied elsewhere. OMP/Agent Manager's own
configured `MODEL_API_KEY` is also a separate, longer-lived LiteLLM virtual key;
its current exposure control is redaction plus a signed alert, not per-chat
Keybroker issuance. API Guard's routine key rotation is an administrator
workflow and does not rewrite consumer secrets automatically.

The LiteLLM master key is held by Keybroker, not remote workers or API Guard.
The provider/OpenAI key remains in LiteLLM's private deployment environment;
workers and OMP should never receive it.

## Exposure reporting and 24/7 notification

The current worker incident path is:

1. Worker scans prompts and model responses for an exact match to its current
   session key and a limited set of recognized provider-key formats. A match
   is withheld, further model calls stop, and the run returns a sanitized
   `security_notice`.
2. Keybroker revokes an exact current-session key when LiteLLM is available.
   It stores a secret-free signed-event payload in PostgreSQL before attempting
   API Guard delivery. When API Guard is unavailable, Keybroker retries the
   queued event with backoff.
3. API Guard verifies HMAC, rejects raw key/prompt/output fields, stores the
   exposure event, and—when `API_GUARD_ALERT_WEBHOOK_URL` is configured—queues a
   normalized notification in its own durable outbox. Failed webhook deliveries
   retry with backoff. Event IDs let downstream receivers deduplicate
   at-least-once delivery.
4. The worker completion result and API Guard dashboard are visual alert
   surfaces. The optional HTTP webhook can bridge the event to an existing
   Forge notification relay or paging service.

This queue covers remote worker session events. Agent Manager OMP's own
`MODEL_API_KEY` remains a longer-lived virtual key, but OMP stores its
secret-free exposure event in the manager database before attempting API Guard
delivery and retries it through the manager's background outbox. If the manager
database is unavailable, OMP still redacts the value and warns the current user
but cannot durably queue that event. The manager key still requires manual
rotation in API Guard; it is not yet a per-chat Keybroker session.

This is **not built-in email**, a pager, an on-call rota, or proof that a person
acknowledged the incident. A `2xx` response means the receiver accepted the
webhook; it does not mean someone acted. Email alone is not sufficient for a
24/7 response unless it feeds a monitored, acknowledged on-call process. For
production, route the webhook to the wider Forge's existing incident/paging
service if it supports signed webhooks; otherwise use an approved relay. Assign
primary and backup responders, require acknowledgement, escalate after a
short no-ack interval, and test the after-hours path. The API Guard dashboard
shows queued delivery state but cannot schedule or page responders.

If API Guard is unavailable, Keybroker and Agent Manager retain pending events
in their separate databases and retry delivery after recovery; downstream
external paging is delayed until API Guard accepts the event. If the
originating service's database is also unavailable, its event cannot be
durably queued. The worker still stops locally, and OMP shows a warning to the
current user; neither state proves that an on-call responder was notified.

## Model escape and security monitoring

### Implemented boundaries

- Remote workers receive only per-run LiteLLM virtual keys, scoped by Keybroker
to the server-approved `luna` alias by default, capped at `$25` per session,
with a one-hour TTL. Mission payloads cannot choose the model, budget, TTL, or
worker identity.
- API Guard controls administrative key issuance, revocation, spend visibility,
and exposure-event review. It is not on the inference data path and does not
replace LiteLLM's `$25,000/365d` hard budget or provider-side spend limits.
- The `crew_rules.container_create_body` helper encodes a no-network, read-only,
  resource-limited container policy, but source search found no callsite that
  enforces it in a running mission path. Do not count that helper as an active
  security boundary without a live integration test.
- Remote-worker mission artifacts are saved and returned; mission execution
does not execute those generated artifacts or modify Forgejo repositories.
- The separate `create-python-script` path remains disabled by default. If
 enabled, it runs generated Python as the worker operating-system user and is
 **not sandboxed**; keep it disabled on hosts holding valuable data or
 credentials.
- Caddy limits Keybroker and API Guard routes to Tailscale source addresses.
  Keybroker additionally requires Caddy's proxy token and maps the trusted client
  IP to one of the six allowlisted workers. API Guard has separate Forgejo OAuth
  and admin allowlisting.
- The Ansible worker unit configures `IPAddressDeny=any` and allows only the
  service VM (`100.94.49.45/32`). Ansible pins the Keybroker TLS hostname to
  that IP in the worker's `/etc/hosts`; the worker needs no DNS resolver
  allowlist. This per-service policy is inactive until deployed and verified.

### Not implemented or not guaranteed

- This systemd network filter is **per-service**, not host-wide. It does not
  restrict other processes on a worker or monitor network flows. The worker can
  still reach permitted control-plane services; protect those services, maintain
  Tailscale ACLs, and deploy host-level monitoring separately.
- Pattern scanning catches only exact current-session-key echoes and listed
  key formats. It cannot detect arbitrary encodings, covert exfiltration,
  malicious model/tool behavior, or every provider credential format. A model
  allowed to access a secret can still reveal it in an unrecognized form.
- A model allowlist is a routing boundary, not proof that a model is safe.
Untrusted or "abliterated" models need separate approval and isolation; JEV's
API Guard review is advisory and does not inspect or authorize mission behavior.
- Prometheus/Grafana infrastructure metrics and API Guard spend data are not
  EDR, prompt-injection detection, network-flow analysis, or source-code
  exfiltration monitoring. This change does not add endpoint detection or
  continuous security analytics.
- The repository does not automatically isolate a worker host, rotate an
  upstream provider key, notify a human, or roll back leaked architecture. Those
  actions need an operator or an integrated incident-response service.

### Architecture confidentiality

Keybroker protects credentials; it does not make model input confidential. When
the configured LiteLLM route is backed by OpenAI, prompts and any architecture
details included in them are sent to OpenAI for inference. API Guard does not
inspect or classify those contents. Do not include provider secrets, SSH
material, private source code, unpublished designs, or sensitive topology in
external-model prompts unless that data transfer is explicitly approved.
Prefer a reviewed self-hosted model route for restricted designs, and keep
Forgejo credentials and repository access out of worker prompts and processes.
Check the chosen provider's retention and data-use terms before sending
proprietary architecture.

Verify the systemd egress filter on every enrolled worker, then add and test
host-level egress allowlists (Tailscale ACL plus host firewall), keep generated
code disabled or run it in a disposable isolated VM/container with no secrets,
restrict model aliases to reviewed routes, audit process/network events, and
alert on unusual egress, provider spend, key creation, and worker behavior.
These remaining host-wide controls are production hardening actions, not
current repo guarantees.

## Operator response checklist

For a worker/session exposure:

1. Stop or isolate the affected worker through the team's incident process; the
   detected run should already have stopped locally.
2. Confirm Keybroker reports the session key revoked. If it reports otherwise,
   revoke it in API Guard/LiteLLM immediately and keep the worker offline until
   its state is understood.
3. Inspect the API Guard event, worker/run identifiers, Keybroker delivery
   state, manager/NATS events, and worker journal without copying secret values
   into chat or tickets.
4. Check whether the event matched a session key. For an `unmapped` pattern,
   identify the actual credential before resolving the alert; do not revoke an
   unrelated key based on a pattern match alone.
5. For any provider/master-key suspicion, rotate that credential in the
   provider account, update LiteLLM's deployment secret, and verify provider
   spend limits and direct-call restrictions. Keybroker cannot revoke a
   provider key.
6. Preserve relevant logs, assess whether the worker's filesystem or Forgejo
   access was reached, and restore the host from a trusted state if compromise
   is plausible.
7. Resolve the API Guard alert only after revocation/rotation, scope review,
   and owner acknowledgement are complete.

## Rollout checks

- Deploy Keybroker, API Guard, and Agent Manager with distinct random admin,
  proxy, inbound-exposure, alert-delivery, and acknowledgment secrets. Match
  `API_GUARD_ALERT_WEBHOOK_SECRET` and `API_GUARD_ALERT_ACK_SECRET` between API
  Guard and Agent Manager; never reuse the inbound exposure secret.
- Configure Agent Manager's `NOTIFICATION_WEBHOOK_URL` and token to reach a real
  monitored pager/Forge notification relay. The Agent Manager inbox is durable
  but is not itself 24/7 on-call coverage.
- Verify the signed Tailnet-only `/keybroker/healthz` and worker-identity routes.
  API Guard's readiness check must report the Forge receiver, external notifier,
  and acknowledgment callback as configured.
- Exercise one exposure event end to end: Keybroker revokes the exact session,
  Agent Manager stores and delivers it, a human acknowledges in Forge, and the
  signed callback marks it acknowledged in API Guard. Also test failed delivery
  retries and the pager's after-hours/no-ack escalation.
- Run Ansible against one worker first. Verify
  `systemctl status dark-factory-agent-worker`, complete a low-budget model run,
  confirm session revocation and denied public/DNS egress, then revoke its legacy
  LiteLLM key. Repeat for all six hosts; inventory entries alone do not prove
  that a host has the worker service or egress boundary installed.
