# Short-lived LiteLLM key smoke-test implementation plan

**Status:** Plan only; not executed.  
**Current gate:** **Blocked / NO-GO.** No owner-approved compatible feature commit or isolated test services have been supplied.  
**Safety:** This plan does not authorize setup or execution. Do not clone, build, configure, or start a worker until the compatibility and isolation gates pass.

## Current branch assessment

The local checkout is `factory-testing`; local cached ref `origin/VaultGuard` points to `51b40e226de59a1336dc31394ce779fd3c29edd9`. No `git fetch` was performed, so that ref may not be the current upstream tip. The local diff from `origin/factory-testing` to `origin/VaultGuard` reports 57 changed files, 793 insertions, and 9,184 deletions.

`origin/VaultGuard` has a Keybroker flow: `KEYBROKER_URL`, `POST /v1/sessions` with `run_id` and `operation`, a one-time response key/model endpoint, and `DELETE /v1/sessions/{session_id}` in worker cleanup. The raw session key is held in process memory in that branch. These facts establish lease-related code, not readiness for this smoke test.

The candidate is **not a compatible drop-in** for the current worker:

- It replaces the current OMP-container mission execution with direct `mission.py` model calls, deletes the OMP image entrypoint, and removes current artifact/credential test suites and Manager artifact/incident features. TC-01/03 require the specified container cleanup; TC-04 requires the compatible failure/artifact path; TC-06 explicitly requires bounded OMP-container cleanup. These are not equivalent to the candidate's in-process mission path.
- Its Manager documents that cancellation publishes `agent.runs.cancelled` but nothing consumes the event; the candidate worker has no cancellation-event consumer. TC-05 cannot pass on this candidate.
- Its worker removes explicit HTTPS-scheme validation for Keybroker and no longer checks `GET /v1/worker/identity` before minting. It removes `MODEL_CA_FILE` support from Keybroker requests; its README instead requires installing the private Caddy CA into the system trust store. The worker has `MODEL_TLS_INSECURE`; it must remain `0`.
- The worker's default `KEYBROKER_URL` and broker's default model URL are production-facing. Candidate Keybroker defaults are model `luna`, a `$25` per-session budget, a 3,600-second TTL, and a 60-second reaper. These are neither isolated test endpoints nor suitably short test expiry settings.
- The candidate's Manager Compose/test override does not provision separate test NATS/Manager state. Its `compose.worker-test.yml` is only a NATS bind-address override.

**Required branch decision:** obtain an owner-approved feature commit compatible with the current worker/Manager contracts and the listed smoke cases. If the owner instead intends `origin/VaultGuard`, the owner must provide a reviewed integration commit that restores the required OMP, cancellation, artifact, timeout, HTTPS/CA, and identity-check behavior—or explicitly authorize a broader migration and revise the smoke-test contract without dropping required cases. Do not silently treat unsupported cases as passed.

## 1. Owner inputs and why each is required

Provide endpoint names, commit IDs, and protected credential references only; never place secret values in reports or chat.

1. **Approved compatible branch/commit and API contract.** Exact commit; documented broker request/response; session scope, model/budget limits, expiry, revocation, and cleanup behavior. Confirm required OMP-container behavior, cancellation, bounded timeout, artifact handling, HTTPS/CA validation, and worker identity preflight.  
   **Why:** a key-lease implementation on a branch that removes the current worker behavior would exercise a different system. Exact revisions also prevent worker/broker API mismatch and make results reproducible.
2. **Dedicated test NATS URL and credentials.** Owner confirms the host is non-production and the isolated JetStream server has stream `AGENT_RUNS` with subject `agent.runs.created`. Supply a protected credential reference, not the value.  
   **Why:** the worker uses fixed stream/subject names. A new durable consumer on production can still receive and acknowledge production missions; consumer names do not isolate data or effects.
3. **Test Manager URL and credentials.** Provide readiness, submission, status, and cancellation paths; confirm the Manager publishes only to the test NATS and uses separate test state. Identify the Manager commit/version compatible with the worker.  
   **Why:** Manager run records, queue publication, cancellation, and completion state must stay test-only. TC-05 needs a functioning Manager/worker cancellation path, which the candidate branch does not have.
4. **Test Keybroker and identity path.** Provide the test broker URL, test-only Caddy route, source-IP-to-test-worker-ID mapping, and protected reference for the proxy token. Confirm `GET /v1/worker/identity` returns the test worker identity.  
   **Why:** the candidate Keybroker trusts Caddy-injected `X-Darkfactory-Client-IP` and `X-Darkfactory-Proxy-Token`; it maps source IP to worker ID and does not accept a worker-supplied ID. A production route or mapping could mint under a real worker identity. The proxy token and broker's LiteLLM master/dashboard tokens are server-side secrets, not worker credentials.
