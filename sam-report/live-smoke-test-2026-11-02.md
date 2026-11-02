# Live short-lived-key smoke test — 2026-11-02

**Branch:** `sam-smoke-test`  
**Commit:** `cf2eb346dcff0479486e2b4aa668df9cfb9ceacd`  
**Approval:** User-approved for this test.  
**Status:** **PASS for the isolated rootless smoke harness, with the runtime adaptation below; not a rootful production-deployment validation.**

## Runtime and isolation

- Rootless Docker Engine `29.9.2` is active for `dow_admin`; `docker info` reports the `rootless` security option. The host-specific AppArmor profile and narrowly scoped sudo helper were installed after administrator authorization.
- The branch OMP image was built as `local/omp-worker-shortkey-test:cf2eb346`; container smoke returned `omp/18.12.5`.
- The test stack uses generated test-only database, Manager, Keybroker, LiteLLM, and proxy credentials. The supplied OpenAI API key is stored in the mode-`0600` test `.env` and is passed only to LiteLLM; the worker received no OpenAI or admin key.
- Manager, NATS, Keybroker, LiteLLM, Caddy, and an allowlisted OpenAI CONNECT proxy run in the separate rootless Docker engine. The OMP container is attached only to internal network `shortkey-test-internal`; a test-only Docker CLI shim maps the worker's hard-coded `bridge` network to that network and container user `1000` to rootless UID `0` for bind-mount access. Rootless UID `0` maps to the unprivileged host user. This harness adaptation is not equivalent to the unmodified rootful Docker invocation.
- The worker's Manager URL is hard-coded to `infra-lab-services.tail494f6d.ts.net`. A loopback-only CONNECT proxy accepts only that hostname and forwards to local test Caddy. Caddy-worker handles Keybroker/Manager routes; Caddy-model handles LiteLLM. The OMP network cannot reach Caddy-worker; direct unauthenticated Keybroker identity access returned 403. OpenAI egress is only through the separate proxy, which allows `api.openai.com:443` and requires proxy auth; OMP's unauthenticated attempt received 407.
- Local preflight succeeded: Manager `/readyz` reported database/NATS `ok`; test Keybroker identity returned `worker-shortkey-test`; OpenAI's configured model was listed and LiteLLM chat returned HTTP 200. The local test stack made no production-service requests.

## Live cases observed

- **TC-00 — local preflight/isolation:** local Manager, NATS, Keybroker identity, TLS trust, and service boundaries verified. The production-looking Manager hostname is intercepted by the loopback-only test proxy; no production service was contacted.
- **TC-01 — successful OMP mission:** **pass.** Manager run `5f17978b-46fd-4d65-ab1d-96319f4f8f5d` completed on `worker-shortkey-test` using OpenAI model `gpt-4.1-mini`. Keybroker issued safe key ID `e65ba403a3c8046e83de84cbf7c3d146e588550b060b0aee3a0dd0f599c8b2d6` (alias `session-worker-shortkey-test-45efd5df-6cf`, expiry `2026-11-02T17:44:52Z`); it was revoked at `2026-11-02T16:45:26Z`. Manager stored artifact `artifacts.zip` (276 bytes, SHA-256 `445f7d3f16e91cd6ada28c7397b911550a2407cec93c228ee8f30a142df93604`); its manifest listed `smoke.txt`, whose content matched `short-key-smoke-ok`.
- **TC-02/03 — model scope and revocation:** an OpenAI-backed live Keybroker session called allowed `luna` successfully (HTTP 200; safe key ID `1050b3e3d90028485e32db088147ebd892b9e498ceeeb00d27c781430b6191dd`), a disallowed model was rejected (HTTP 403), and after revocation LiteLLM rejected the key (HTTP 401). No raw key was recorded.
- **TC-04 — mission failure cleanup:** earlier control-route and local-model failures were visible; each checked session was revoked and each OMP container was removed.
- **TC-05 — cancellation cleanup:** local Manager cancellation after session issuance returned `cancelled`; Keybroker revoked the session and the OMP container was removed.

## Timeout, expiry, and final checks

**TC-06:** the OMP image's configured 600-second timeout was reached; the failure was visible, the container was removed, and the session was revoked. **TC-07 passed with OpenAI configured:** a test key with 10-second TTL and 1-second reaper (safe key ID `8176b66ba4a2cd8949a77b40e6421a821ed7600b09a29b7e9cf61ee308004e66`) expired at `2026-11-02T16:52:29Z`; LiteLLM returned HTTP 401 after expiry and the Keybroker row was revoked at `16:52:30Z`. The normal TTL/reaper policy was restored.

**TC-08 passed:** `worker.env` and the OpenAI test `.env` are both mode `0600`; `worker.env` has no static `MODEL_API_KEY` or admin/provider credentials, and the workspace is empty after OMP cleanup. Keybroker's admin session view showed safe key ID, alias/model, expiry, and revoked status without exposing the key. **TC-09** was not run (optional separate exposure fixture). **TC-10 passed for this local harness:** OMP used the internal network, could not reach Caddy-worker or unauthenticated OpenAI egress, and no production service was contacted. The worker and CONNECT proxy received SIGINT during cleanup, so the supervisor reported exit code 1 with `KeyboardInterrupt`; the successful OpenAI mission had already completed. The separate run `1e4aec21-1ede-4839-933a-53b255e207a3` failed its control-route check. Compose shutdown completed; no smoke containers remain. Rootless Docker remains active, with test volumes/config retained for reuse.

Overall: **PASS for the isolated local rootless smoke cases TC-00 through TC-08 and TC-10; TC-09 not run.** This result includes the stated rootless CLI shim and does not validate the unmodified rootful container-user path or a production deployment. LiteLLM's local global budget cap was `$1` and Keybroker's per-session cap `$0.25`; the OpenAI project-level usage limit was not inspected. Monitor provider usage before reusing the key.

## OpenAI backend used

LiteLLM was configured with `LUNA_API_BASE=https://api.openai.com/v1`, model `gpt-4.1-mini`, and `LUNA_BACKEND_API_KEY` sourced from `OPENAI_API_KEY` in the protected test `.env`. Keybroker's public model alias remained `luna`; the worker received only the short-lived Keybroker session key. Egress used an authenticated CONNECT proxy allowlisted to `api.openai.com:443`. No key value is included in this report.

