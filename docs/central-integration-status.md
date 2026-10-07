# Central dashboard integration handoff

**Scope:** Repository preparation and isolated local verification. This is not a report of a central VM deployment.

## Repository state

- Prepared locally on branch `infra-capture/live-capture-20261007-144000` in commit `81dcf49` (`Prepare central dashboard and worker chat integration`). At the time of this handoff, that commit had **not** been pushed or deployed. Verify the branch and commit at the source before transferring it; do not assume the VM or GitHub has received them.
- Agent Manager now has a separate run-only `AGENT_MANAGER_OPENWEBUI_TOKEN` for Open WebUI. It cannot authorize dashboard administration, task management, user management, or run listing. Forgejo OAuth remains the human-login path; the bootstrap API token is not a shared personal login.
- The opt-in [Open WebUI Pipe](../Docker-Documents/open_webui_services/README.md#agent-manager-coder-function-manual-opt-in) submits pooled `create-python-script` runs, polls their state, and reports worker output and failure without silently resubmitting uncertain requests. Repository changes do not install the Function into a running Open WebUI instance.
- The [central deployment helper](../scripts/deploy_central_dashboard.py) defaults to a read-only plan. Apply is VM-local and manager-only, with checks for existing Compose identity, data volumes, port bindings, network topology, credentials, and Caddy routing. It backs up the manager image, source, configuration, and PostgreSQL database before replacing the manager. It does not initialize or restart PostgreSQL, NATS, Grafana, Forgejo, or Open WebUI.
- The dashboard uses PostgreSQL for tasks, runs, worker state, and activity; NATS for worker events; Forgejo OAuth and signed webhooks for identity and server-visible Git activity; and the existing Prometheus/Grafana installation for metrics and per-host links. Git activity tracking does not observe uncommitted local edits.

## Verification performed locally

| Check | Observed result |
| --- | --- |
| Agent Manager regression suite | 20 tests passed |
| Open WebUI Pipe regression suite | 22 tests passed |
| Deployment helper regression suite | 22 tests passed |
| JavaScript syntax | `node --check` passed for dashboard `app.js` |
| Generated Caddy routing | `caddy validate` reported `Valid configuration` against an isolated generated fixture |
| Disposable real service stack | PostgreSQL, NATS, Manager, model stub, and a disposable worker completed one Pipe-submitted run, `b2e81470-d84e-4bc2-85d6-ad57d09b70b8`; worker `smoke-worker`, return code `0`, output `CENTRAL_PIPE_SMOKE_OK`. Run-only credential requests to user and run-list APIs returned `401`. |
| Browser | Local dashboard login displayed the worker and completed run; a deadline task persisted through reload and moved to **In progress**. No browser errors were observed. |

The isolated containers and throwaway scripts were removed after the smoke. The model response in that scenario was deterministic; it did **not** verify real LiteLLM inference, the live Open WebUI installation, live Forgejo OAuth/webhooks, central Grafana targets, or VM Caddy routing. The helper's full live `--apply` path has not been exercised.

## Central rollout sequence

1. Transfer or push the reviewed commit to a checkout on the infrastructure VM. Do not overwrite its existing service directories or `.env` files with a blanket repository copy.
2. Read [Central dashboard deployment](central-dashboard-deployment.md), especially the pause, backup, identity, rollback, and credential prerequisites. On the VM, from the reviewed checkout, run the **read-only** plan:

   ```bash
   python3 scripts/deploy_central_dashboard.py --plan
   ```

3. Resolve any stop condition privately. Arrange authorized VM-local access as `infra-lab-user`, noninteractive Docker sudo, protected backup storage, paused submitters, and drained or paused workers. Only after reviewing the plan and accepting manager downtime, run:

   ```bash
   python3 scripts/deploy_central_dashboard.py --apply --submissions-paused
   ```

4. Configure Forgejo OAuth callback and a signed repository/organization webhook using the private manager environment; confirm an individual user's login and one deduplicated test push event. Verify the existing Prometheus targets, `host` labels, and individual Grafana access. Follow the [dashboard operations guide](dark-factory-dashboard.md) for exact settings.
5. Separately install the [Pipe](../Docker-Documents/open_webui_services/README.md#agent-manager-coder-function-manual-opt-in) into the existing Open WebUI instance as an administrator. Give its admin-only Valve the independent run-only credential. Run a synthetic task on a disposable worker and compare the Pipe result, Manager run, worker ID, return code, and artifact evidence.
6. Check the central HTTPS dashboard and unchanged `/forgejo`, `/grafana`, and Open WebUI `:8443` routes in a browser before resuming submissions. If any check fails, keep submissions paused and use the deployment guide's recovery procedure; do not blindly restore a database over new work.

**Safety boundary:** The current worker executes generated Python as its OS user, without a sandbox. Do not expose the Pipe to ordinary users or production workers until execution isolation and credential separation are in place. The current worker implements only `create-python-script`, not an autonomous plan/review/merge pipeline. Existing worker NATS connectivity over the approved private Tailscale path is a separate prerequisite; the deployment helper does not open a NATS port.
