# Proxmox VE Installation

This document records the in-place Proxmox VE installation performed on the Debian host `server-debian`.

## Installation overview

- **Source OS:** Debian 13 (Trixie)
- **Target:** Proxmox VE 9
- **Hostname:** `server-debian`
- **FQDN:** `server-debian.home.arpa`
- **Management IP:** `10.1.10.156/24`
- **Gateway:** `10.1.10.1`
- **DNS servers:** `75.75.75.75`, `75.75.76.76`
- **Physical network interface:** `enp0s31f6`
- **Proxmox bridge:** `vmbr0`
- **Proxmox web interface:** <https://10.1.10.156:8006>
- **Repository:** Proxmox VE 9 `pve-no-subscription`

The installation was performed in two stages using [`install-pve-inplace.sh`](./install-pve-inplace.sh). The existing Debian kernel was retained as a fallback.

## Stage 1: Prepare Debian and install the Proxmox kernel

Run this stage from the local console or another session with recovery access. The network configuration is changed during the process.

```bash
sudo bash /home/df-server/install-pve-inplace.sh stage1
```

Stage 1 performs the following actions:

1. Verifies that the host is running Debian 13 Trixie with UEFI boot.
2. Creates a root-owned configuration backup under `/root/pve-inplace-backup-<timestamp>/`.
3. Configures the hostname and `/etc/hosts`.
4. Converts the active NetworkManager connection to the static address `10.1.10.156/24`.
5. Disables third-party Docker and Tailscale APT repositories during the conversion.
6. Adds the Proxmox VE 9 archive and verifies its signing-key checksum.
7. Updates Debian and installs `proxmox-default-kernel` and `ifupdown2`.
8. Updates GRUB.
9. Prompts for confirmation before rebooting.

After rebooting, verify that the system is running the Proxmox kernel:

```bash
uname -r
```

The output should contain `pve`.

## Stage 2: Install Proxmox VE and configure networking

After confirming that the Proxmox kernel is running, execute:

```bash
sudo bash /home/df-server/install-pve-inplace.sh stage2
```

Stage 2:

1. Installs `proxmox-ve`, `postfix`, `open-iscsi`, `chrony`, and `ifupdown2`.
2. Replaces NetworkManager management with the Proxmox-managed `vmbr0` bridge.
3. Assigns the host address and default route to `vmbr0`.
4. Configures DNS and the `home.arpa` search domain.
5. Enables the Proxmox cluster and management services.

The bridge configuration is equivalent to:

```text
iface enp0s31f6 inet manual

auto vmbr0
iface vmbr0 inet static
    address 10.1.10.156/24
    gateway 10.1.10.1
    bridge-ports enp0s31f6
    bridge-stp off
    bridge-fd 0
```

## Post-installation checks

Open the Proxmox administration interface:

```text
https://10.1.10.156:8006
```

Useful checks from the host are:

```bash
ip -br addr
ip route
pvesm status
systemctl status pveproxy pvedaemon pvestatd
```

Before removing the fallback Debian kernel, verify Proxmox, networking, storage, and at least one additional reboot.

## Refresh the SSH host key on clients

When the server is reinstalled or its SSH host key changes, run the following command on each SSH client. It removes the stale `server-debian` entry and appends the current Ed25519 host key to `~/.ssh/known_hosts`:

```bash
ssh-keygen -R server-debian && ssh-keyscan -t ed25519 server-debian >> ~/.ssh/known_hosts
```

This command refreshes the server's **host-key entry** on the client; it does not generate the client's personal SSH key pair. Verify the fingerprint through a trusted channel before accepting a new host key.
