# Inventory

`hosts.yml` contains the current Proxmox VE host as the only active target. It uses the local connection and the system Python interpreter, so the project can audit this host without making an SSH connection back into itself. The host is in both `lab_local` and `proxmox_hosts`; the latter selects Proxmox-specific validation.

Remote Tailscale peers are not active inventory entries yet. Their observed names and addresses are recorded by `playbooks/audit.yml` in `state/tailscale-status.txt`. Before adding a peer, verify:

1. Its current Tailscale address or MagicDNS name.
2. Its operating-system username.
3. SSH key and/or Tailscale SSH authorization.
4. Its Python interpreter path.
5. Which playbooks are safe to run against it.

A Debian guest VM was created on `server-debian` as **VMID 100**. Its hostname is `infra-lab-services`, with `ens18` configured as `10.1.0.2/24`. It is documented in `inventory/proxmox-guests.example.yml`, but is not active until SSH is installed and the Debian operating-system user is configured.

From the Debian VM console, install and enable SSH and Python:

```bash
sudo apt update
sudo apt install -y openssh-server python3
sudo systemctl enable --now ssh
```

Set `ansible_user` in the example inventory to the Debian login account, then test the guest with:

```bash
ansible-inventory -i inventory/hosts.yml -i inventory/proxmox-guests.example.yml --graph
ansible infra-lab-services \\
  -i inventory/hosts.yml \\
  -i inventory/proxmox-guests.example.yml \\
  -m ansible.builtin.ping
```

The Proxmox host is on `10.1.10.0/24`, while this guest is on `10.1.0.0/24`. Confirm that this separate subnet and its gateway are intentional and routable before troubleshooting SSH.

Use host-specific variables under `host_vars/` for values that differ between machines; do not put passwords, private keys, or tokens in inventory files.
