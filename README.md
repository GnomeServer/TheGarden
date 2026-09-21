# lab-infra

Ansible project for the current lab machine and its Tailscale-connected infrastructure.

## Current status

This project was created from the machine currently being managed:

| Item | Observed value |
| --- | --- |
| Host | `server-debian` |
| OS | Debian GNU/Linux 13 (`trixie`) |
| Architecture | `x86_64` |
| LAN address | `10.1.10.156` |
| Tailscale address | `100.102.154.23` |
| Hardware | Dell Pro Max 14 MC14250 |
| Local Ansible | `ansible-core 2.21.4` |

The project is **audit-first**. It describes the current host and captures its state, but the default playbook does not change packages, services, users, networking, or files. A desired state should be reviewed and added deliberately before enabling changes.

## Why it is currently under `$HOME`

The requested `/lab-infra` path is owned by root and the current SSH session cannot use `sudo` without an interactive password. The project is therefore staged at:

```text
/home/df-server/lab-infra
```

After reviewing it, move it to the requested path with an interactive shell:

```bash
sudo mv "$HOME/lab-infra" /lab-infra
sudo chown -R df-server:df-server /lab-infra
```

All paths in the Ansible configuration are relative, so the project works from either location.

## Project layout

```text
lab-infra/
├── .gitignore
├── README.md
├── ansible.cfg
├── requirements.yml
├── inventory/
│   ├── README.md
│   ├── hosts.yml
│   ├── tailscale-peers.example.yml
│   ├── group_vars/
│   │   └── all.yml
│   └── host_vars/
│       └── server-debian.yml
├── playbooks/
│   ├── audit.yml
│   └── site.yml
├── roles/
│   └── common/
│       ├── README.md
│       ├── defaults/main.yml
│       ├── handlers/main.yml
│       ├── meta/main.yml
│       ├── tasks/main.yml
│       ├── templates/.gitkeep
│       └── vars/main.yml
└── state/
    └── README.md
```

## Quick start

From the project directory:

```bash
cd /home/df-server/lab-infra   # use /lab-infra after moving it
export PATH="$HOME/.local/bin:$PATH"

ansible-inventory --graph
ansible-playbook playbooks/site.yml
ansible-playbook playbooks/audit.yml
```

`audit.yml` gathers and saves a current snapshot under `state/`. It records Ansible facts, installed package facts, systemd service facts, and the available Tailscale status. These files can contain hostnames, addresses, package names, and service details; review them before committing the project to a public repository.

Use check mode when reviewing future changes:

```bash
ansible-playbook playbooks/site.yml --check --diff
```

## Inventory and Tailscale

`inventory/hosts.yml` intentionally contains only this machine as an active Ansible target. The current Tailscale peer list is also available as a non-loaded template in `inventory/tailscale-peers.example.yml`; it is not enabled as remote inventory because the operating-system usernames and SSH policy of those peers have not been verified. The audit playbook captures the live peer status in `state/tailscale-status.txt`.

The observed Tailscale peers at creation time were:

- `server-debian` — `100.102.154.23` — this host
- `donatello` — `100.94.145.104` — active
- `inkii` — `100.98.125.127` — offline when checked
- `naruto-dell-pro-max-14-mc14250` — `100.88.92.78`
- `raphael` — `100.116.35.103` — active

To add a peer later, first confirm SSH access and the remote OS user, then add it to a separate inventory group instead of assuming that the local `df-server` account exists on the peer.

## What is and is not managed

Currently represented:

- Local host identity and connection settings
- Debian 13 platform details
- The existing Ansible controller setup
- Tailscale address and observed peer information
- A repeatable read-only audit of facts, packages, services, and Tailscale status

Not created or changed:

- SSH keys, credentials, or vault passwords
- `ansible.cfg` on the system
- Remote-host configuration
- Firewall rules
- Package or service desired state
- Tailscale ACLs, routes, or DNS settings
- Community Ansible collections

The `common` role is deliberately a safe skeleton. Add concrete tasks only after defining the intended lab baseline.

## Useful commands

```bash
# Check the controller and inventory
ansible --version
ansible-inventory --graph
ansible-inventory --list

# Run a local connectivity check
ansible lab_local -m ansible.builtin.ping

# Capture a fresh baseline
ansible-playbook playbooks/audit.yml

# See the files produced by the last audit
find state -maxdepth 1 -type f -printf '%f\n' | sort
```
