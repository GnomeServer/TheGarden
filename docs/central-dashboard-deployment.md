# Central dashboard deployment

Editing this checkout does **not** update the running manager. The VM builds its
manager from `~/agent-manager-service`; Caddy reads `~/caddy-service/Caddyfile`.
A `/dashboard` 404 can therefore mean an old manager image or an inactive/missing
Caddy route, even when the repository already contains the dashboard.

## Prerequisites and scope

- Arrange VM access/source transfer separately. Establish any SSH session outside
  this helper; **the helper never runs SSH or deploys remotely**. Apply only in a
  local VM shell as `infra-lab-user`, with non-interactive `sudo -n docker` already
  authorized. Do not run the helper itself under sudo or on the developer stack.
- Review the checkout first. Python 3.10+ and Docker Compose v2 with JSON `config`
  output are required. Defaults target the existing service directories above;
  `--source`, `--live-dir`, `--caddyfile`, and `--backup-root` support explicit paths.
- The live `.env`, Compose file, manager source, manager/PostgreSQL/NATS containers,
  and `caddy` container must already exist. PostgreSQL/NATS must be running and
  healthy if healthchecks are defined. Existing data volumes must be exactly
  `agent-manager_postgres_data` and `agent-manager_nats_data`. The existing networks
  must include `agent_manager_internal`, `caddy_proxy`, and `open-webui_internal`.
  There is no database/bootstrap/topology initialization path.
- Pause **all** submitters (dashboard, API clients, Open WebUI and webhooks), and
  drain or pause worker execution before apply. The manager is stopped for a
  consistent database backup and image rebuild. The flag is an operator
  acknowledgement, not a mechanism for automatically pausing clients/workers.
- Workers need the **existing** private Tailscale TCP 4222 publication. This helper
  never enables it or restarts NATS. If it is absent, separately review and opt in
  to `compose.worker-test.yml` with the correct Tailscale bind address before a
  worker rollout; never expose unauthenticated NATS on all interfaces/public LAN.
- Keep sufficient space for the old source, image archive and database dump in
  `~/central-dashboard-backups` (0700). Backups contain secrets; keep them private,
  retain them through acceptance, and copy them only to approved protected storage.

## Plan, then apply

From the reviewed checkout **on the VM**, run this single read-only plan command:

```bash
python3 scripts/deploy_central_dashboard.py --plan
```

`--plan` is also the default. It validates source/dashboard/static files and the
live Caddy structure, prints non-secret source/destination/steps, and reports OAuth
and credential values only as `configured`/`not-configured`. It does not contact
Docker, run sudo, generate secrets, write files, or require external credentials.
It can run against a disposable tree with a blank `.env`. Runtime identity,
volume, network and port checks are explicitly deferred until apply; a plan is
not proof that the live containers match.

After reviewing the plan and satisfying the pause/downtime prerequisites:

```bash
python3 scripts/deploy_central_dashboard.py --apply --submissions-paused
```

Apply discovers the **existing** Compose project and file list from container
labels. It accepts `compose.yml` alone or with the existing
`compose.worker-test.yml`; other override sets, ownership/identity drift, unknown
mounts/volumes, or changed published ports cause a stop before installation.
The candidate uses the current reviewed source Compose file, the original project
name, and the original live override. It checks effective live/candidate settings
and actual running volume/port bindings rather than relying on directory names.
The Docker socket is explicitly local; ambient Docker remote-context and exported
Compose credential overrides are not inherited.

The helper then:

1. Backs up source, `.env`, Compose files and Caddyfile; tags and exports the current
   manager image. It prints the private backup path and rollback image tag.
2. Stops **only** the manager, creates `pg_dump -Fc` from the existing PostgreSQL
   container, and validates the nonempty archive catalog using `pg_restore --list`.
   Catalog validation is not an isolated restoration test.
3. Replaces only the manager package/build files/base Compose file. The live NATS
   override is retained. It preserves all unrelated `.env` entries and existing
   nonempty credentials, generates independent `DASHBOARD_SESSION_SECRET` and
   `AGENT_MANAGER_OPENWEBUI_TOKEN` only when missing/empty, and writes `.env` as 0600.
4. Builds only `agent-manager` (Compose `build` does not have a `--no-deps` flag and
   does not build dependencies unless requested), then runs
   `up -d --no-deps --no-build agent-manager`. PostgreSQL, NATS, exporters, Forgejo,
   Grafana and Open WebUI are not recreated or restarted.
5. Requires `/readyz` to report `ok`, `/dashboard` to return HTTP 200, and Caddy to
   reach the manager upstream before modifying/reloading Caddy. It inserts a
   named snippet/import into the single explicit central hostname block, retaining
   all existing route bytes, including the Open WebUI `:8443` block. An already
   matching manager route is supported without rewriting it. It validates inside
   the running Caddy container before reload and repeats manager/upstream checks.

The required settings are the HTTPS hostname origin
`DASHBOARD_PUBLIC_URL=https://infra-lab-services.tail494f6d.ts.net`,
`DASHBOARD_SESSION_SECURE=true`, `PROMETHEUS_URL=http://prometheus:9090`, and
`GRAFANA_NODE_DASHBOARD_UID=server-overview`. Empty/missing settings are filled;
conflicting existing values stop the deployment rather than silently overwriting
operator configuration. `--public-url` allows another explicitly reviewed HTTPS
hostname origin with a matching Caddy host block.

