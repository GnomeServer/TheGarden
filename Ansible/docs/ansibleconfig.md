# Ansible configuration and lab infrastructure

This document describes the Ansible project stored in TheGarden. It covers the
current inventory, read-only Proxmox validation, Docker Compose deployment, and
worker rollout conventions.

## Project location

From a checkout of the repository:

```bash
cd TheGarden/Ansible
export PATH="$HOME/.local/bin:$PATH"
```

`Ansible/ansible.cfg` configures the default inventory, role and collection
paths, automatic Python interpreter discovery, SSH host-key checking, ten
parallel forks, a 30-second connection timeout, SSH pipelining, and disabled
retry files.

The default inventory is:

```text
TheGarden/Ansible/inventory/hosts.yml
```

## Inventory groups

```text
lab_local
└── server-debian

proxmox_hosts
└── lab_local

proxmox_guests
└── (empty)

docker_hosts
└── infra-lab-services

agent_workers
├── inkii
├── naruto
├── raphael
└── donatello
```

The worker hosts use their Tailscale addresses, configured SSH usernames, and
the `coder` worker role. `inventory/worker-1.yml` is retained as an optional
single-worker test inventory and is not loaded by the default configuration.
Do not load it together with `hosts.yml` unless duplicate definitions are
intentional.

Inspect and test the inventory:

```bash
cd TheGarden/Ansible
ansible-inventory --graph
ansible-inventory --host inkii
ansible agent_workers --limit inkii -m ansible.builtin.ping
```

Use `--limit` while bringing up a new worker so an unreachable node does not
block the entire rollout.

## Compose projects

Compose source and documentation are stored in the repository under:

```text
TheGarden/Docker-Documents/caddy_service
TheGarden/Docker-Documents/forgejo
TheGarden/Docker-Documents/open_webui_services
TheGarden/Docker-Documents/llama_services
TheGarden/Docker-Documents/grafana_services
TheGarden/Docker-Documents/litellm-service
TheGarden/Docker-Documents/agent-manager-service
```

The paths in `inventory/group_vars/docker_hosts.yml` are absolute VM checkout paths under `/home/infra-lab-user/`, not these repository directories. The playbook does not deploy `litellm-service`.

Install the Docker collection and validate the deployment playbook:

```bash
cd TheGarden/Ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook playbooks/docker-services.yml --syntax-check
```

Deploy the Compose projects in dependency order:

```bash
ansible-playbook playbooks/docker-services.yml --ask-become-pass
```

The playbook targets `docker_hosts`, not `all`. Caddy starts first because it
creates `caddy_proxy`; Agent Manager starts after the networks and dependent
services are available.

## Worker deployment

The worker inventory is in `inventory/hosts.yml` under `agent_workers`. The
playbook and active role are:

```text
TheGarden/Ansible/roles/agent_worker
TheGarden/Ansible/playbooks/agent-workers.yml
```

The worker runtime contract, NATS shared-consumer configuration, LiteLLM
variables, CA trust, and troubleshooting steps are documented in:

```text
TheGarden/agent-worker/README.md
```

`playbooks/agent-workers.yml` installs Python, a virtualenv, cron, CA trust,
and process-management tools; copies `worker.py`, requirements, and the OMP job
image sources; builds the pinned image; installs each worker's LiteLLM key and
Caddy root certificate; and manages the worker with systemd. Targets deploy one
at a time. Source, environment, certificate, and image changes restart the
service. The playbook removes the legacy cron entry and obsolete `mission.py`
module. Mission output is uploaded to Agent Manager's PostgreSQL artifact store
and returned as a signed download link; the temporary OMP workspace is removed
after upload.

`allow_generated_code` is false by default. This blocks only the separate,
unsandboxed `create-python-script` operation; OMP mission coding remains enabled.
Only explicitly enable it on a worker if unsandboxed code execution is
deliberately accepted. Each worker requires its own LiteLLM virtual key.

## Proxmox validation

The Proxmox roles are read-only validators. They do not install Proxmox, alter
networking, restart services, or reboot hosts.

```bash
cd TheGarden/Ansible
ansible-playbook playbooks/proxmox-audit.yml --syntax-check
ansible-playbook playbooks/proxmox-audit.yml
```

The original in-place Proxmox installation used a host-local installation
script that is not part of TheGarden. Its procedure is documented separately
in `Proxmox-Documents/proxmox-installation.md`.

## Audits and generated state

Run the read-only audit playbook with:

```bash
cd TheGarden/Ansible
ansible-playbook playbooks/audit.yml
```

Generated host snapshots are written under `Ansible/state/` and ignored by Git
because they contain hostnames, addresses, package inventories, and service
details.

## Secrets

Do not commit any of the following:

- Compose `.env` files
- LiteLLM virtual or master keys
- Agent Manager API tokens
- SSH private keys
- Ansible vault passwords
- Caddy private keys

Use Ansible Vault or another secure secret store for future automated
provisioning. The Caddy public root certificate may be distributed to workers,
but never distribute `root.key`.
