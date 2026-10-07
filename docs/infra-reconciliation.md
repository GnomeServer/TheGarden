# Infrastructure capture reconciliation and central rollout

## Status and source

The VM capture is commit `70838b3307c278ed0e803641bcdb4ef29bde32f4`, imported on
Donatello under `refs/remotes/infra-capture/live-capture-20261007-144000` from
the Git bundle in `~/Downloads`. No new bundle needs to be created.

The reconciled publication branch is
`refs/heads/infra-capture/live-capture-20261007-144000` on `origin`. It contains
the reviewed dashboard changes and records the captured VM commit as ancestry
after manual reconciliation. The original `factory-testing` and `main` branches
are not rewritten. Publishing source does not deploy it or transfer databases.

The operator has restored the VM and bare-metal host. The running VM still has
its older standalone source under `~/agent-manager-service`; that directory
is separate from both VM and Donatello repository checkouts.

## Reconciliation decisions

| Area | Kept or changed | Reason |
|---|---|---|
| Dashboard | Kept all current API/UI modules, tasks, timers, auth, activity, metrics and Grafana links | Never replace modular dashboard with the old monolithic main.py |
| Run admission | Integrated live `find_available_worker` request/reply and metadata validation | Return `503 NO_WORKER_AVAILABLE`, `Retry-After: 15`, and create no run if no eligible worker responds |
| Worker responder | Integrated `agent.workers.health.<role>` with targeted-ID filtering and execution-enabled gate | Disabled workers can heartbeat but must not advertise executable capacity |
| Worker stream | Narrowed `AGENT_WORKERS` to `agent.workers.heartbeat` | A stream on `agent.workers.>` intercepts health probes and can send storage acknowledgements ahead of real worker replies |
| Queue replay | Retained the earlier `e80c2da` change: `DeliverPolicy.ALL`, shared role consumer, `ack_wait=900` | New durable consumers replay accepted work; existing durable state remains intact |
| Heartbeats | Kept periodic and transition heartbeats, current run, version and capabilities | Health request/reply supplements rather than replaces dashboard presence |
| Shutdown | Kept the idle Ctrl+C cleanup and unsubscribe both job and health subscriptions | No idle-worker traceback or leftover connection |
| NATS exporter | Added pinned 0.20.2 with `restart: unless-stopped` | Preserve central metrics and allow recovery after host restart |
| Model gateway | Default manager model URL is `http://litellm:4000/v1` | Preserve central gateway routing; no key reuse |
| Caddy | Kept `/llama`, `/litellm`, `/forgejo`, `/grafana`, Open WebUI `:8443`, dashboard/auth/API paths and Proxmox fallback | Do not break already-instantiated services |
| Prometheus | Kept all current worker/node labels and added NATS exporter scrape | Inkii remains `worker-02`, not the capture's stale `worker-1` |
| VM node exporter | Target `node-exporter:9100` | Tracked Compose connects exporter and Prometheus to one bridge; it has no host-gateway alias. A differently networked live exporter must be checked before applying this target |
| Grafana | Continue linking existing `server-overview` with `var-host` | Do not overwrite the central Infrastructure – Server dashboard or datasource provisioning during this rollout |
| Pi provisioning | Imported defaults, model/settings templates and public Caddy CA | Current live workers still use Pi; OMP remains planned, not implemented |
| Worker playbook | One canonical `playbooks/agent-workers.yml` | Captured `configure-agent-workers.yml` duplicates the same role; callers should use the canonical path |
| Ansible controller | `infra-lab-services` is the sole local target; bare metal uses explicit SSH | Current secret source paths and worker provisioning live on the VM, not Donatello or bare metal |
| Shell profiles | Use the loop item for `.profile` and `.bashrc` | Captured task previously wrote `.profile` twice |

Worker identities stay fixed: Raphael `worker-01`, Inkii `worker-02`, Donatello
`worker-03`, Naruto `worker-04`. All are `coder`. Existing LiteLLM keys are not
activity keys or dashboard administrator credentials.

## What this does not fix

