# TheGarden - Small AI Lab

## Scope

This is the deliberately small first version of the lab.

Current lab description:

- One Proxmox VE 9 server laptop, converted in-place from Debian 13
- Five Ubuntu worker laptops
- Approximately 32 GB RAM and 512 GB SSD per laptop
- Intel integrated Arc graphics on the laptops
- Tailscale tailnet already configured
- No other services considered production-ready yet

The goal is to build a useful multi-node AI and software-agent lab without introducing Kubernetes, high-availability storage, or a large number of complex services too early.

## Repository layout

```text
TheGarden/
├── agent-worker/
│   ├── worker.py
│   ├── requirements.txt
│   └── README.md
├── Ansible/
│   ├── ansible.cfg
│   ├── requirements.yml
│   ├── inventory/
│   ├── playbooks/
│   ├── roles/
│   └── state/
├── Docker-Documents/
│   ├── README.md
│   ├── caddy_service/
│   ├── forgejo/
│   ├── agent-manager-service/
│   ├── grafana_services/
│   ├── llama_services/
│   └── open_webui_services/
├── docs/
│   └── lab-layers.md
├── proxmox-installation.md
├── README.md
├── server-debian-ufw-hardening.md
├── small-lab-open-source-architecture.md
└── tailscale-setup.md
```

The Ansible project and its supporting directories are grouped under `Ansible/`. Installation history remains in `Ansible/docs/`; current operating instructions are in `Ansible/README.md`.

Implementation and recovery handoff: [`docs/github-handoff-dark-factory.md`](docs/github-handoff-dark-factory.md).
JEV routing, token budgeting, OMP migration, and service placement: [`docs/jev-budget-omp-architecture.md`](docs/jev-budget-omp-architecture.md).

## Bare-metal host and worker controller

| Item | Observed value |
| --- | --- |
| Host | `server-debian` |
| OS | Proxmox VE 9.2 on Debian GNU/Linux 13 (`trixie`) |
| Architecture | `x86_64` |
| LAN address | `10.1.10.156` |
| Tailscale address | `100.102.154.23` |
| Hardware | Dell Pro Max 14 MC14250 |
| Ansible | `ansible-core 2.21.4` |

The table above describes the bare-metal host. Captured worker provisioning
runs as `infra-lab-user` on `infra-lab-services`; it is the sole local inventory
target. `server-debian` is an explicit SSH target, preventing accidental
Proxmox tasks on the VM. Run Ansible from `TheGarden/Ansible/` on the VM.

## Using Ansible

Run Ansible from its project directory so `Ansible/ansible.cfg` is discovered automatically:

```bash
cd TheGarden/Ansible
export PATH="$HOME/.local/bin:$PATH"

ansible --version
ansible-config dump --only-changed
ansible-inventory --graph
ansible lab_local -m ansible.builtin.ping
```

Capture a read-only snapshot of the current host:

```bash
ansible-playbook playbooks/audit.yml
```

The audit records Ansible facts, installed packages, systemd services, Proxmox version output, and Tailscale status under `Ansible/state/`. Generated state files contain machine-specific information and are ignored by Git by default.

A Debian VM exists on the Proxmox node as VMID `100`, hostname `infra-lab-services`, with address `10.1.10.2/24` on `ens18`. Its active Ansible target is `infra-lab-user@10.1.10.2`.

## Playbooks and roles

### `Ansible/playbooks/audit.yml`

The audit playbook gathers the current host state and writes:

- `Ansible/state/server-debian-facts.json`
- `Ansible/state/server-debian-packages.json`
- `Ansible/state/server-debian-services.json`
- `Ansible/state/tailscale-status.txt`
- `Ansible/state/proxmox-version.txt`
- `Ansible/state/audit-meta.yml`

These files are snapshots, not the desired configuration.

### `Ansible/playbooks/site.yml`

This playbook currently includes:

- `common` — verifies and reports the Debian-family host; it is audit-only
- `proxmox` — validates the installed Proxmox VE version, running kernel, bridge, and services; it is read-only

The existing `site.yml` remains audit-oriented. Docker Compose deployment is handled separately by `Ansible/playbooks/docker-services.yml`.

Validate before applying changes:

```bash
ansible-playbook playbooks/site.yml --syntax-check
ansible-playbook playbooks/site.yml --check --diff
```

### `Ansible/roles/common`

The common role confirms the Debian-family platform and reports host information without changing packages, services, users, firewall rules, or networking.

### `Ansible/roles/proxmox`

The Proxmox role validates the completed in-place installation. Run it directly with:

```bash
cd TheGarden/Ansible
ansible-playbook playbooks/proxmox-audit.yml
```

The role does not install Proxmox or modify networking. The staged installation procedure remains documented in [`proxmox-installation.md`](proxmox-installation.md).

## Inventory and Tailscale

`Ansible/inventory/hosts.yml` preserves the four coder IDs and separate key
files. The services VM is `10.1.10.2`; the local Docker networks on Donatello
and on the VM are separate even when their names match.

The operator has restored both central hosts and supplied a running-service
snapshot. Reconciliation and deployment steps are in
[`docs/infra-reconciliation.md`](docs/infra-reconciliation.md).

- `server-debian` — `100.102.154.23` — bare-metal host, SSH target
- `infra-lab-services` — `100.94.49.45` — services VM and worker controller
- `donatello` — `100.94.145.104` — online coder, `worker-03`
- `inkii` — `100.98.125.127` — online coder, `worker-02`
- `naruto-dell-pro-max-14-mc14250` — `100.88.92.78` — online coder, `worker-04`
- `raphael` — `100.116.35.103` — online coder, `worker-01`

## Git workflow

The Git repository tracks the Ansible configuration and documentation. The remote is:

```text
git@github.com:GnomeServer/TheGarden.git
```

Check the working tree from the repository root:

```bash
cd TheGarden
git status
git log --oneline --decorate -5
```

Commit Ansible changes explicitly:

```bash
git add Ansible docs README.md .gitignore
git commit -m "Describe the change"
git push
```

Generated `Ansible/state/` snapshots are ignored because they contain hostnames, addresses, package inventories, and service details. Review sensitive infrastructure information before publishing any files.

## Docker services

The `infra-lab-services` VM currently runs these Docker Compose services:

- Caddy
- Forgejo and its PostgreSQL database
- Open WebUI and Ollama
- llama.cpp
- Grafana, Prometheus, and Node Exporter
- Agent Manager, its PostgreSQL database, and NATS JetStream

Caddy is the HTTPS entry point; Grafana is available under `/grafana/`, Forgejo under `/forgejo/`, and the LiteLLM gateway under `/litellm/`. The Agent Manager source and Compose project are stored in `Docker-Documents/agent-manager-service`. The remote worker pool uses NATS over the VM's Tailscale address and is documented in [`agent-worker/README.md`](agent-worker/README.md). See [`Docker-Documents/README.md`](Docker-Documents/README.md) for the Docker topology and [`Ansible/README.md`](Ansible/README.md) for deployment through Ansible.

## Not managed yet

- SSH keys, credentials, or vault passwords
- Remote-host configuration for worker laptops
- Firewall rules
- Tailscale ACLs, routes, or DNS settings
- A complete package or service desired-state policy
- Kubernetes or high-availability storage