5. **Test model gateway and CA.** Provide the test LiteLLM URL/model alias, a trusted CA path or verified system trust installation, confirmation of a dedicated test instance/database or explicitly approved test-only namespace, and the broker-side model URL. Set `MODEL_TLS_INSECURE=0`.  
   **Why:** the broker response supplies the model endpoint, and the candidate defaults point to production. Correct TLS prevents interception of the one-time key and mission data. Restore an HTTPS-only `KEYBROKER_URL` guard in the approved code. Since this candidate has no explicit CA-file setting for Keybroker requests, install the test root into system trust or restore a supported CA-file setting; keep `MODEL_TLS_INSECURE=0`.
6. **Harmless mission, limits, expiry, authority, and optional exposure fixture.** Provide a harmless mission, small test budget/rate limit, short test TTL, clock-skew allowance, named key inspector/revoker, and—if TC-09 is included—an approved synthetic canary, isolated API Guard endpoint, and protected test webhook-secret reference.  
   **Why:** candidate defaults are `$25` and one hour, with a 60-second reaper. Low limits bound cost; TTL, reaper interval, and skew determine expiry timing. A duplicate `(worker_id, run_id)` mint returns `409` and may revoke the existing session, so use a fresh run ID per case. TC-09 must not send alerts to production or implicate a real key.

If any input is missing, ambiguous, or production-facing, stop before preparation.

## 2. Provision isolated test services

1. Provision a **separate** test NATS server, JetStream store, credentials, and `AGENT_RUNS` stream with `agent.runs.created`. Restrict network reachability; use NATS TLS/authentication or a strictly isolated private test network. Do not reuse production NATS data, host, credentials, or stream, and never bind an unauthenticated test listener publicly.
2. Provision a test Manager with a separate database and credentials, configured to publish only to that test NATS. Verify readiness and test-only submit/status/cancel behavior. Confirm cancellation is consumed and reaches a terminal `cancelled` state.
3. Provision a separate test Keybroker and database. Configure its Caddy route to overwrite/inject the trusted client-IP and proxy-token headers; map only the test worker source IP to a unique test identity. Keep its master key, dashboard token, and proxy token on the server/proxy side. If TC-09 is included, connect only to a separate test API Guard with its own webhook secret; never route test exposure events into production. Do not rely on a new durable name as isolation.
4. Provision a separate test LiteLLM instance/database or an explicitly owner-approved test namespace. Configure the test broker's model URL, allowed model, low budget/rate limits, and short session TTL. Never use the candidate branch's production URL/default values for the pilot.
5. Install the test Caddy root certificate into the approved trust store, or use the compatible branch's explicit CA-file setting. Require HTTPS for broker/gateway traffic and keep TLS verification enabled.

**Gate A — service isolation:** the owner/operator independently confirms every NATS, Manager, Keybroker, Caddy, and model endpoint is test-only. Verify the test Manager publishes only to test NATS. Call the test broker's identity endpoint without minting and confirm the returned identity is the designated test worker. If the selected worker commit lacks identity preflight or HTTPS enforcement, do not proceed until an approved fix is present.

## 3. Prepare the isolated worker only after Gate A

1. Use only `~/agent-worker-shortkey-test` for the checkout, virtual environment, build context, and test environment. Keep runtime workspace and transient files under `~/agent-workspace-shortkey-test`. Preserve `~/agent-worker`, its `worker.env`, any existing service, and production credentials.
2. Check out the exact owner-approved compatible commit. Confirm it contains the lease client and broker contract **and** retains the OMP/cancellation/artifact/TLS behavior required by the smoke cases. The cached `origin/VaultGuard` candidate is not sufficient as-is.
3. Create a private virtual environment, install that commit's worker dependencies, and build the unique OMP image from that commit's image context. Record commit and image tag only. If the OMP image/context is absent, stop; do not substitute direct in-process mission execution.
4. Create a test-only environment file with mode `0600`. Set explicit test NATS and `KEYBROKER_URL`; never rely on a branch default. Set `ALLOW_GENERATED_CODE=0` and `MODEL_TLS_INSECURE=0`. Do not set `MODEL_API_KEY` or put a raw session key, LiteLLM master key, dashboard token, or Caddy proxy token in the worker environment. If the approved OMP handoff uses an ephemeral protected file, require mode `0600` and cleanup after each mission.
5. Run the worker in the foreground only. Do not install/modify systemd or run the production Ansible worker playbook.

