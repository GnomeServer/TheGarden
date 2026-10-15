# TheGarden - Small AI Lab

## Scope

This is the deliberately small first version of the lab.

Current lab description:

- One Proxmox VE 9 server laptop, converted in-place from Debian 13
- Four worker hosts are in the Ansible inventory (`inkii`, `naruto`, `raphael`, `donatello`); the hardware plan is five Ubuntu laptops
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
│   ├── docs/
│   ├── inventory/
│   ├── playbooks/
│   ├── roles/
│   └── state/
├── Docker-Documents/
│   ├── README.md
│   ├── agent-manager-service/
│   ├── caddy_service/
│   ├── forgejo/
│   ├── grafana_services/
│   ├── litellm-service/
│   ├── llama_services/
│   └── open_webui_services/
├── docs/
│   ├── lab-layers.md
│   └── software-factory-roadmap.md
├── Proxmox-Documents/
│   └── proxmox-installation.md
├── Worker-Nodes/
│   └── Pi-LiteLLM-Worker-setup.md
├── README.md
├── server-debian-ufw-hardening.md
├── small-lab-open-source-architecture.md
└── tailscale-setup.md
```

The Ansible project and its supporting directories are grouped under `Ansible/`. Installation history remains in `Ansible/docs/`; current operating instructions are in `Ansible/README.md`.

## Current Ansible controller

| Item | Observed value |
| --- | --- |
| Host | `server-debian` |
| OS | Proxmox VE 9.2 on Debian GNU/Linux 13 (`trixie`) |
| Architecture | `x86_64` |
| LAN address | `10.1.10.156` |
| Tailscale address | `100.102.154.23` |
| Hardware | Dell Pro Max 14 MC14250 |
| Ansible | `ansible-core 2.21.4` |

The repository checkout is `TheGarden/`. The Ansible project root is
`TheGarden/Ansible/`.

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

A Debian VM exists on the Proxmox node as VMID `100`, hostname `infra-lab-services`, with address `10.1.10.2/24` on `ens18`. `Ansible/inventory/hosts.yml` already lists it as a local `docker_hosts` target (`ansible_connection: local`), so `docker-services.yml` runs on the Ansible controller and does not SSH to `10.1.10.2`. `Ansible/inventory/proxmox-guests.example.yml` is not loaded; enabling it would duplicate the hostname. `proxmox_guests` in the active inventory is empty.

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

The role does not install Proxmox or modify networking. The staged installation procedure remains documented in [`Proxmox-Documents/proxmox-installation.md`](Proxmox-Documents/proxmox-installation.md).

### `Ansible/playbooks/agent-workers.yml`

This playbook targets `agent_workers` and applies `roles/agent_worker`. The role copies a LiteLLM key to `~/.config/litellm/worker-api-key` and tries to install Pi harness files. Those sources (`files/caddy-local-root.crt`, `templates/models.json.j2`, `templates/settings.json.j2`) are not in the role, and the role does not install `agent-worker/worker.py` or set `WORKER_*`. Do not treat this playbook as a working worker rollout.

## Inventory and Tailscale

`Ansible/inventory/hosts.yml` contains the Proxmox audit target `server-debian`, the local Docker target `infra-lab-services`, and the approved worker nodes in the `agent_workers` group. The Docker target and the worker hosts pin `ansible_python_interpreter` to `/usr/bin/python3`. `playbooks/agent-workers.yml` does not install `worker.py`; the role also references certificate and template files that are not in `roles/agent_worker/`. Runtime setup is still manual. See [`agent-worker/README.md`](agent-worker/README.md). `Ansible/inventory/worker-1.yml` remains an optional single-worker inventory for isolated tests and is not loaded separately by default. Remote Tailscale peers are also available as a non-loaded template in `Ansible/inventory/tailscale-peers.example.yml`; operating-system usernames and SSH policy must be verified before enabling additional peers.

Observed Tailscale nodes:

- `server-debian` — `100.102.154.23` — this host
- `infra-lab-services` — `100.94.49.45` — active VM
- `donatello` — `100.94.145.104` — active
- `inkii` — `100.98.125.127` — coder worker
- `naruto-dell-pro-max-14-mc14250` — `100.88.92.78`
- `raphael` — `100.116.35.103` — active

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

Repository Compose projects for the `infra-lab-services` VM:

- Caddy (`Docker-Documents/caddy_service`)
- Forgejo and its PostgreSQL database (`Docker-Documents/forgejo`)
- Open WebUI and Ollama (`Docker-Documents/open_webui_services`)
- llama.cpp (`Docker-Documents/llama_services`)
- LiteLLM and its PostgreSQL database (`Docker-Documents/litellm-service`)
- Grafana, Prometheus, and Node Exporter (`Docker-Documents/grafana_services`)
- Agent Manager, its PostgreSQL database, and NATS JetStream (`Docker-Documents/agent-manager-service`)

The checked-in Caddyfile listens on ports 80 and 443 only. It routes `/grafana/` to Grafana, `/forgejo/` to Forgejo, and every other path on `infra-lab-services.tail494f6d.ts.net` to the Proxmox UI at `https://10.1.10.156:8006`. It does not route `/litellm/`, `/llama/`, or Open WebUI, and it does not publish port `8443`.

`Ansible/playbooks/docker-services.yml` deploys every project above except LiteLLM. LiteLLM is started from its own Compose directory. The base Agent Manager Compose file does not publish NATS; remote workers need `compose.worker-test.yml`. See [`Docker-Documents/README.md`](Docker-Documents/README.md) and [`Ansible/README.md`](Ansible/README.md).

## Not managed yet

- SSH private keys, credentials, or vault passwords
- Python worker runtime (`worker.py`, virtualenv, and `WORKER_*`). `roles/agent_worker` does not install that runtime
- Firewall rules
- Tailscale ACLs, routes, or DNS settings
- A complete package or service desired-state policy
- Kubernetes or high-availability storage