A live health response is an availability preflight, not a reservation or
fenced lease. Busy enabled workers may respond, and an accepted run can remain
queued if a worker disconnects after responding.

This capture contains queue replay and availability changes, not process locks,
exactly-once execution, active subprocess cancellation, or supervisor/planner/
reviewer implementations. Keep those backlog tasks open.

Explicit `worker_id` remains suitable only for single-worker targeted tests:
the shared pull consumer does not route a job to a particular worker. Normal
pool submissions omit worker_id and specify worker_role. Do not roll out mixed
role execution pools before implementing role-specific scheduling.

## Isolated verification performed

No production credentials, remote hosts, local developer database, or local
worker queue were used. Verification ran in a separate Docker Compose project
with disposable PostgreSQL and NATS state and test-only credentials.

Observed:

- 13 unittest regressions passed, including no-worker rejection without a DB
  mutation, invalid metadata, wrong role/ID, disabled worker, malformed health
  requests, and rejection of a JetStream storage acknowledgement as health.
- A pre-existing run table and completed row survived manager startup.
- An existing wildcard worker stream was migrated to heartbeat-only subjects.
- No worker returned 503 without creating a run; malformed metadata returned 422.
- A disabled worker appeared in presence but could not admit execution.
- Wrong target ID and wrong role returned 503.
- An enabled worker-03 completed a supplied harmless Python task, wrote the
  expected artifact, and produced one start and one completion activity entry.
- Replaying the same completion did not duplicate the activity entry.
- The coder durable consumer retained `DeliverPolicy.ALL`.
- Idle worker SIGINT exited successfully without a traceback.
- Dashboard session, CSRF task update, and stale-version 409 continued working.
- A real browser showed the old and new completed runs, the completed task, and
  retained login after reload with no browser errors.
- NATS exporter served live `gnatsd_varz` metrics.
- Caddy configuration and Prometheus configuration validated.
- Both Agent Manager and Caddy Compose configurations validated.
- Seven Ansible playbooks passed syntax checks with ansible-core 2.18.10 and
  community.docker 5.4.0; inventory and Pi JSON templates were rendered offline.
- Public Caddy CA parsed as a certificate; no private key was imported.

This does not prove central multi-worker behavior, upstream paid inference, or
live Caddy TLS routing. Perform the central acceptance checks below before
claiming deployment complete.

## Before any central deployment

1. Revoke/rotate exposed provider and LiteLLM administrator credentials.
   Database password rotation must update the existing PostgreSQL role and
   application configuration together. Do not blindly rotate the LiteLLM salt:
   stored encrypted credentials may require controlled re-encryption.
2. Review the reconciled code. Do not use `git add .`; stage only reviewed files.
   Existing private `.env` files, `.env` backups, worker keys and database dumps
   must never enter Git or a source archive.
3. Pause new run submissions and stop worker execution after active work has
   completed. Do not interrupt generated subprocesses and assume they stopped.
4. Confirm which worker source is actually installed. Deploy manager and worker
   changes together during the maintenance window; an older worker may not
   support the health responder or new heartbeat subjects.
5. Keep using `infra-lab-user` on the VM as the worker Ansible controller. Do not
   run VM-local inventory from Donatello or the Proxmox host.

## Stage the reviewed runtime on the VM

This is an optional source-file transfer when GitHub push is unavailable. It
is not a Git bundle and does not transfer databases or secrets. Run only after
reviewing the selected source files. The Caddy source must preserve any further
live route edits made after the capture.

On **Donatello**:

```bash
cd ~/TheGarden
umask 077
mkdir -p ~/Downloads
tar --exclude='__pycache__' --exclude='*.pyc' \
  -czf ~/Downloads/df-reviewed-runtime.tar.gz \
  Docker-Documents/agent-manager-service/agent_manager \
  Docker-Documents/agent-manager-service/requirements.txt \
  Docker-Documents/agent-manager-service/Dockerfile \
  Docker-Documents/agent-manager-service/compose.yml \
  Docker-Documents/agent-manager-service/compose.worker-test.yml \
  Docker-Documents/caddy_service/Caddyfile \
  Docker-Documents/grafana_services/prometheus/prometheus.yml \
  agent-worker/worker.py agent-worker/requirements.txt \
  docs/dark-factory-backlog.json scripts/seed_dark_factory_backlog.py
scp ~/Downloads/df-reviewed-runtime.tar.gz \
  infra-lab-user@100.94.49.45:~/
```

