# Garage S3 Ansible Configuration

**Status:** Ansible configuration updated; deployment still requires an interactive sudo password.

## Overview

The Ansible project now manages a two-node Garage S3 cluster:

| Host | Tailscale address | Garage zone | Capacity | Role |
| --- | --- | --- | --- | --- |
| `server-debian` | `100.102.154.23` | `lab-server` | `350G` | Controller and storage node |
| `donatello` | `100.94.145.104` | `lab-donatello` | `350G` | Storage node |

The cluster uses:

- Garage `v2.4.1`
- Replication factor `2`
- Consistent mode
- SQLite metadata, retained for safe in-place deployment of the existing nodes
- Tailscale addresses for Garage RPC and S3 traffic
- A local-only admin API on `127.0.0.1:3903`
- Path-style S3 access because no DNS root domain is configured

A two-node cluster stores two copies of data, but it cannot continue normal
writes while one node is unavailable. Garage recommends at least three nodes
for a highly available production cluster.

## Inventory changes

`inventory/hosts.yml` now defines a `garage_cluster` group containing both
hosts. The existing `lab_local` group remains as an alias for the site
playbook.

Host-specific Garage settings are stored in:

- `inventory/host_vars/server-debian.yml`
- `inventory/host_vars/donatello.yml`

The shared cluster settings are in:

- `inventory/group_vars/garage_cluster.yml`

Each storage node explicitly defines its Garage zone, capacity, and tag. The
role validates these values before making changes.

## Garage role changes

The role in `roles/garage/` now performs the following operations.

### Binary installation

- Downloads the official Garage static binary to the Ansible control node.
- Uses an architecture-specific release path.
- Verifies the binary with SHA-256 before installing it.
- Caches the binary under:

  ```text
  ~/.cache/ansible-garage/
  ```

- Installs it as `/usr/local/bin/garage` with root ownership.

The current checksum is defined for the x86_64 `v2.4.1` release in
`roles/garage/defaults/main.yml`. If the Garage version is changed, its
corresponding checksum must also be updated.

### Secrets

The role creates persistent control-node secrets under:

```text
~/.ansible-secrets/
```

The generated files are:

- `garage_rpc_secret` — shared by all Garage nodes
- `garage_admin_token` — protects administrative API calls

The secrets are copied to each host as mode `0600` files and are referenced by
path from `/etc/garage.toml`. Secret contents are not stored in Git or printed
in Ansible output.

### Service security

Garage runs as the unprivileged `garage` system user and group. The systemd
unit includes:

- `NoNewPrivileges=true`
- `ProtectHome=true`
- `ProtectSystem=full`
- Restricted writable paths
- A private temporary directory
- A restrictive umask
- Increased `LimitNOFILE`
- Automatic restart on failure

Garage metadata and data directories are owned by the `garage` service user.
The admin API is bound to localhost and protected by the generated admin
token.

### Cluster initialization

When `garage_manage_layout` is enabled, the role:

1. Starts Garage on every inventory host.
2. Reads each node's full Garage node identifier.
3. Connects all nodes through the first host in `garage_cluster`.
4. Stages the configured zone, capacity, and tag for each node.
5. Reads the current layout version.
6. Applies exactly one new layout version only when staged changes exist.
7. Runs `garage health` and waits for a healthy cluster.

Layout operations are stateful and must be run against the complete cluster.
The role deliberately fails if the Garage play is invoked with `--limit` while
layout management is enabled.

## Playbooks

Use the dedicated Garage playbook for deployment:

```bash
cd /home/df-server/lab-infra/Ansible
ansible-playbook playbooks/garage.yml --ask-become-pass
```

The existing site playbook also includes the Garage role through the
`lab_local` alias:

```bash
ansible-playbook playbooks/site.yml --ask-become-pass
```

Use check mode to review changes without starting services or applying cluster
layout operations:

```bash
ansible-playbook playbooks/garage.yml --check --diff --ask-become-pass
```

## S3 access

The S3 API is available over Tailscale. The primary endpoint is:

```text
http://100.102.154.23:3900
```

No application keys or buckets are created automatically. After deployment,
create them manually on the coordinator or manage them separately with Ansible
Vault or another secret manager. For example:

```bash
sudo garage -c /etc/garage.toml key create my-app
sudo garage -c /etc/garage.toml bucket create my-bucket
sudo garage -c /etc/garage.toml bucket allow --read --write --owner \
  my-bucket --key my-app
```

The returned access-key secret must be stored securely and must not be added to
inventory or templates in plaintext.

## Database engine note

The existing Garage deployment was initialized with SQLite metadata. The role
therefore keeps:

```toml
db_engine = "sqlite"
```

Garage recommends LMDB for higher-performance replicated clusters, but changing
the database engine is a stateful migration. Any future SQLite-to-LMDB change
must be performed as a separate maintenance operation using Garage's
`convert-db` procedure and a verified backup.

## Current deployment state

The configuration changes have been validated with:

- Ansible syntax checks for the Garage, site, and audit playbooks
- Inventory graph and variable resolution
- Ansible connectivity checks to both cluster hosts
- Garage CLI simulations for node connection, layout assignment, layout apply,
  replication, and health checks
- Generated systemd unit validation

The live deployment was not applied during the configuration update because
both hosts require an interactive sudo password. Before deployment, the
existing `server-debian` Garage service is running but has no applied cluster
layout, so its health endpoint reports that quorum is unavailable. Running the
Garage playbook above will install the new configuration and reconcile the
cluster layout.

## Network requirements

The role does not manage host firewall rules. Ensure the network policy allows:

- TCP `3901` between Garage nodes over Tailscale
- TCP `3900` from intended S3 clients over Tailscale
- TCP `3903` only on localhost, as configured by the role
