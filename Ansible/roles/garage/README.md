# garage role

This role installs the official Garage static binary, runs it as the unprivileged
`garage` system user, configures the S3/RPC/admin listeners, and reconciles the
cluster layout.

## Current lab topology

The active inventory contains two nodes:

- `server-debian` — zone `lab-server`, 350G Garage capacity
- `donatello` — zone `lab-donatello`, 350G Garage capacity

They use their Tailscale addresses for RPC and S3 traffic. The cluster uses a
replication factor of two and consistent mode. Two nodes provide a second copy
of data, but the cluster cannot continue writes while one node is unavailable;
Garage recommends at least three nodes for a highly available production
cluster.

The S3 API is path-style by default because no DNS root domain is configured.
For example, the endpoint on `server-debian` is:

```text
http://100.102.154.23:3900
```

The admin API is bound to `127.0.0.1:3903` and requires a generated token.
The RPC secret and admin token are generated once on the Ansible control node
under `~/.ansible-secrets/` and are installed with restrictive permissions on
every node. Do not commit those files.

## Deploy

Use the dedicated playbook from this directory:

```bash
ansible-playbook playbooks/garage.yml --ask-become-pass
```

The role connects all nodes through the first inventory host, assigns their
zones/capacities/tags, applies a new layout version only when staged changes
exist, and waits for `garage health` to succeed. Layout operations are
stateful Garage operations; do not use `--limit` with this play.

The current hosts have existing SQLite metadata, so the role deliberately keeps
`garage_db_engine: sqlite` for a safe in-place rollout. Migrating to LMDB should
be a separate, backed-up maintenance operation using Garage's `convert-db`
procedure.

## S3 credentials

The role does not invent or commit application credentials, buckets, or access
policies. After the cluster is healthy, create them through the local Garage CLI
on the coordinator, for example:

```bash
sudo garage -c /etc/garage.toml key create my-app
sudo garage -c /etc/garage.toml bucket create my-bucket
sudo garage -c /etc/garage.toml bucket allow --read --write --owner \
  my-bucket --key my-app
```

Store the returned secret in a separate secret manager or Ansible Vault. Keep
TCP/3901 restricted to the cluster's Tailscale peers and expose TCP/3900 only
to intended S3 clients; this repository does not enable a host firewall.
