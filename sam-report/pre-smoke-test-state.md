# Pre-smoke-test machine state report

**Assessment date:** 2026-10-28  
**Decision:** **NO-GO — do not start a worker or submit a mission.**  
**Scope:** Read-only review of the supplied procedure, repository files, safe directory listings, and local Git metadata/content. No service probes, network requests, worker setup, or service changes were performed. Secret-bearing local files and deployed credential values were not read; only version-controlled example placeholders/test fixtures were inspected.

## Findings

| Area | Observed state | Smoke-test impact |
|---|---|---|
| Worker implementation | Checked-in `agent-worker/worker.py` reads `MODEL_API_KEY` from the process environment (lines 33–51), requires it for mission execution (884–888), writes it into a per-run `job.env` (950–963), and passes that file to Docker. The mission `finally` block cleans up the watcher/mission bookkeeping and checkpoint (1133–1138); the reviewed path contains no broker lease request or key-revocation call. | This checkout does not implement the required per-mission lease lifecycle. Do not use it as the short-lived-key worker. |
| Existing worker files | A safe filename-only listing found an existing `~/agent-worker/` containing `worker.env`, `start-worker.sh`, and a Python virtual environment. The contents of `worker.env` were deliberately not read. | Treat this as protected existing state. Do not overwrite it, inspect or copy its credentials, or use it as the test checkout. |
| Test workspace | `~/agent-worker-shortkey-test` and `~/agent-workspace-shortkey-test` were not present in the scoped directory listing. | The isolated worker/test workspace has not been prepared. |
| Worker inventory | Checked-in `Ansible/inventory/hosts.yml` has four hosts in `agent_workers` (inkii, naruto, raphael, donatello; lines 20–52). `Ansible/playbooks/agent-workers.yml` targets that group. | Do not add this test node to that group or run the production worker playbook against it. |
| NATS / Manager isolation | The standard Manager Compose project is named `agent-manager` and uses the named `agent-manager_nats_data` volume (`Docker-Documents/agent-manager-service/compose.yml`, lines 1, 23–47, 146–150). The checked-in `compose.worker-test.yml` only overrides the NATS bind address/port (lines 1–6); it does not define a separate test server, stream, or Manager. | This override is not evidence of isolation. Do not connect a pilot worker to that NATS or Manager. A new durable consumer alone is explicitly insufficient isolation. |
| LiteLLM / broker isolation | The current `factory-testing` checkout has persistent worker-scoped keys, not a lease broker. The cached `origin/VaultGuard` ref contains Keybroker code, but its defaults point to production and no isolated test broker/gateway was established. | A test broker and model gateway are still missing; never give the worker a master key. |
| Candidate branch compatibility | Compared with `origin/factory-testing`, cached `origin/VaultGuard` changes 57 files (+793/−9,184), replaces the OMP-container runner with direct `mission.py` calls, removes the OMP entrypoint and current artifact/credential tests, and removes Manager artifact/incident functionality. It also drops the broker HTTPS-scheme guard and worker-identity preflight; its Manager docs state cancellation is published but not consumed. | Not a drop-in for this smoke test. TC-05 cannot pass; OMP/cancellation/artifact cases and TLS/identity guarantees require a compatible owner-approved integration commit. |
| Owner prerequisites | No owner-approved compatible branch/commit or test-service inputs were supplied. The local candidate ref is `origin/VaultGuard` at `51b40e226de59a1336dc31394ce779fd3c29edd9`, but it is not approved or compatible as-is. Test endpoint names/credentials, trusted test CA, harmless mission, limits, and authorized key inspector/revoker remain missing. | The cached candidate is not authorization to use its code or production routes. Required inputs and isolation proof remain outstanding. |
| Live machine/services | No service or network probes were run. Current service activity, systemd state, installed Docker/Python versions, NATS stream contents, Manager readiness, gateway reachability, CA validity, inventory membership on the live host, and credential contents are **unverified**. | Static files cannot establish runtime isolation or readiness. Do not infer that a service is up or isolated. |

## Basis and risk

The supplied procedure identifies the checked-in worker as using persistent `MODEL_API_KEY` and requires an owner-provided lease branch plus isolated services before startup (lines 5–7, 18–25). The active local checkout is `factory-testing`; the locally cached `origin/VaultGuard` contains lease code but is a broader, incompatible change. Its worker/Manager execution and cancellation contracts differ from the supplied smoke cases, and its broker defaults are production-facing. No owner approval, test endpoints, or live readiness checks exist.

The existing `compose.worker-test.yml` is a network bind override, not an isolated NATS deployment. Applying it to the standard Manager Compose project would retain that project's NATS state and would not satisfy the procedure's dedicated test NATS/Manager requirement. A production stream can deliver jobs to another durable consumer, so consumer naming is not a safety boundary.

## Required disposition

**Stop before setup or worker startup.** Preserve `~/agent-worker`, `worker.env`, any systemd worker service, and all existing credentials. Do not run `Ansible/playbooks/agent-workers.yml`, publish to production NATS, or use production LiteLLM/Keybroker routes. Reassess only after the owner supplies a compatible approved commit and all test-only prerequisites, then TC-00 positively verifies isolation.

No deployed credential values or secret-bearing local files were read or copied into this report; inspection of version-controlled examples/test fixtures exposed placeholders only.