On **infra-lab-services**:

```bash
umask 077
STAGE=$(mktemp -d "$HOME/df-runtime-stage.XXXXXX")
tar -xzf ~/df-reviewed-runtime.tar.gz -C "$STAGE"
printf 'Reviewed source staged at %s\n' "$STAGE"
```

Keep this VM shell open so `$STAGE` and `$BACKUP` remain defined. Staging does
not change the running services. Ansible inventory/role changes should travel
through a separately reviewed repository update; this runtime archive does not
silently replace the controller checkout.

## Back up and stop only the manager control plane

On **infra-lab-services**, after workers and submitters are paused:

```bash
cd ~/agent-manager-service
umask 077
BACKUP="$HOME/df-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$BACKUP"
cp -a ~/agent-manager-service "$BACKUP/agent-manager-service"
cp ~/caddy-service/Caddyfile "$BACKUP/Caddyfile"
cp ~/grafana-service/prometheus/prometheus.yml "$BACKUP/prometheus.yml"
sudo docker image tag \
  "$(sudo docker inspect --format '{{.Image}}' agent-manager-agent-manager-1)" \
  local/agent-manager:pre-reconcile
sudo docker compose stop agent-manager
sudo docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP/agent-manager.dump"
test -s "$BACKUP/agent-manager.dump"
sudo docker compose exec -T postgres pg_restore --list \
  < "$BACKUP/agent-manager.dump"
sudo docker compose stop nats
sudo docker run --rm \
  -v agent-manager_nats_data:/data:ro \
  -v "$BACKUP:/backup" \
  alpine:3.22 tar -czf /backup/nats-data.tar.gz -C /data .
```

Stop immediately on any command failure. The private backup directory contains
secrets from the live `.env`; never upload it as a source artifact. Copy the
backups to approved protected storage. Catalog validation is not a full restore
test; test restoration in an isolated stack before a destructive operation.
Do not run `docker compose down -v` or recreate PostgreSQL volumes.

## Install the reviewed manager source without replacing .env

On **infra-lab-services**, in the same shell:

```bash
cp -a "$STAGE/Docker-Documents/agent-manager-service/agent_manager/." \
  ~/agent-manager-service/agent_manager/
cp "$STAGE/Docker-Documents/agent-manager-service/requirements.txt" \
  ~/agent-manager-service/requirements.txt
cp "$STAGE/Docker-Documents/agent-manager-service/Dockerfile" \
  ~/agent-manager-service/Dockerfile
cp "$STAGE/Docker-Documents/agent-manager-service/compose.yml" \
  ~/agent-manager-service/compose.yml
cp "$STAGE/Docker-Documents/agent-manager-service/compose.worker-test.yml" \
  ~/agent-manager-service/compose.worker-test.yml
nano ~/agent-manager-service/.env
```

Preserve existing `POSTGRES_*`, `AGENT_MANAGER_API_TOKEN`, volumes and project
name. Add independent session/webhook secrets privately; do not copy Donatello's
`.env`. Non-secret central settings:

```dotenv
DASHBOARD_PUBLIC_URL=https://infra-lab-services.tail494f6d.ts.net
DASHBOARD_SESSION_SECURE=true
GRAFANA_PUBLIC_URL=https://infra-lab-services.tail494f6d.ts.net/grafana
GRAFANA_NODE_DASHBOARD_UID=server-overview
PROMETHEUS_URL=http://prometheus:9090
MODEL_BASE_URL=http://litellm:4000/v1
NATS_BIND_ADDRESS=100.94.49.45
```