**Gate B — worker isolation:** verify the test process has only test endpoints and the designated test identity; no production worker key/master key; and the host is not in production `agent_workers`. Confirm the worker and its image use the owner-approved commit.

## 4. Run cases in order

Record case ID, time, branch/commit, non-secret endpoint labels, mission ID, worker ID, safe session/key ID or alias, issued expiry, result, and sanitized evidence source. Never record a raw key, credential, or unredacted log. Save reports under `sam-report/`; keep test runtime files in the dedicated test directories.

| Case | Procedure and branch-specific gate |
|---|---|
| TC-00 — Preflight/isolation | Verify test NATS/stream, Manager readiness and test-only publishing, Keybroker identity response, test gateway/CA, no production worker inventory/key/master credential. **Any uncertain endpoint stops the run.** |
| TC-01 — Successful lease | Submit the harmless mission through test Manager. Verify one session for the fresh run ID, returned model/limits/expiry, successful model call, and revocation during cleanup. The supplied case expects OMP-container cleanup; an in-process mission runner is not an equivalent pass. |
| TC-02 — Model/quota scope | Use a separate disposable test session; attempt one disallowed model and exercise only explicitly configured test budget/rate limits. Verify server-side rejection. |
| TC-03 — Normal revocation | Inspect safe Keybroker/LiteLLM test metadata after TC-01; verify revoked status and a post-revocation request rejection. Container deletion alone is not proof. |
| TC-04 — Mission failure cleanup | Use an owner-approved failure hook after session issuance. Verify visible failure, cleanup path, broker revocation, and no secret in logs/artifacts. |
| TC-05 — Cancellation cleanup | Start a harmless mission, confirm session issuance, then cancel through test Manager. Require the worker to consume the cancel event, stop the OMP container, report `cancelled`, and revoke the session. Candidate `origin/VaultGuard` cannot pass: its docs say cancellation is published but not consumed. |
| TC-06 — Timeout cleanup | Run only with an approved shortened OMP timeout/test hook. Verify container kill/removal and revocation. Candidate `origin/VaultGuard` has no OMP mission container or 660-second OMP timeout; its `SCRIPT_TIMEOUT` applies to the unsandboxed script operation, which must stay disabled. Do not substitute that path. |
| TC-07 — Server-side expiry fallback | Ask the test broker for a separate disposable key with short test TTL and do not revoke it. Wait for TTL plus the configured reaper interval and clock-skew allowance; verify the test gateway rejects it. Candidate defaults (3,600-second TTL/60-second reaper) are not suitable without explicit test overrides. |
| TC-08 — Secret persistence/exposure | Confirm no static `MODEL_API_KEY`; no raw session key in persistent files, env, logs, NATS, artifacts, or dashboard. If the approved container handoff uses a temporary secret file, verify it is protected and gone after cleanup. Confirm dashboard state shows safe ID, model, expiry, and revoked/expired state only. |
| TC-09 — Dashboard exposure detection (separate) | Use only an owner-approved synthetic canary/test fixture and test API Guard/Keybroker. Verify safe alert identity, manually revoke through the test broker, and confirm rejection. Keep remediation human-operated. |
| TC-10 — Production isolation | Confirm the worker subscribed only to test NATS and the mission exists only in test Manager; review redacted logs for production connections. If isolation cannot be proven, stop and do not repeat. |

## 5. Stop, cleanup, and acceptance

- If a required endpoint or owner approval is uncertain, stop before worker startup.
- If revocation fails, leave server-side expiry enabled, notify the owner, mark the case failed, and do not report an overall pass.
- Stop the foreground worker with Ctrl-C. Do not stop/restart production workers or alter production services.
- Remove test secrets and transient files only through the approved test cleanup procedure; do not remove pre-existing worker files.
- Overall pass requires a compatible approved commit, TC-00/TC-10 isolation proof, successful lease/scope/revocation, failure and cancellation cleanup, server-side expiry independent of worker cleanup, and TC-08 secret hygiene. TC-06 remains conditional only as stated in the supplied procedure; unsupported TC-05 is a blocker, not a pass.
- Final report contains branch/commit, non-secret test endpoint labels, sanitized outcomes, mission/worker/safe key IDs, expiry/revocation results, dashboard visibility, failures, and production-isolation evidence. Exclude all key material and admin/master credentials.

This plan follows `TheGarden-short-lived-key-worker-smoke-test.txt` and the locally cached candidate branch contract; it does not authorize use of any production service or replace owner approval.
