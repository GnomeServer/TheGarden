# Ansible Configuration and Lab Infrastructure Setup

**Created:** 2026-09-21  
**Machine:** `server-debian`  
**User:** `df-server`

This document records the Ansible installation and the audit-first infrastructure project created on this machine.

## 1. Ansible installation

The lightweight Ansible distribution, `ansible-core`, was installed instead of the larger `ansible` community bundle.

| Item | Value |
| --- | --- |
| Package | `ansible-core` |
| Version | `2.21.4` |
| Operating system | Debian GNU/Linux 13.7 (`trixie`) |
| Python | `3.13.5` |
| Ansible virtual environment | `/home/df-server/.local/share/ansible-core-venv` |
| CLI directory | `/home/df-server/.local/bin` |
| Installation scope | Current user only |

A user-local virtual environment was used because the current SSH session did not have a usable non-interactive `sudo` session; `sudo` required a password. No system Python packages or system package database were modified.

The Ansible commands exposed in `/home/df-server/.local/bin` are:

```text
ansible
ansible-config
ansible-console
ansible-doc
ansible-galaxy
ansible-inventory
ansible-playbook
ansible-pull
ansible-test
ansible-vault
```

For a shell that does not already include the directory in `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

The existing login profile already includes `$HOME/.local/bin` for login shells.

## 2. Lab project location

The repository is currently staged at `/home/df-server/lab-infra`, with the Ansible project under:

```text
/home/df-server/lab-infra/Ansible
```

The requested `/lab-infra` path requires root privileges. After reviewing the repository, move it from an interactive SSH shell with:

```bash
sudo mv "$HOME/lab-infra" /lab-infra
sudo chown -R df-server:df-server /lab-infra
```

After moving it, the Ansible project root is `/lab-infra/Ansible`. The Ansible configuration uses relative paths and works from either project location.

## 3. Project structure

```text
lab-infra/
├── Ansible/
│   ├── ansible.cfg
│   ├── requirements.yml
│   ├── inventory/
│   │   ├── README.md
│   │   ├── hosts.yml
│   │   ├── proxmox-guests.example.yml
│   │   ├── tailscale-peers.example.yml
│   │   ├── group_vars/all.yml
│   │   └── host_vars/server-debian.yml
│   ├── playbooks/
│   │   ├── audit.yml
│   │   ├── proxmox-audit.yml
│   │   └── site.yml
│   ├── roles/
│   │   ├── common/
│   │   └── proxmox/
│   └── state/
│       └── README.md
├── docs/
│   ├── ansibleconfig.md
│   └── ansible-intall.md
└── README.md
```

The duplicate Markdown files previously under `Ansible/` were removed. The canonical copies remain in `docs/`.

## 4. Ansible configuration

`Ansible/ansible.cfg` configures:

- `Ansible/inventory/hosts.yml` as the default inventory
- `Ansible/roles/` as the role search path
- `collections/` as the collection path
- Automatic Python interpreter discovery
- SSH host-key checking enabled
- Ten parallel forks
- A 30-second connection timeout
- SSH pipelining enabled
- Retry files disabled
- Deprecation warnings enabled

The active inventory contains the current machine only. It belongs to both the general local group and the Proxmox-specific group:

```yaml
all:
  children:
    lab_local:
      hosts:
        server-debian:
          ansible_connection: local
          ansible_python_interpreter: /usr/bin/python3
    proxmox_hosts:
      children:
        lab_local:
```

The current Tailscale peers are listed in `Ansible/inventory/tailscale-peers.example.yml`, but that file is intentionally not loaded by default. Remote operating-system usernames and SSH permissions have not been verified.

A Debian guest VM was created on the Proxmox node as **VMID 100**. Its hostname is `infra-lab-services`, and its `ens18` address is `10.1.10.2/24`. Its connection template is `Ansible/inventory/proxmox-guests.example.yml`. It remains inactive because SSH has not been installed in the guest yet. The Proxmox host inventory records the VM under `proxmox_guest_vms`.

## 5. Current machine captured

| Item | Observed value |
| --- | --- |
| Hostname | `server-debian` |
| OS | Proxmox VE 9.2 on Debian GNU/Linux 13.7 (`trixie`) |
| Kernel | `7.0.14-19-pve` |
| Architecture | `x86_64` |
| Hardware | Dell Pro Max 14 MC14250 |
| LAN address | `10.1.10.156` |
| Tailscale address | `100.102.154.23` |
| Installed Debian/Proxmox packages | observed by the latest audit |
| Proxmox bridge | `vmbr0` (`10.1.10.156/24`) |
| Discovered systemd services | observed by the latest audit |

From the guest console, install SSH and Python:

```bash
sudo apt update
sudo apt install -y openssh-server python3
sudo systemctl enable --now ssh
```

After setting the Debian login account in the example inventory, test it with:

```bash
ansible infra-lab-services \\
  -i inventory/hosts.yml \\
  -i inventory/proxmox-guests.example.yml \\
  -m ansible.builtin.ping
