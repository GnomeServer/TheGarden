# Inventory

`hosts.yml` contains the current machine as the only active target. It uses the local connection and the system Python interpreter, so the project can audit this host without making an SSH connection back into itself.

Remote Tailscale peers are not active inventory entries yet. Their observed names and addresses are recorded by `playbooks/audit.yml` in `state/tailscale-status.txt`. Before adding a peer, verify:

1. Its current Tailscale address or MagicDNS name.
2. Its operating-system username.
3. SSH key and/or Tailscale SSH authorization.
4. Its Python interpreter path.
5. Which playbooks are safe to run against it.

Use host-specific variables under `host_vars/` for values that differ between machines; do not put passwords, private keys, or tokens in inventory files.
