# Ansible

This directory contains the Ansible project for TheGarden. It currently manages two different concerns:

- read-only audits of the Proxmox host;
- deployment of the Docker Compose stacks on `infra-lab-services`.

The Docker deployment is intentionally separate from the audit-oriented `site.yml` playbook.

## Project root

Run Ansible from this directory so `ansible.cfg` is loaded automatically:

```bash
cd Ansible
export PATH="$HOME/.local/bin:$PATH"
```

## Inventory

The default inventory is `inventory/hosts.yml`. `ansible.cfg` loads only that file.

```text
lab_local
└── server-debian          local Proxmox audit target

proxmox_hosts
└── lab_local              child group, not a direct host list

proxmox_guests
└── (empty)

docker_hosts
└── infra-lab-services     local connection; docker-services.yml does not SSH to 10.1.10.2

agent_workers
├── inkii                  100.98.125.127  user inkii       worker-02
├── naruto                 100.88.92.78    user naruto      worker-04
├── raphael                100.116.35.103  user raphael     worker-01
└── donatello              100.94.145.104  user bgurrol4    worker-03
```

Every host above pins `ansible_python_interpreter` to `/usr/bin/python3`. Worker hosts are reached by SSH. `infra-lab-services` is `ansible_connection: local`.
Inspect the inventory with:

```bash
ansible-inventory --graph
ansible-inventory --host infra-lab-services
```

## Collections

The Docker deployment uses `community.docker.docker_compose_v2`. Install the collection from the project requirements file:

```bash
ansible-galaxy collection install -r requirements.yml
```

## Docker Compose deployment

`playbooks/docker-services.yml` deploys the projects named in `inventory/group_vars/docker_hosts.yml`, in this order:

1. `caddy` at `/home/infra-lab-user/caddy-service` (`build: never`), which creates `caddy_proxy`
2. `keybroker` at `/home/infra-lab-user/TheGarden/Docker-Documents/keybroker-service` (`build: always`)
3. `forgejo` at `/home/infra-lab-user/forgejo-service`
4. `open-webui` at `/home/infra-lab-user/open-webui-service`
5. `llama` at `/home/infra-lab-user/llama-service`
6. `grafana` at `/home/infra-lab-user/grafana-service`
7. `agent-manager` at `/home/infra-lab-user/agent-manager-service` (`build: always`)
8. `api-guard-dashboard` at `/home/infra-lab-user/TheGarden/Docker-Documents/api-guard-dashboard` (`build: always`)

These are VM paths. Most Compose projects are separate deployment directories; Keybroker and API Guard use the existing `TheGarden` checkout path shown above. The playbook does not copy source files, does not deploy `litellm-service`, and does not add `compose.worker-test.yml`. Keep Keybroker, API Guard, Caddy proxy, and manager secrets in their deployment `.env` files. Because `infra-lab-services` is a local connection, the playbook runs on the controller. It does not SSH to `10.1.10.2`.

Repository source directories:

```text
TheGarden/Docker-Documents/caddy_service
TheGarden/Docker-Documents/forgejo
TheGarden/Docker-Documents/open_webui_services
TheGarden/Docker-Documents/llama_services
TheGarden/Docker-Documents/grafana_services
TheGarden/Docker-Documents/litellm-service
TheGarden/Docker-Documents/agent-manager-service
TheGarden/Docker-Documents/api-guard-dashboard
TheGarden/Docker-Documents/keybroker-service
```

Each project keeps its local `.env` file outside Git. Those files contain
secrets and are not managed by the repository. Set the deployment paths in
`inventory/group_vars/docker_hosts.yml` to the checkout location used by the
host running Ansible; do not commit secret values.

Validate the playbook:

```bash
ansible-playbook playbooks/docker-services.yml --syntax-check
```

Deploy or update the stacks:

```bash
ansible-playbook playbooks/docker-services.yml --ask-become-pass
```

The playbook uses `become: true` because the current user does not have direct permission to access `/var/run/docker.sock`.

The agent manager has `build: always`; the other Compose projects use `build: never`. Images are pulled only when missing.

## Audits

Capture a read-only host snapshot:

```bash
ansible-playbook playbooks/audit.yml
```

Run the existing audit-oriented site playbook:

```bash
ansible-playbook playbooks/site.yml --syntax-check
ansible-playbook playbooks/site.yml --check --diff
```

Before the first worker rollout, stage Caddy's **public** root certificate on
the Ansible controller. On the Caddy VM:

```bash
cd ~/caddy-service
sudo docker compose exec -T caddy \
  cat /data/caddy/pki/authorities/local/root.crt \
  > /tmp/thegarden-caddy-root.crt
```

Copy that certificate to `/tmp/thegarden-caddy-root.crt` on the controller if
the playbook runs elsewhere. The role fails before deleting any worker key if
the certificate or authenticated worker identity check is missing. Never copy
or distribute Caddy's `root.key`.

`playbooks/agent-workers.yml` installs the worker runtime, Caddy root CA,
Keybroker URL, and a supervised `dark-factory-agent-worker.service`. Workers
obtain a model-scoped, budget-limited key per run, then revoke it on completion;
the service does not receive a persistent LiteLLM key. Before the one-time
cutover, Ansible checks Keybroker readiness and authenticates each host through
`/v1/worker/identity` over HTTPS, then stops the old cron/systemd worker,
removes `~/.config/litellm/worker-api-key`, verifies that the old process is
gone, and records a cutover marker. It does not revoke the old LiteLLM key:
after confirming each worker is using session keys, revoke legacy worker keys
in API Guard. Subsequent playbook runs restart the service only when its code
or configuration changes.

The worker uses the Tailnet-only Caddy endpoint at
`https://infra-lab-services.tail494f6d.ts.net/keybroker`. Check a host with
`sudo systemctl status dark-factory-agent-worker` and
`sudo journalctl -u dark-factory-agent-worker -n 100 --no-pager`.

The worker process polls the shared durable `agent-workers-coder` consumer and
sends a heartbeat every 15 seconds. The default `worker_nats_url` is
`nats://100.94.49.45:4222`; that listener must be reachable over Tailscale.
Mission artifacts are generated from the user's goal and saved under
`AGENT_WORKSPACE/missions/`; the worker returns artifact contents to Agent
Manager/Open WebUI. This flow does not clone or push the configured Forgejo
repository, and generated artifacts are not executed by mission runs.

Only the four enrolled workers in `inventory/hosts.yml` receive Keybroker
access. `michelangelo` and `sammy` remain unregistered until their owners opt
in and their SSH/Tailscale identities are enrolled. Do not leave their old
static keys active while they remain out of band.

Use `--ask-become-pass` because package installation and system CA trust require
privilege. `allow_generated_code` remains `false`; the separate
`create-python-script` operation executes generated Python unsandboxed and is
not needed for mission artifacts.
