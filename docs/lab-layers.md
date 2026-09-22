# Lab Architecture Layers

This document explains the lab in two ways:

1. **Functional layers** — what the lab does.
2. **Security and network layers** — how access is restricted.

These are not strict OSI layers. They are practical defense-in-depth boundaries for this pilot.

## 1. Functional layers

```text
Human
  │
  ▼
Mission interface
Open WebUI + Caddy
  │
  ▼
Agent execution
Agent Control + NATS + llama-server + DF Workers
  │
  ▼
Evidence and system records
PostgreSQL + Forgejo + Prometheus/Grafana
  │
  ▼
Backup
Restic + external USB disk
```

### Mission interface

This is where a person interacts with the lab:

- Open WebUI
- Caddy HTTPS front door
- Required mission-intake fields
- Run number and links returned to the user
- Human review and mission decisions

A user should not connect directly to PostgreSQL, NATS, Docker, or Agent Control internals.

### Agent execution

This is where work happens:

- Agent Control contains the Planner and Supervisor.
- NATS carries task messages.
- `llama-server` provides model inference.
- DF Workers receive tasks.
- Sandboxes run generated code.
- Workers push branches and evidence to Forgejo.

Agents do not communicate directly with one another. They communicate through Agent Control, NATS, and Forgejo.

### Records, evidence, and visibility

Each system has a defined responsibility:

| System | Responsibility |
|---|---|
| PostgreSQL | Runs, tasks, events, workers, retries, and decisions |
| Forgejo | Source code, branches, pull requests, evidence, and approvals |
| Prometheus | Time-series health metrics |
| Grafana | Dashboards and operator views |
| Restic | Encrypted backups |

NATS is a queue, not the permanent record of truth.

## 2. Physical and virtualization layers

```text
Physical Ethernet switch
        │
        ▼
Physical server laptop: df-control-01
        │
        ├── Proxmox VE host
        │       ├── Tailscale: tag:control
        │       └── Node Exporter
        │
        ├── vmbr0 wired bridge
        │       └── infra-01 Ubuntu VM
        │               ├── Tailscale: tag:infra
        │               └── Docker Compose control services
        │
        └── No Docker, PostgreSQL, Forgejo, NATS, or agent code on the host
```

The current server has Debian installed. Installing Proxmox VE from the official ISO replaces that installation with the Debian-based Proxmox VE operating system. The final physical host is therefore a Proxmox host, not a general-purpose Debian server.

The five other physical laptops are bare-metal Ubuntu workers:

```text
df-worker-01 ... df-worker-05
        └── Ubuntu
            └── Docker
                └── DF Worker
                    └── sandbox containers
```

The final OS and placement are:

| Device | OS | Location |
|---|---|---|
| `df-control-01` | Proxmox VE, Debian-based | Physical server laptop |
| `infra-01` | Ubuntu Server 24.04 | VM on `df-control-01` |
| `df-worker-01`–`df-worker-05` | Ubuntu 24.04 | Five physical worker laptops |

The Proxmox host and `infra-01` are separate network identities. The VM gets its own Tailscale installation and Tailscale IP.

## 3. Network security layers

Traffic passes through several controls:

```text
Physical network
    ↓
Tailscale encrypted overlay and ACLs
    ↓
Host firewall
    ↓
Docker network and published ports
    ↓
Caddy or service listener
    ↓
Application authentication
    ↓
Database/NATS permissions
```

### Physical Ethernet

The switch and router provide connectivity. They are not the main authorization system.

Ethernet carries:

- Tailscale underlay traffic
- Proxmox and VM bridge traffic
- No intentionally public lab services

Lab services should be addressed through Tailscale IPs and ACLs.

### Tailscale ACLs

Tailscale answers:

> Which user or machine may reach which device and port?

Examples:

```text
Admins  -> df-control-01:8006
Users   -> infra-01:443
Workers -> infra-01:4222,8443
Users   -> df-worker-03:9000-9099
```

Tailscale ACLs control network reachability. They do not decide whether a user may merge a pull request or read a PostgreSQL table.

### UFW, Proxmox firewall, or host firewall

A host firewall answers:

> If traffic reaches this operating system, which ports may accept it?

The current Debian system was configured as a temporary pre-Proxmox baseline:

```text
default deny incoming
default allow outgoing
allow traffic arriving on tailscale0
```

That is reasonable for the current Debian laptop, but it is not the final Proxmox configuration. Installing Proxmox from the ISO replaces the current system and its UFW rules.

After Proxmox installation:

- The Proxmox host has its own host/firewall configuration.
- The `infra-01` Ubuntu VM has its own firewall.
- A firewall rule on the Proxmox host must not be assumed to secure every service inside the VM.
- VM traffic through `vmbr0` must be handled deliberately.