```

The Proxmox host and guest are on `10.1.10.0/24`. Confirm that the guest address is reserved and reachable.

The observed Tailscale nodes were:

- `server-debian` — `100.102.154.23` — this machine
- `donatello` — `100.94.145.104` — active
- `inkii` — `100.98.125.127` — offline when checked
- `naruto-dell-pro-max-14-mc14250` — `100.88.92.78`
- `raphael` — `100.116.35.103` — active

## 6. Playbooks and role

### `Ansible/playbooks/site.yml`

Runs the audit-only `common` role against `lab_local` and the read-only `proxmox` validation role against `proxmox_hosts`.

### `Ansible/playbooks/proxmox-audit.yml`

Validates the completed Proxmox VE installation without making changes. It checks the Proxmox version, running kernel, `vmbr0` management address, default route interface, and core PVE services:

```bash
cd /home/df-server/lab-infra/Ansible
ansible-playbook playbooks/proxmox-audit.yml
```

### `playbooks/audit.yml`

Captures the current state by collecting:

- Ansible host facts
- Installed package facts
- systemd service facts
- Tailscale peer status
- Proxmox `pveversion --verbose` output
- Audit timestamp and host metadata

Run it with:

```bash
cd /home/df-server/lab-infra/Ansible
export PATH="$HOME/.local/bin:$PATH"
ansible-playbook playbooks/audit.yml
```

### `roles/common`

The common role currently:

- Confirms the machine belongs to the Debian family used by Proxmox VE
- Reports host, OS, kernel, and Python information
- Explicitly confirms that no mutation tasks are enabled

### `roles/proxmox`

The Proxmox role is a read-only post-installation validator. It confirms that Proxmox VE 9 is installed, a PVE kernel is running, `vmbr0` owns `10.1.10.156/24`, and the PVE management services are running.

The in-place installation itself was performed by `/home/df-server/install-pve-inplace.sh` in two stages. Ansible does not repeat that conversion because it changes the active network configuration and requires controlled reboots. The procedure is documented in `proxmox-installation.md`.

## 7. Captured state files

The audit generated these files under `Ansible/state/`:

- `server-debian-facts.json` — full Ansible facts
- `server-debian-packages.json` — installed package facts
- `server-debian-services.json` — systemd service facts
- `tailscale-status.txt` — Tailscale status at audit time
- `proxmox-version.txt` — Proxmox `pveversion --verbose` output
- `audit-meta.yml` — audit timestamp and summary

These files contain machine-specific information such as addresses, hostnames, package names, and service names. The supplied `.gitignore` excludes the generated state files by default.

## 8. Validation performed

The project passed the following checks:

```bash
ansible-config dump --only-changed
ansible-inventory --graph
ansible-inventory --list
ansible-playbook playbooks/site.yml --syntax-check
ansible-playbook playbooks/audit.yml --syntax-check
ansible-playbook playbooks/proxmox-audit.yml --syntax-check
ansible-playbook playbooks/site.yml --check --diff
ansible-playbook playbooks/proxmox-audit.yml
ansible lab_local -m ansible.builtin.ping
```

The local ping test returned:

```text
server-debian | SUCCESS => {
    "changed": false,
    "ping": "pong"
}
```

The check-mode playbook completed with `changed=0`.

## 9. Normal commands

```bash
cd /home/df-server/lab-infra/Ansible   # use /lab-infra/Ansible after moving it
export PATH="$HOME/.local/bin:$PATH"

# Show the inventory
ansible-inventory --graph

# Test local connectivity
ansible lab_local -m ansible.builtin.ping

# Run the read-only baseline and Proxmox validation roles
ansible-playbook playbooks/site.yml

# Validate only Proxmox
ansible-playbook playbooks/proxmox-audit.yml

# Refresh the current-state snapshot
ansible-playbook playbooks/audit.yml

# Review potential changes before enabling any mutating tasks
ansible-playbook playbooks/site.yml --check --diff
```

## 10. Not performed

The following were not changed or created:

- System-wide Ansible installation
- System Python configuration
- SSH keys or credentials
- Ansible Vault passwords or secrets
- Remote hosts
- Firewall rules
- Tailscale ACLs, routes, or DNS settings
- Community Ansible collections
- A package or service desired-state policy

The project is intentionally a safe starting point for documenting and gradually managing the lab infrastructure.
