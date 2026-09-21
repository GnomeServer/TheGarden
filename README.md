# TheGarden - Small AI Lab

## Scope

This is the deliberately small first version of the lab.

Current lab description from the repository:

- One Debian server laptop
- Five Ubuntu worker laptops
- Approximately 32 GB RAM and 512 GB SSD per laptop
- Intel integrated Arc graphics on the laptops
- Tailscale tailnet already configured
- No other services considered production-ready yet

The goal is to build a useful multi-node AI and software-agent lab without introducing Kubernetes, high-availability storage, or a large number of complex services too early.

## Current Ansible controller

The current controller is:

| Item | Observed value |
| --- | --- |
| Host | `server-debian` |
| OS | Debian GNU/Linux 13 (`trixie`) |
| Architecture | `x86_64` |
| LAN address | `10.1.10.156` |
| Tailscale address | `100.102.154.23` |
| Hardware | Dell Pro Max 14 MC14250 |
| Ansible | `ansible-core 2.21.4` |

The Ansible project is currently staged at `/home/df-server/lab-infra`. The requested `/lab-infra` path requires root access. After review, it can be moved with:

```bash
sudo mv "$HOME/lab-infra" /lab-infra
sudo chown -R df-server:df-server /lab-infra
```

All project paths are relative, so either location works.

## Project layout

```text
lab-infra/
├── .gitignore
├── README.md
├── ansible.cfg
├── requirements.yml
├── docs/
│   ├── ansible-intall.md
│   └── ansibleconfig.md
├── inventory/
│   ├── README.md
│   ├── hosts.yml
│   ├── tailscale-peers.example.yml
│   ├── group_vars/all.yml
│   └── host_vars/server-debian.yml
├── playbooks/
│   ├── audit.yml
│   └── site.yml
├── roles/
│   ├── common/
│   └── garage/
├── state/
│   └── README.md
└── site.yml
```

The root `site.yml` is currently empty. Use `playbooks/site.yml` for the active site playbook.

## Quick start

```bash
cd /home/df-server/lab-infra   # use /lab-infra after moving it
export PATH="$HOME/.local/bin:$PATH"

ansible --version
ansible-inventory --graph
ansible lab_local -m ansible.builtin.ping
```

Capture a read-only snapshot of the current host:

```bash
ansible-playbook playbooks/audit.yml
```

The audit records Ansible facts, installed packages, systemd services, and Tailscale status under `state/`. Generated state files contain machine-specific information and are ignored by Git by default.

## Playbooks and roles

### `playbooks/audit.yml`

The audit playbook gathers the current host state and writes the following files:

- `state/server-debian-facts.json`
- `state/server-debian-packages.json`
- `state/server-debian-services.json`
- `state/tailscale-status.txt`
- `state/audit-meta.yml`

These files are snapshots, not the desired configuration.

### `playbooks/site.yml`

This playbook currently includes:

- `common` — verifies and reports the Debian host; it is audit-only
- `garage` — state-changing role that downloads Garage, installs a systemd service, creates directories, and starts the service

The Garage role requires sudo and should not be run until its secret handling, checksum, service user, firewall exposure, and initial Garage layout have been reviewed.

Use syntax and check-mode validation before applying changes:

```bash
ansible-playbook playbooks/site.yml --syntax-check
ansible-playbook playbooks/site.yml --check --diff
```

The Garage role currently requires an interactive sudo password when run against this host:

```bash
ansible-playbook -K playbooks/site.yml
```

### `roles/common`

The common role currently:

- Confirms the host belongs to the Debian family
- Reports the host, OS, kernel, and Python information
- Performs no package, service, user, firewall, or network changes

### `roles/garage`

The Garage role is an initial deployment draft for a single-node Garage service. Before production use, add or verify:

- Ansible Vault or another secure source for `garage_rpc_secret`
- An official binary checksum
- A dedicated `garage` system user and group
- Appropriate service hardening
- Firewall and bind-address restrictions
- Garage layout initialization
- Access keys, buckets, and permissions

## Inventory and Tailscale

`inventory/hosts.yml` intentionally contains only this machine as an active Ansible target. Remote Tailscale peers are available as a non-loaded template in `inventory/tailscale-peers.example.yml`; operating-system usernames and SSH policy must be verified before enabling them.

Observed Tailscale nodes:

- `server-debian` — `100.102.154.23` — this host
- `donatello` — `100.94.145.104` — active
- `inkii` — `100.98.125.127` — offline when checked
- `naruto-dell-pro-max-14-mc14250` — `100.88.92.78`
- `raphael` — `100.116.35.103` — active

## Git workflow

The local Git repository tracks the Ansible configuration and documentation. The remote is:

```text
git@github.com:GnomeServer/TheGarden.git
```

Check the working tree:

```bash
git status
git log --oneline --decorate -5
```

Commit configuration changes explicitly:

```bash
git add ansible.cfg inventory playbooks roles docs README.md requirements.yml .gitignore
git commit -m "Describe the change"
git push
```

Generated state snapshots are ignored because they contain hostnames, addresses, package inventories, and service details. Review sensitive infrastructure information before publishing any files.

## What is not managed yet

- SSH keys, credentials, or vault passwords
- Remote-host configuration for the worker laptops
- Firewall rules
- Tailscale ACLs, routes, or DNS settings
- A complete package or service desired-state policy
- Garage layout, buckets, and access keys
- Kubernetes or high-availability storage
