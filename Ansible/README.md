# Ansible

This directory contains the Ansible project for TheGarden. It manages:

- read-only audits of the Proxmox host;
- deployment of the Docker Compose stacks on `infra-lab-services`;
- existing Pi/LiteLLM worker configuration and node activity collectors.

The Docker deployment is intentionally separate from the audit-oriented `site.yml` playbook.

## Project root

Run Ansible as `infra-lab-user` from the checkout on the infrastructure VM, in this directory so `ansible.cfg` is loaded automatically. Do not run the active inventory on Donatello or bare metal: its only local target is the VM.

```bash
cd Ansible
export PATH="$HOME/.local/bin:$PATH"
```

## Inventory

The default inventory is `inventory/hosts.yml`:

```text
server-debian       Proxmox host used by the audit playbooks
infra-lab-services  Docker VM used by docker-services.yml
```

`infra-lab-services` is the controller and sole local target (`infra-lab-user`, LAN `10.1.10.2`, Tailscale `100.94.49.45`). It belongs to `docker_hosts` and uses `/usr/bin/python3`. `server-debian` is the separate Proxmox host reached explicitly over SSH as `df-server@100.102.154.23`; it is not the controller. Workers also use SSH.

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

`playbooks/docker-services.yml` deploys the existing Compose projects in dependency order:

1. `caddy-service`, which creates the external `caddy_proxy` network;
2. `forgejo-service`, `open-webui-service`, `llama-service`, and `grafana-service`;
3. `agent-manager-service`, which needs both `caddy_proxy` and `open-webui_internal`.

The project paths are defined in:

```text
inventory/group_vars/docker_hosts.yml
```

The Compose source directories are stored in the repository under:

```text
TheGarden/Docker-Documents/caddy_service
TheGarden/Docker-Documents/forgejo
TheGarden/Docker-Documents/open_webui_services
TheGarden/Docker-Documents/llama_services
TheGarden/Docker-Documents/grafana_services
TheGarden/Docker-Documents/agent-manager-service
```

Each live project runs from its separate `/home/infra-lab-user/<name>-service`
directory, as recorded in `inventory/group_vars/docker_hosts.yml`, not directly
from `Docker-Documents`. Local `.env` files remain outside Git and are not
managed here. Do not commit secret values.

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

Generated state is written under `state/` and is ignored by Git because it contains host-specific information.

## Secrets and future layout

Do not add Compose `.env` files, API tokens, passwords, private keys, or vault passwords to TheGarden. The next step for a reproducible rebuild is to store secret values in Ansible Vault and template each Compose `.env` file with mode `0600`.

Worker nodes are grouped under `agent_workers` in the default inventory.
`playbooks/agent-workers.yml` is the canonical worker playbook; the captured
`configure-agent-workers.yml` duplicate is intentionally not imported.
`roles/agent_worker/` provisions existing Pi models/settings, the public Caddy
CA, and each worker's separate LiteLLM key from
`/home/infra-lab-user/litellm-service/.worker-XX-api-key` on the VM controller.
The shell CA export is installed in both `.profile` and `.bashrc`. Use
`--limit inkii` for an initial rollout. Pi remains in use until the planned
OMP cutover; this role does not install or implement OMP. Worker runtime
configuration is documented in `../agent-worker/README.md`.

## Node activity collectors

`playbooks/node-activity-collectors.yml` targets the infrastructure VM, four
coder workers, and the bare-metal Proxmox host. Limit the initial deployment
to `agent_workers:docker_hosts`; deploy to `server-debian` only when approved.
Worker IDs stay `worker-01` through `worker-04` for Raphael, Inkii, Donatello,
and Naruto respectively.

Create the node-scoped keys first as documented in
[`../node-activity-collector/README.md`](../node-activity-collector/README.md),
then validate and deploy:

```bash
ansible-playbook playbooks/node-activity-collectors.yml --syntax-check
ansible-playbook playbooks/node-activity-collectors.yml --limit 'agent_workers:docker_hosts' --check --diff --ask-become-pass
ansible-playbook playbooks/node-activity-collectors.yml --limit 'agent_workers:docker_hosts' --ask-become-pass
```

The plaintext node keys remain under the private controller directory
`/home/infra-lab-user/.config/dark-factory/activity-keys/<node>` (directory
mode `0700`, files `0600`) and are copied with `no_log: true`. Provision them
independently of existing `.env`, LiteLLM, Forgejo, and worker credentials.