Forgejo OAuth client ID/secret and dashboard administrator allowlist remain
operator-configured in the live `.env`. Keep bootstrap administrator credentials
out of browsers/end-user configuration. The generated Open WebUI credential is
independent and run-only; install/configure the Open WebUI Pipe/workflow separately
as an administrator, using its integration runbook. This helper never installs a
workflow, grants user access, seeds backlog tasks, or changes Grafana provisioning.
## Connect existing Forgejo, Grafana, and Open WebUI

The deployment helper installs manager source and Caddy routing only. Before
resuming routine work, configure a separate Forgejo OAuth2 application with
callback `https://infra-lab-services.tail494f6d.ts.net/auth/forgejo/callback`
and set `FORGEJO_OAUTH_CLIENT_ID`, `FORGEJO_OAUTH_CLIENT_SECRET`, and the intended
individual logins in `DASHBOARD_ADMINS` in the private VM `.env`. Create an
organization or per-repository JSON webhook pointing to
`https://infra-lab-services.tail494f6d.ts.net/v1/webhooks/forgejo`, enable push,
create/delete, pull request, issue, and release events, and set the same private
`FORGEJO_WEBHOOK_SECRET` on both sides. After changing the `.env`, recreate
**only** `agent-manager` using the reviewed Compose project and existing
override; never paste those secrets into a shell command, issue, or chat.
Sign in as each real Forgejo user and push a reviewed test commit; verify one
new activity event under that account. A missing webhook secret means Git
activity ingestion remains disabled, even when OAuth login works.

The Nodes view reads the existing Prometheus at `http://prometheus:9090`; it
links the existing Grafana `server-overview` dashboard using its `host` label.
Before calling it synchronized, verify Prometheus targets for
`agent-manager:8000`, `nats-exporter:7777`, the VM node exporter and each
approved worker node exporter; verify their `up` series and each node's `host`
label. Open Nodes → Open Grafana and one worker's per-host link in a browser
with an **individual** Grafana session. The helper does not start Prometheus,
install dashboards, create Grafana accounts, or change existing scrape targets.
Missing exporters or credentials show degraded/missing data; links alone do not
establish metric coverage.

For chat-driven run submission, follow the
[Open WebUI Pipe installation and synthetic smoke](../Docker-Documents/open_webui_services/README.md#agent-manager-coder-function-manual-opt-in).
Use the independent run-only manager token in administrator Valves, not the
bootstrap API token or a per-user browser credential. The function has to be
installed in the **existing** Open WebUI database; repository files alone do
not change that instance. Verify submission, worker ID, queued → running →
completed state, return code, and the same run in the dashboard. Generated
code is not sandboxed; do not enable the Pipe for normal users or production
workers until the execution boundary is hardened. The current worker only
implements `create-python-script`, not a general autonomous task/review loop.


## Conservative stops and recovery

The helper rejects ambiguous/conflicting manager routes, hidden host/top-level
Caddy imports, regex path matchers, nested manager routes, duplicate dotenv keys,
multiline dotenv values and other unsupported structure rather than guessing.
Reconcile those cases privately/manually, then plan again. Normal unrelated routes
are left byte-for-byte intact. A Caddyfile changed by another operator during the
rollout is not overwritten. Avoid concurrent deployments or live configuration
edits during the maintenance window.

On Caddy validation/reload failure, the original bytes are restored **in place**
(the container bind-mounts that inode), then the original file is reloaded. If that
recovery command fails, the helper stops; the operator must inspect Caddy before
resuming traffic. Child stdout/stderr are withheld because Compose/build errors
can contain secrets. Inspect detailed local diagnostics privately; do not paste
raw Compose configuration, environment values or logs into issues/chat.

Any other apply failure stops the rollout; the manager can remain stopped or on
the new image. **No database is automatically restored, and no `down -v` is ever
run.** Keep submissions paused. The reported backup's `recovery.json` records the
original project, Compose files, live locations, image ID and saved image tag.
Before a manager rollback:

- Establish whether the new manager started and ran database migrations. Do not
  assume an old image is compatible with the new schema. Review compatibility and
  choose recovery deliberately; a blind database restore could discard new work.
- With the manager stopped, restore its backed-up package, build/Compose files and
  `.env` (0600). Preserve the original project and override list in `recovery.json`.
  Restore Caddy bytes in place if needed, validate in its container, then reload.
- The previous image remains tagged as recorded; `manager-image.tar` provides an
  offline image backup. Select that saved image for the manager only and recreate
  with the original project/files and `up -d --no-deps --no-build agent-manager`.
  Do not rebuild the rollback image, recreate dependencies or remove data volumes.
- Use `agent-manager.dump` only through a separately reviewed recovery procedure
  after testing restoration into an isolated database. An early failed backup can
  be incomplete; verify the recovery inventory and archive before relying on it.

After success, use a browser trusting the central TLS CA to check
`https://infra-lab-services.tail494f6d.ts.net/dashboard`, individual Forgejo OAuth
login, worker visibility, Grafana links (`server-overview`, `var-host`), and the
unchanged `/forgejo`, `/grafana`, and `:8443` services. Automated checks cover the
manager and Caddy upstream/reload, **not** external browser TLS/login. Only then
resume submissions/workers. Repository preparation or a local test is not proof
of a deployment to the central VM.

Deployment-specific unit tests are runnable separately with
`python3 -m unittest discover -s scripts/tests -p 'test_deploy_central_dashboard.py'`.
