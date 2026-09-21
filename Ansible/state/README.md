# Captured state

Run the audit playbook from the project root to refresh this directory:

```bash
ansible-playbook playbooks/audit.yml
```

Generated files:

- `server-debian-facts.json` — Ansible facts for the host
- `server-debian-packages.json` — installed package facts
- `server-debian-services.json` — systemd service facts
- `tailscale-status.txt` — Tailscale peer status at audit time
- `audit-meta.yml` — timestamp and summary metadata

These files are machine-specific and may contain IP addresses, hostnames, package inventories, and service information. They are ignored by the supplied `.gitignore` by default; remove the relevant ignore rules only if the state is intentionally meant to be versioned.