The broad rule below is redundant and overly permissive if used together with `allow in on tailscale0`:

```bash
sudo ufw allow from 100.64.0.0/10
```

If the current Debian system remains online before migration, remove that rule only after verifying a second Tailscale session. Do not make a firewall change from the only active administrative session.

### Docker and container networking

Docker answers:

> Which containers can communicate, and which ports are published outside Docker?

Most control services should stay on an internal Compose network:

```text
PostgreSQL <-> Forgejo
PostgreSQL <-> Agent Control
NATS       <-> Agent Control
NATS       <-> Workers
```

These connections do not need to be published to every laptop.

Only deliberately exposed services should be published:

```text
443    Caddy/Open WebUI
8443   Caddy/Forgejo
9443   Caddy/Grafana
4222   NATS, restricted to the Tailscale IP if required
```

PostgreSQL should not be published.

UFW alone is not sufficient to protect Docker-published ports because Docker can install firewall rules that bypass ordinary UFW processing. Use correct Compose bindings and, if necessary, Docker `DOCKER-USER` filtering.

## 4. Application and identity layers

Even when a device can reach a service, the service performs its own authorization.

| Layer | Control |
|---|---|
| Caddy | HTTPS and routing |
| Forgejo | Human accounts, bot accounts, and repository permissions |
| PostgreSQL | Separate database users and privileges |
| NATS | Separate users and subject permissions |
| Open WebUI | Individual user accounts |
| Grafana | Individual user accounts and dashboard permissions |
| Forgejo | Branch protection and human approval |

A worker may be allowed to reach Forgejo over HTTPS while still being unable to:

- Push to `main`
- Approve a pull request
- Merge code
- Read another bot's credentials

## 5. Agent sandbox layer

The trusted DF Worker starts containers. Generated code runs inside a more restricted sandbox:

```text
DF Worker, trusted
    │
    └── sandbox container, untrusted generated code
        ├── no network
        ├── not root
        ├── no Docker socket
        ├── limited CPU, memory, and processes
        ├── temporary workspace only
        └── deleted after the task
```

This is different from the worker's host firewall:

- UFW restricts access to the laptop.
- Docker isolation restricts the process.
- `--network none` restricts generated code.
- Forgejo permissions restrict what the worker bot can change.
- Human approval restricts what becomes an approved demo.

## 6. Role identity is separate from laptop identity

A physical laptop can be both an administrator's workstation and a worker host. Those are separate identities and permissions.

For example, `df-worker-04` may have:

```text
Physical identity: df-worker-04
Operating system: Ubuntu
Tailscale tag: tag:worker
Optional role tag: tag:security
DF Worker role: security
NATS credential: security
Forgejo bot: df-security
Human users: individual accounts
```

These identities must not be conflated.

A laptop being capable of running Ansible does not mean every DF Worker process on it receives Ansible credentials. A laptop being an administrator's workstation does not mean generated code running there receives administrator access.

Default worker assignments may be:

```text
df-worker-01: role=coder, model=true
df-worker-02: role=reviewer
df-worker-03: role=demo, spare_coder=true
df-worker-04: role=security
df-worker-05: role=spare
```

These are configuration defaults, not permanent physical restrictions. Any Ubuntu worker can be rebuilt and assigned another role.

## 7. Example request paths

### Administrator opens Proxmox

```text
Admin laptop
  → Tailscale ACL
  → df-control-01:8006
  → Proxmox web UI
```

This does not pass through Caddy, Docker, Open WebUI, or `infra-01`.

### User opens Open WebUI

```text
User laptop
  → Tailscale ACL
  → infra-01:443
  → Caddy
  → Open WebUI
  → llama-server on df-worker-01
```

The model call uses the worker's Tailscale IP and API key.

### Agent sends a task

```text
Agent Control in infra-01
  → NATS on infra-01
  → DF Worker on df-worker-02
  → fresh checkout from Forgejo
  → sandbox container
  → test report to Forgejo
  → result to NATS
  → state to PostgreSQL
```

### Worker tries to connect to PostgreSQL

```text
Worker
  → Tailscale ACL / VM firewall / no published port
  → connection fails
```

Workers should use Agent Control or Forgejo rather than connect directly to the database.

## 8. Simple analogy

| Building concept | Lab equivalent |
|---|---|
| Road | Ethernet |
| Private access road | Tailscale |
| Security guard deciding who may enter | Tailscale ACL |
| Locked doors | UFW or Proxmox firewall |
| Rooms | VMs and Docker networks |
| Door badges | Application accounts |
| Locked cabinets | PostgreSQL and NATS permissions |
| Isolated work room | Sandbox container |
| Manager signoff | Human Forgejo approval |
| Security camera and logbook | PostgreSQL, Forgejo, and Grafana |
| Off-site safe | Restic backup |
