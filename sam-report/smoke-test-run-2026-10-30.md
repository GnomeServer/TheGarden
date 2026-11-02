# Isolated short-lived-key smoke test — run report

**Date:** 2026-10-30  
**Disposition:** **NO-GO for the live smoke test; offline dummy-backed suites pass.**  
**Scope:** The current branch was approved by the user. Installed worker and Keybroker test dependencies in isolated virtual environments and ran local suites with dummy credentials/mocks. No live services or worker were started because the host has no container runtime and no isolated service endpoints were supplied.

## Current checkout

- Branch: `sam-smoke-test`
- HEAD: `cf2eb346dcff0479486e2b4aa668df9cfb9ceacd` (`Add worker onboarding and dashboard controls`)
- Before this report was created, `git status --short --branch` showed only the untracked supplied procedure and `sam-report/`; no tracked files were modified during this run.
- The existing `~/agent-worker` path is present. `~/agent-workspace-shortkey-test` remains absent. This run created `~/agent-worker-shortkey-test/.venv` and `~/agent-worker-shortkey-test/.venv-broker`; no source checkout or runtime files were created. Secret-bearing files were not read.

The prior intake and plan describe the earlier `factory-testing` checkout and cached `origin/VaultGuard` candidate. Those are historical assessments, not the current branch assessment. The current branch's `agent-worker/worker.py` includes the lease request/revocation flow, HTTPS enforcement and verified TLS context, worker-identity preflight, OMP-container launch, and a cancellation-event subscription. The user has explicitly approved this exact branch/commit for the smoke test. Branch approval does **not** establish test-environment isolation.

## Isolation gate

**Gate A did not pass.** No test-only NATS, Manager, Keybroker, or model-gateway endpoints, protected credential references, confirmed broker identity, CA/trust path, test policy, harmless mission, or authorized test-key inspector/revoker were supplied or verified. Rechecked after the live-test request: Docker, Podman, nerdctl, containerd, NATS server, and Caddy remain unavailable; `/var/run/docker.sock` is absent. The current user is uid 1000 and noninteractive `sudo` is unavailable, so installing/enabling Docker Engine requires administrator authentication. The worker must launch an OMP container for the specified mission path. The checked-in Compose files are not safe substitutes: they reference external `caddy_proxy`/other networks, fixed persistent volumes, production-facing defaults, and the Manager service mounts the host Docker socket.

Consequently, TC-00 isolation proof is unavailable and TC-01 through TC-10 were **not run**. I did not probe any service endpoint or start a worker, OMP container, or service against defaults. No mission was submitted. The Python dependencies are installed in isolated venvs and the offline suites passed. No production service or credential was accessed or changed.

## Rootless Docker access attempt

The current user is uid `1000`, with `XDG_RUNTIME_DIR=/run/user/1000`. `/etc/subuid` and `/etc/subgid` each grant `dow_admin` 65,536 subordinate IDs; `iptables` is installed and unprivileged user namespaces are enabled. On the first attempt, the installer stopped because `newuidmap` was missing. After `uidmap` became available, I reran the official rootless installer with `DOCKER_BIN` set to the dedicated test directory. It downloaded Docker `29.9.2` and rootless extras there, but setup failed at RootlessKit: `fork/exec /proc/self/exe: permission denied`. The verified setting is `kernel.apparmor_restrict_unprivileged_userns=1`.

The installer did not create a user Docker service or context. On the verification retry, `docker --version` and `dockerd --version` reported `29.9.2`, `rootlesskit --version` reported `3.3.0`, and `dockerd-rootless-setuptool.sh check` said requirements are satisfied. However, `systemctl --user` reports `docker.service` not found/inactive, `/run/user/1000/docker.sock` is absent, and `docker info` cannot connect. The AppArmor exception and sudoers drop-in are still absent. Docker binaries are installed; the rootless daemon is **not operational**.

```sh
sudo tee /etc/apparmor.d/home.dow_admin.agent-worker-shortkey-test.docker.bin.rootlesskit >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

/home/dow_admin/agent-worker-shortkey-test/docker/bin/rootlesskit flags=(unconfined) {
  userns,
  include if exists <local/home.dow_admin.agent-worker-shortkey-test.docker.bin.rootlesskit>
}
EOF
sudo systemctl restart apparmor.service
```