Configure Forgejo OAuth only after individual Forgejo accounts exist. The
bootstrap token works before OAuth is configured. Central migration from an
old API-only deployment creates dashboard tables; local Donatello task records
are not implicitly imported. Re-run the tracked backlog importer separately
when the central API is ready.

## Start and verify the upgraded API before changing Caddy

The current worker-test override publishes unauthenticated NATS only on the
VM's Tailscale IP. Use it only on the approved private lab with restrictive
Tailscale policy; per-worker NATS credentials are still pending. Do not expose
4222 to the LAN, public internet, or all interfaces.

```bash
cd ~/agent-manager-service
sudo docker compose -f compose.yml -f compose.worker-test.yml config --quiet
sudo docker compose -f compose.yml -f compose.worker-test.yml up -d --build
curl -fsS http://127.0.0.1:8090/readyz
curl -fsS http://127.0.0.1:8090/v1/auth/config
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/dashboard/
```

Expect readiness `ok`, auth capability JSON, and dashboard HTTP 200. Use the
same two Compose files for subsequent central operations so NATS publishing is
not accidentally removed. If readiness fails, stop rollout and inspect service
logs; do not restart every unrelated service.

## Merge the Caddy route and monitoring configuration

Review against live files first:

```bash
diff -u ~/caddy-service/Caddyfile \
  "$STAGE/Docker-Documents/caddy_service/Caddyfile"
```

A diff's exit status 1 means differences were found, not a failed read. Preserve
all routes added after the capture. When reviewed, install the Caddyfile and
validate before reload:

```bash
cp "$STAGE/Docker-Documents/caddy_service/Caddyfile" ~/caddy-service/Caddyfile
sudo docker exec caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

The captured live Caddy already publishes 8443. Do not replace its Compose file
merely to add a dashboard route. The updated repository Compose includes that
port for future deployments.

Similarly review the staged Prometheus configuration against the live one.
The reconciled central exporter target is `node-exporter:9100` for the tracked
bridge-network Compose. If the live exporter uses host networking, confirm its
reachable address instead of blindly changing it. After review:

```bash
cp "$STAGE/Docker-Documents/grafana_services/prometheus/prometheus.yml" \
  ~/grafana-service/prometheus/prometheus.yml
sudo docker exec grafana-services-prometheus-1 \
  promtool check config /etc/prometheus/prometheus.yml
sudo docker kill --signal=HUP grafana-services-prometheus-1
```

No Grafana database, dashboard, administrator account, or datasource UID needs
to be replaced. Preserve the existing `server-overview` dashboard.

## Worker and end-to-end acceptance

Update the source in the actual worker runtime directory, not just a repository
checkout. Stop the old worker before launching its replacement. Keep each
node's existing `.env`, model key, TLS trust, ID and coder consumer configuration.
Do not run both a service-managed and manual worker process.

Start only Inkii first, then Donatello, Raphael and Naruto after acceptance:

1. No matching enabled worker: POST run returns 503 and creates no run.
2. Worker running with code execution explicitly permitted for the disposable
   test: matching role receives a supplied harmless Python script.
3. Run shows `queued`, then `running`, then `completed` with expected stdout.
4. One completion activity entry; correct worker ID and hostname.
5. Dashboard session survives reload; task edits still use version conflicts.
6. Central `/dashboard/`, existing Forgejo/Grafana/LiteLLM routes and Open WebUI
   remain reachable under trusted HTTPS.
7. NATS exporter target is up; node labels retain worker-01 through worker-04.

A health reply is not exactly-once execution evidence. Keep process-lock,
lease/fencing, active cancellation, OMP, JEV and budget-ledger tasks open.

## Rollback boundary

Do not delete state to roll back. Stop the upgraded manager and pause workers,
restore the backed-up source/Compose/Caddy files, and start the saved
`local/agent-manager:pre-reconcile` image without rebuilding it. The old image
must not be expected to consume new worker subjects. Restore PostgreSQL/NATS
from backup only through an explicit maintenance/restore procedure: restoration
would discard post-backup changes. First test rollback against cloned data.
