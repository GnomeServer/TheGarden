# Dark Factory API Guard

API Guard is a separate web service and URL, not a section of Agent Manager's team dashboard. It authenticates its own Forgejo OAuth application, has its own administrator allowlist and session secret, keeps exposure-event state in its own SQLite volume, and calls Keybroker over the private Docker network for LiteLLM key, spend, session, and worker-roster data. Keybroker alone holds the LiteLLM master key; API Guard never receives it. The browser never receives an administrative key.

## What it does

- Shows LiteLLM rolling-365-day and current-month spend, prompt/completion/total tokens, per-key totals, budget caps, key age, and token-bearing zero-cost counts grouped by safe model ID.
- Uses the default $25,000 shared hard ceiling in `litellm-service/config.yaml`; this applies to calls through LiteLLM. The dashboard's monthly pacing target is `$25,000 / 12 = $2,083.33`, a warning target, not a second hard stop.
- Issues budgeted, model-scoped LiteLLM virtual keys through Keybroker. A new raw key is sent once to the authenticated browser with `Cache-Control: no-store`; API Guard never persists it. Keys expire after twice the rotation interval. Existing unlimited keys are not silently changed.
- Keybroker checks LiteLLM pricing metadata before issuing either session or admin keys. Unpriced external models are blocked; only explicitly allowlisted local model API bases may have zero cost.
- Flags HMAC-signed OMP exposure reports. Events may contain a safe worker/session identifier and a key alias/identifier. The schema rejects content fields and recognized credential formats; it does not claim to detect arbitrary exfiltration.
- Displays the Keybroker Tailnet source-IP allowlist, historical job sessions, and usage attributed by exact LiteLLM key ID. The allowlist is not proof that a host is online or that Ansible installed its egress boundary.
- Sends signed incidents to the separate Agent Manager Forge inbox, tracks human acknowledgments, and retries until the callback is received. Agent Manager retries any configured external notification relay; neither HTTP acceptance nor this dashboard proves 24/7 on-call coverage.
- Rotates a key by issuing a replacement with its model scope and budget through Keybroker. Legacy model-unrestricted keys are narrowed to `luna`. Routine rotation keeps the old key active until the consumer has been updated and verified. For a known exposed key, the UI offers immediate revoke after issuing the replacement; this may interrupt a consumer until its secret is updated.
- Offers JEV a sanitized, advisory-only review. JEV cannot mint/revoke keys, change budgets, or override LiteLLM policy.

The default rotation interval is 14 days (biweekly). Set `API_GUARD_ROTATION_DAYS=7` for a weekly cadence. The dashboard flags aged keys and gives an administrator a replacement workflow; it does not silently rewrite worker secret files or revoke a key before its consumer can be updated. Configure provider-account spend limits as well: LiteLLM cost estimates and provider invoices can differ.

## Install and configure

1. Create a separate Forgejo OAuth application for API Guard. Callback URL:

   ```text
   https://infra-lab-services.tail494f6d.ts.net/api-guard/auth/callback
   ```

2. Copy `.env.example` to a deployment-only `.env` with mode `0600`. Set an independent random `API_GUARD_SESSION_SECRET`, `API_GUARD_ADMINS`, the OAuth credentials, the API Guard/Keybroker `KEYBROKER_DASHBOARD_TOKEN`, and `API_GUARD_EXPOSURE_WEBHOOK_SECRET`. The dashboard token must match Keybroker's setting. The LiteLLM master key belongs only in Keybroker's environment; never configure it in API Guard or commit it.
3. Match the annual ceiling in `litellm-service/config.yaml` and `API_GUARD_ANNUAL_BUDGET_USD`. LiteLLM needs PostgreSQL for global budget enforcement. Dashboard usage is rolling-365-day; monthly spend is compared with the pacing target.
4. Share `API_GUARD_EXPOSURE_WEBHOOK_SECRET` with Keybroker so it can sign worker-session exposure events sent to `http://api-guard:8000/api-guard/api/v1/webhooks/exposure`. The existing Agent Manager OMP notifier can use that same webhook URL and secret. OMP can redact the configured key and recognized credential patterns, but this is not a scanner for arbitrary exfiltration.
5. API Guard's Compose default sends signed incident events to the separate Forge/Agent Manager receiver. Set matching, distinct `API_GUARD_ALERT_WEBHOOK_SECRET` and `API_GUARD_ALERT_ACK_SECRET` values in API Guard and Agent Manager. Never reuse `API_GUARD_EXPOSURE_WEBHOOK_SECRET`. For actual external paging, also configure Agent Manager's `NOTIFICATION_WEBHOOK_URL` and `NOTIFICATION_WEBHOOK_TOKEN` to the organization's monitored relay; no endpoint or recipient is provisioned by this repository.
6. Use a restricted LiteLLM virtual key for JEV and set `API_GUARD_JEV_API_KEY`; configure the `jev` model alias in LiteLLM first. The checked-in LiteLLM model list contains only `luna`, so JEV review stays unavailable until an operator configures a real JEV route and key. Never use the LiteLLM master key for JEV calls.