That system-wide policy change requires a human administrator; it permits this specific `rootlesskit` binary to create user namespaces. I did not bypass AppArmor with `--force` or disable the restriction globally. After an administrator applies/reviews the rule, I can run the installed `dockerd-rootless-setuptool.sh install`, start the user service, and verify `docker info`. See [Docker rootless mode](https://docs.docker.com/engine/security/rootless/).

The supplied `enable-rootlesskit-sudo.sh` initially contained literal Markdown fences and trailing whitespace on here-document terminators. I removed those wrappers/whitespace, changed its mode from `775` to `750`, and verified `bash -n` passes. Inspection found `/usr/local/sbin/enable-test-rootlesskit-apparmor` as a root-owned mode-`0750` helper; the sudoers drop-in and AppArmor profile are absent. I updated the bootstrap to accept that helper only if it is byte-for-byte identical to the expected root-owned mode-`0750` content, or remove the exact known malformed prior helper by hash; it refuses all other existing files. The latest bootstrap is syntax-valid. A human must rerun it with interactive administrator authentication; `sudo -n` cannot run in this session.
The subsequent reported line 76 error came from a backslash followed by trailing spaces before `printf`, which ended the command and caused the sudoers rule to be executed as a command. I replaced the continuation with a single-line `printf`. `bash -n` passes and no trailing-space continuations remain. The privileged bootstrap has not been rerun in this session.


## Offline check

The worker credential/lease and cancellation unit suites passed using the isolated venv:

```text
test_worker_credentials.py  10 tests, OK
test_worker_execution.py     7 tests, OK
```

The Keybroker test suite passed using a separate Python 3.12 venv:

```text
18 passed, 1 deprecation warning
```

Keybroker tests use a temporary SQLite database, a stubbed LiteLLM admin API, and dummy proxy/dashboard/master/webhook values from the test fixture. Worker tests mock broker, NATS, and Docker interactions. These results exercise local code paths only; no live virtual key was issued by LiteLLM and no service isolation was proved.

The first Keybroker dependency install under Python 3.14 could not resolve the pinned `psycopg-binary==3.2.3`; Python 3.12 was available, and the full pinned test requirements installed successfully in `.venv-broker`. No system Python packages were changed.

## Dummy credential feasibility and remaining requirements

The repository's dummy fixture values are sufficient for the Keybroker unit tests, not for authenticating to a live service. For a fully local stack, fresh random test-only values can be generated for database passwords, Manager bearer auth, LiteLLM master/salt, Keybroker dashboard/proxy/webhook secrets, and a mock model-backend token. A mock model server can accept its own dummy token, so no paid-provider key is required. The worker must receive only a session key minted by the test Keybroker, never the LiteLLM master/dashboard/proxy secrets. No `.env` was created because there is no local service runtime to consume it.

To continue with a local live stack:

1. A host administrator must install/enable Docker Engine and grant the isolated test runner access to its daemon/socket. This is required to build and run the OMP image; this host currently has no container CLI or daemon socket.
2. Use a dedicated Compose project with loopback-only published ports, unique test volumes/networks, no production external networks, and no production URLs. Do not use the checked-in Compose files unchanged: the Manager Compose mounts `/var/run/docker.sock`, while NATS defaults to a Tailscale bind and service configs include production-facing defaults.
3. Configure only a test worker identity in Keybroker's source-IP map and a local test-only Caddy route that injects the dummy proxy token. Set an explicit local model URL, CA/trust path, low budget, and short TTL/reaper interval. Keep the test model/mock backend local.

If the test must use remotely hosted services instead of a local mock stack, dummy values will be rejected. A service owner/admin must provision the isolated NATS/Manager/Keybroker/LiteLLM resources, create their credentials through the respective admin interfaces, map the test worker identity, configure the test Caddy route and trusted CA, and provide protected credential references. If a real external model provider is used, its owner must issue/authorize a test API key and spending limit; it cannot be synthesized locally.

The branch approval was provided; no test-service endpoint, credential reference, CA, or service-administrator provisioning was provided. Do not start a worker until the host runtime is available and TC-00 proves every endpoint is test-only.

Preserve `~/agent-worker`, its credentials, and production services. Do not interpret these offline test results as a live smoke-test pass.

## Evidence limits

No live service readiness, endpoint ownership, NATS stream contents, Manager isolation, broker identity, CA validity, production inventory membership, or runtime network isolation was verified. Do not interpret this preflight report as a smoke-test pass.