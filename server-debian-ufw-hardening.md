# server-debian — Network Hardening (UFW + Tailscale)

**Date:** 2026-09-21
**Host:** `server-debian` (100.102.154.23) — designated Laptop 1 / data-plane + control-plane server
**Performed from:** `donatello` via `ssh df-server@server-debian` (Tailscale SSH)

## Context

Per the lab README, `server-debian` was previously marked **offline** and UFW was
**disabled** with no firewall rules in place. Before installing the Docker Compose
stack (Forgejo, PostgreSQL, Garage S3, Conduit+Element, Open WebUI, llama.cpp), the
server needed to be reachable, confirmed as the correct node, and locked down to
tailnet-only access.

## Findings

### 1. Server reachability confirmed

`tailscale status` on `donatello` showed `server-debian` (100.102.154.23) with no
`offline` flag (unlike `inkii`, which showed `offline, last seen 2d ago`). A
`tailscale ping server-debian` followed by `ssh df-server@server-debian` succeeded,
confirming the node is alive and reachable — resolving blocker #1 from the README
("server laptop identity unknown").

### 2. Pre-hardening port audit

`sudo ss -tulpn` on `server-debian` showed:

| Port | Service | Bound to | Notes |
|---|---|---|---|
| 631 | `cupsd` (printing) | `127.0.0.1` / `::1` only | Already localhost-only, no exposure |
| 5353 | `avahi-daemon` (mDNS) | `0.0.0.0` / `::` | LAN discovery, low sensitivity |
| 41641, 35287, 39219 | `tailscaled` | tailnet interfaces | Tailscale's own control traffic |

**No services were found bound to `0.0.0.0` on a sensitive port.** The box was not
actively exposed at audit time, but had no firewall enforcing that going forward.

### 3. No traditional SSH daemon

`sudo systemctl status ssh` → *"Unit ssh.service could not be found."*
`which sshd` → no output.

This confirmed the server has **no standalone `sshd`**. SSH access is provided by
**Tailscale SSH**, which handles the protocol inside `tailscaled` itself rather than
a conventional listening socket on port 22. This meant:

- A standard "allow port 22" UFW rule is not the relevant control.
- Access is gated by the tailnet interface / Tailscale ACLs, not a local port rule.
- Because there is no fallback SSH path, firewall changes had to be verified with a
  **second, independent session** before closing the first — to avoid a hard lockout.

## Steps taken

Run on `server-debian`:

```bash
# 1. Install UFW (not previously installed)
sudo apt update
sudo apt install ufw -y

# 2. Set default-deny posture
sudo ufw default deny incoming
sudo ufw default allow outgoing

# 3. Trust the entire tailnet (interface + CIDR, belt and suspenders)
sudo ufw allow in on tailscale0
sudo ufw allow from 100.64.0.0/10

# 4. Enable
sudo ufw enable
sudo ufw status verbose
```

**Verification (critical step):** before closing the original SSH session, a second,
independent `ssh df-server@server-debian` session was opened from another tailnet
node to confirm Tailscale SSH still worked post-enable. Only after that succeeded was
the original session considered safe to close.

## Why `100.64.0.0/10`, not per-host rules

`100.64.0.0/10` is Tailscale's entire allocated address range for this tailnet. A
single CIDR rule covers every current and future node on `GnomeServer` without
per-machine entries:

| Node | Tailnet IP |
|---|---|
| server-debian | 100.102.154.23 |
| donatello | 100.94.145.104 |
| inkii | 100.98.125.127 |
| naruto-dell-pro-max-14-mc14250 | 100.88.92.78 |
| raphael | 100.116.35.103 |

This matches the lab's internal-trust model for a fast conceptual demo: all five
laptops need to reach `server-debian` (SSH now; Forgejo, Postgres, Garage S3, Open
WebUI, and llama.cpp once the compose stack is up). Any future laptop added to the
same tailnet inherits access automatically — no rule changes needed.

## Deferred / to revisit

- **Per-service scoping** — right now the CIDR rule grants all five machines access
  to all ports on `server-debian`. If this moves past demo stage, consider narrowing
  so only Laptop 1's admin reaches Postgres/Garage S3 directly, while worker laptops
  only reach the RPC server port.
- **Bind services to tailnet IP, not `0.0.0.0`** — when Forgejo, Postgres, Garage S3,
  Conduit+Element, Open WebUI, and llama.cpp are installed, each must be explicitly
  bound to `100.102.154.23` (or `tailscale0`) rather than the default `0.0.0.0`, so
  UFW is a backstop rather than the only control.
- **`avahi-daemon` (mDNS, 5353/UDP)** — currently open on `0.0.0.0`/`::`. Low risk,
  but worth deciding whether it's needed on this box or can be disabled.

## Next step

With `server-debian` reachable and locked to the tailnet, the next item in the build
plan is:

```bash
sudo apt install docker-compose-v2
```

...to begin standing up the data-plane stack (Forgejo, PostgreSQL, Garage S3,
Conduit+Element, Open WebUI, llama.cpp) bound to the tailnet IP only.