7. Start the service after Caddy, Keybroker, and LiteLLM are attached to `caddy_proxy`:

   ```bash
   cd /home/infra-lab-user/TheGarden/Docker-Documents/api-guard-dashboard
   sudo docker compose up -d --build
   ```

   Open `https://infra-lab-services.tail494f6d.ts.net/api-guard/` from a Tailscale device. Caddy rejects non-tailnet requests before they reach API Guard; Forgejo OAuth and the `API_GUARD_ADMINS` allowlist are also required.

The checked-in Caddyfile adds the route, but editing the checkout does not deploy it. The API Guard Compose project and `.env` use the existing `/home/infra-lab-user/TheGarden/Docker-Documents/api-guard-dashboard` checkout path. Reload Caddy only after the route is present in `/home/infra-lab-user/caddy-service/Caddyfile`.

## Key exposure and rotation

Worker-to-Keybroker and Keybroker-to-API Guard events use the inbound `API_GUARD_EXPOSURE_WEBHOOK_SECRET`. Outbound notifications use the separate `API_GUARD_ALERT_WEBHOOK_SECRET`. Both signatures use HMAC-SHA256 in `X-Darkfactory-Signature: sha256=<hex>`. The receiver rejects unsigned/invalid JSON, oversized payloads, and extra fields such as `key`, `token`, or `secret`. Event IDs are deduplicated. Optional worker and session IDs are safe identifiers displayed in the dashboard. API Guard stores outbound notification events before delivery and retries until a receiver returns 2xx; the event ID supports at-least-once delivery. For known key matches, the dashboard links an alert to the LiteLLM key alias/identifier. A generic detected key format is marked `unmapped`; an admin must identify the affected credential before resolution.

LiteLLM pagination is bounded to 10,000 log rows per dashboard refresh. If the proxy reports more than that, API Guard marks the displayed totals as lower bounds rather than presenting a partial result as complete.

Dollar totals are LiteLLM estimates from its model pricing data, not provider invoices. For `$0` token usage, the dashboard now lists request and token counts by sanitized LiteLLM model ID. A zero-priced name may be a genuinely local/free backend or a custom paid model missing its pricing mapping. For a paid model, set LiteLLM `model_info.base_model` to the exact priced catalog model or configure explicit provider `input_cost_per_token` and `output_cost_per_token` rates; do not guess. Keybroker now refuses to mint session/admin keys for unpriced external model routes. Existing usage remains an estimate until compared with the provider invoice.
Spend-log key hashes that do not match a LiteLLM key record are now checked
against Keybroker's rolling-365-day session history. API Guard attributes usage
only by exact, safe LiteLLM key ID; it never guesses from an alias. The worker
session panel separates active, expired, and revoked job sessions and displays
the newest 200 rows while matching spend against up to 10,000 records. Truly
unmatched spend IDs are shown by their final eight characters only. A page cap
or ambiguous key-to-session mapping is reported explicitly; incomplete history
must not be treated as confirmed attribution.

