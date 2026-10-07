# Ansible configuration and lab infrastructure

This document describes the Ansible project stored in TheGarden. It covers the
current inventory, read-only Proxmox validation, Docker Compose deployment, and
worker rollout conventions.

## Project location

Run as `infra-lab-user` from a checkout on the infrastructure VM only:

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

The active inventory contains:

```text
proxmox_hosts
└── server-debian

docker_hosts
└── infra-lab-services

agent_workers
├── inkii
├── naruto
├── raphael
└── donatello
```

`infra-lab-services` is the controller and sole local target (LAN `10.1.10.2`,
Tailscale `100.94.49.45`). `server-debian` is reached explicitly by SSH as
`df-server@100.102.154.23`; audits and the baseline target `proxmox_hosts`.
Do not use this local-target inventory from Donatello or bare metal.

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

The Compose `.env` files contain secrets and remain local deployment files;
they must not be committed. `inventory/group_vars/docker_hosts.yml` points
to the live, separate `/home/infra-lab-user/<name>-service` folders on the VM,
not to the repository's Compose source directories.

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
existing Pi/LiteLLM provisioning role and canonical playbook are:

```text
TheGarden/Ansible/roles/agent_worker
TheGarden/Ansible/playbooks/agent-workers.yml
```

The captured duplicate `configure-agent-workers.yml` is intentionally omitted.
Pi remains supported until the planned OMP cutover; OMP is not implemented
by this role. Per-worker LiteLLM keys remain in the controller's
`/home/infra-lab-user/litellm-service/.worker-XX-api-key` files; activity keys
are independent files under
`/home/infra-lab-user/.config/dark-factory/activity-keys/<node>`.

The worker runtime contract, NATS shared-consumer configuration, LiteLLM
variables, CA trust, and troubleshooting steps are documented in:

```text
TheGarden/agent-worker/README.md
```

Start with one host:

```bash
cd TheGarden/Ansible
ansible-playbook playbooks/agent-workers.yml --limit inkii
```

The worker must have a unique `WORKER_ID`, while workers with the same role
share `WORKER_ROLE` and `WORKER_CONSUMER`. Generated code execution remains an
explicit opt-in with `ALLOW_GENERATED_CODE=1` and is not sandboxed.

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
