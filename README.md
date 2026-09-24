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
├── Ansible/
│   ├── ansible.cfg
│   ├── requirements.yml
│   ├── inventory/
│   ├── playbooks/
│   ├── roles/
│   └── state/
├── Docker-Documents/
│   ├── Caddyfile
│   ├── caddy-setup.md
│   └── grafana_services/
├── docs/
│   └── lab-layers.md
├── proxmox-installation.md
├── README.md
├── server-debian-ufw-hardening.md
├── small-lab-open-source-architecture.md
└── tailscale-setup.md
```

The Ansible project and its supporting directories are now grouped under `Ansible/`. The canonical Ansible Markdown files are kept in `docs/`; duplicate copies that were previously under `Ansible/` were removed.

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

The repository is currently at `/home/df-server/lab-infra`. The requested `/lab-infra` path requires root access. To move the repository after reviewing it:

```bash
sudo mv "$HOME/lab-infra" /lab-infra
sudo chown -R df-server:df-server /lab-infra
```

After moving it, the Ansible project root is `/lab-infra/Ansible`.

## Using Ansible

Run Ansible from its project directory so `Ansible/ansible.cfg` is discovered automatically:

```bash
cd /home/df-server/lab-infra/Ansible   # use /lab-infra/Ansible after moving it
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

A Debian VM now exists on the Proxmox node as VMID `100`, hostname `infra-lab-services`, with address `10.1.10.2/24` on `ens18`. Its Ansible connection template is `Ansible/inventory/proxmox-guests.example.yml`; it should be activated after installing SSH and confirming the guest is routable.

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

No state-changing service roles are enabled yet.

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
cd /home/df-server/lab-infra/Ansible
ansible-playbook playbooks/proxmox-audit.yml
```

The role does not install Proxmox or modify networking. The staged installation procedure remains documented in [`proxmox-installation.md`](proxmox-installation.md).

## Inventory and Tailscale

`Ansible/inventory/hosts.yml` intentionally contains only this machine as an active Ansible target. Remote Tailscale peers are available as a non-loaded template in `Ansible/inventory/tailscale-peers.example.yml`; operating-system usernames and SSH policy must be verified before enabling them.

Observed Tailscale nodes:

- `server-debian` — `100.102.154.23` — this host
- `donatello` — `100.94.145.104` — active
- `inkii` — `100.98.125.127` — offline when checked
- `naruto-dell-pro-max-14-mc14250` — `100.88.92.78`
- `raphael` — `100.116.35.103` — active

## Git workflow

The Git repository tracks the Ansible configuration and documentation. The remote is:

```text
git@github.com:GnomeServer/TheGarden.git
```

Check the working tree from the repository root:

```bash
cd /home/df-server/lab-infra
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

The `infra-lab-services` VM currently runs Caddy, Grafana, Prometheus, and Node Exporter with Docker Compose. Caddy is the HTTPS entry point, and Grafana is available under `/grafana/`. See [`Docker-Documents/README.md`](Docker-Documents/README.md) for the verified topology, deployment files, and test command.

## Not managed yet

- SSH keys, credentials, or vault passwords
- Remote-host configuration for worker laptops
- Firewall rules
- Tailscale ACLs, routes, or DNS settings
- A complete package or service desired-state policy
- Kubernetes or high-availability storage