OMP redacts only key values it can positively identify: its configured model key by exact match, plus recognized OpenRouter/OpenAI/Anthropic/Google key formats. This is a safety net, not a secret scanner for arbitrary credential formats. Never place provider master keys in OMP prompts or model context. Ensure every paid request goes through LiteLLM and use separate keys per consumer. If the webhook is unavailable or unconfigured, OMP still redacts known matching values and tells the user to notify an administrator, but no dashboard event can be recorded.

Routine rotation sequence: issue replacement → copy it directly to the consumer's secret store → restart/reload that consumer → confirm successful requests → revoke the old key. For an exact current-session key exposure, Keybroker revokes that session key and stops the run before sending a safe event to API Guard. Keybroker also persists the event and retries API Guard delivery if that service is unavailable; API Guard's outbox then retries downstream notification delivery. An unassociated key-like pattern is flagged without revoking unrelated keys. Provider/master-key exposures require human owner escalation and rotation unless a separate provider-admin process is configured. The worker run result and dashboard provide in-app/admin alert paths; optional signed-webhook consumers handle downstream escalation. Unknown alerts remain flagged until the key is identified and the incident is handled.

## Notifications and on-call

Exposure reporting uses durable queues and a separate Forge incident receiver:

1. The worker stops further model calls, reports the incident to Keybroker, and
   Keybroker revokes an exact matching session key when possible.
2. Keybroker stores a secret-free event in PostgreSQL and retries API Guard
   delivery if API Guard is down.
3. API Guard stores the event and retries the signed webhook to Agent Manager.
   It remains `awaiting_ack` after HTTP acceptance and re-delivers every five
   minutes until an authenticated human acknowledgment returns.
4. Agent Manager deduplicates by `event_id`, persists the incident in its own
   database, and retries the optional `NOTIFICATION_WEBHOOK_URL` relay. An
   administrator acknowledges the incident in the Forge Agents view; the
   signed callback updates API Guard's incident state.

The Agent Manager incident inbox is an internal control path, not a pager.
24/7 coverage is not active until an external relay is configured, a primary
and backup on-call schedule exists, and after-hours delivery and acknowledgment
have been exercised. Email or the API Guard dashboard alone does not prove a
human is on call. If no external relay is configured, Forge still durably
records the incident but cannot reach a responder outside the service. If
Keybroker/PostgreSQL and API Guard are both unavailable, the worker still stops
locally and reports that central delivery could not be confirmed.


## Budget semantics

`litellm_settings.max_budget: 25000` with `budget_duration: 365d` is the shared hard limit for all requests through this LiteLLM proxy. LiteLLM must have its database available for the limit to be enforced. API Guard's `$2,083.33` monthly target indicates pacing toward the annual allowance; it does not block requests. Each newly issued key also receives a required per-key budget and reset period. Existing keys without per-key budgets remain clearly labeled and must be migrated/managed separately.
For a provider-side OpenAI comparison, optionally set `API_GUARD_OPENAI_ADMIN_API_KEY` to an organization Admin API key with **Usage Read** access, and set `API_GUARD_OPENAI_PROJECT_ID` to the project LiteLLM uses. This is separate from the project API key used for inference and remains server-side. Without a project ID, the costs panel reports organization-wide spend, which may include other workloads. See the [OpenAI Costs API](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs).

This does not limit unrelated provider keys or direct provider calls that bypass LiteLLM. Configure a hard provider-account limit and alerts at OpenRouter (or the actual upstream) as a second boundary. Confirm provider-side invoices against LiteLLM because model-price metadata can be incomplete or stale.
API Guard does not query OpenRouter or prepaid-credit balances. OpenAI costs are optional daily billing data, not a real-time balance; treat the provider invoice and provider-side spending limits as authoritative.

## JEV role

JEV reviews sanitized aggregate spend, safe key aliases/models, rotation age, and exposure count through a dedicated model-restricted virtual key. It is not trusted to classify secrets, authorize operations, or initiate rotations. The dashboard's deterministic webhook signature checks, age rules, LiteLLM budgets, and administrator confirmations remain authoritative.
