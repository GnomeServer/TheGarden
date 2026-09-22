# Inventory

`hosts.yml` defines the two-node `garage_cluster` and retains `lab_local` as a
site-playbook alias. `server-debian` uses the local connection; `donatello` is
reached over its Tailscale address as `bgurrol4`.

Before adding another Garage node, verify:

1. Its current Tailscale address or MagicDNS name.
2. Its operating-system username and SSH authorization.
3. Its Python interpreter path.
4. Its storage capacity and failure-domain/zone.
5. That the full Garage play can be run against every cluster member.

Garage layout changes are intentionally blocked when the play is run with
`--limit`. Use host-specific variables under `host_vars/` for values that
differ between machines; do not put passwords, private keys, or tokens in
inventory files.
