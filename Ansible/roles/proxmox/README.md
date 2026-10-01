# proxmox role

This role validates the existing Proxmox VE installation on `server-debian`.
It is deliberately read-only: it does not install Proxmox, change APT
repositories, edit networking, restart services, or reboot the host.

The in-place conversion from Debian 13 to Proxmox VE 9 was performed by a
host-local staged installation script and is documented in
[`proxmox-installation.md`](../../../Proxmox-Documents/proxmox-installation.md).

## Checks

The role verifies:

- Proxmox VE 9 is installed using `pveversion --verbose`.
- A Proxmox kernel is running.
- `vmbr0` owns `10.1.10.156` and is the default IPv4 interface.
- The PVE cluster and management services are running.

Run the validation with:

```bash
cd TheGarden/Ansible
ansible-playbook playbooks/proxmox-audit.yml
```
