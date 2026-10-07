# Dark Factory node activity collector

The collector tails selected journald records, normalizes access and service-state events, and sends bounded batches to Agent Manager. Raw logs remain in journald or Loki. The collector never sends passwords, SSH keys, environment variables, or raw sudo commands.

## Canonical node identities

| Host | Worker ID | Role | Tailscale address |
|---|---|---|---|
| `raphael` | `worker-01` | `coder` | `100.116.35.103` |
| `inkii` | `worker-02` | `coder` | `100.98.125.127` |
| `donatello` | `worker-03` | `coder` | `100.94.145.104` |
| `naruto-dell-pro-max-14-mc14250` | `worker-04` | `coder` | `100.88.92.78` |
| `infra-lab-services` | n/a | services | `100.94.49.45` |
| `server-debian` | n/a | bare-metal server | `100.102.154.23` |

Worker role and worker ID are separate fields. All four current workers remain `coder`; the collector uses `worker-01` through `worker-04` as node IDs so access evidence aligns with Agent Manager worker identity.

## Node-scoped keys

Existing LiteLLM, Forgejo, PostgreSQL, and worker keys remain in their current `.env` or controller-side files. Do not reuse them for activity ingestion.

As `infra-lab-user` on the infrastructure VM (the Ansible controller), create
one independent `activity:write` key per node with the Agent Manager
administrator token. These are provisioning instructions, not a claim that
keys have been created or collectors deployed. Keep the token out of shell
history and never reuse LiteLLM keys:

```bash
read -rsp 'Agent Manager administrator token: ' AGENT_MANAGER_API_TOKEN; printf '\n'
export AGENT_MANAGER_API_TOKEN
umask 077
export AGENT_MANAGER_URL='https://infra-lab-services.tail494f6d.ts.net'
mkdir -p /home/infra-lab-user/.config/dark-factory/activity-keys
chmod 700 /home/infra-lab-user/.config/dark-factory/activity-keys

create_key() {
  node_id="$1"
  curl -fsS \
    -H "Authorization: Bearer ${AGENT_MANAGER_API_TOKEN}" \
    -H 'Content-Type: application/json' \
    -d "{\"node_id\":\"${node_id}\",\"label\":\"journald collector\"}" \
    "${AGENT_MANAGER_URL}/v1/node-credentials" \
    | jq -er .token \
    > "/home/infra-lab-user/.config/dark-factory/activity-keys/${node_id}"
  chmod 600 "/home/infra-lab-user/.config/dark-factory/activity-keys/${node_id}"
}

create_key worker-01
create_key worker-02
create_key worker-03
create_key worker-04
create_key infra-lab-services
```

The plaintext token is returned once. PostgreSQL stores only a keyed hash. Revoking a credential does not affect any other node.

Create a separate `server-debian` key when the collector is approved for the bare-metal host. Its inventory group is intentionally separate from the worker deployment.

## Identity correlation

Access events initially retain their observed identity, such as Linux username `bgurrol4`. An administrator maps it to a Forgejo dashboard user:

```bash
curl -fsS \
  -H "Authorization: Bearer ${AGENT_MANAGER_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  "${AGENT_MANAGER_URL}/v1/identity-mappings" \
  -d '{
    "user_id": "<dashboard-user-uuid>",
    "source": "linux",
    "external_identity": "bgurrol4"
  }'
```

Use immutable external IDs where available. A shared Linux account cannot prove which human used it; individual Unix accounts are preferred.

## Deploy with Ansible

Run Ansible as `infra-lab-user` on `infra-lab-services` (LAN `10.1.10.2`,
Tailscale `100.94.49.45`), from the repository's `Ansible` directory. The VM
is the sole local inventory target. Workers use their existing SSH identities;
bare-metal `server-debian` is reached explicitly over SSH as
`df-server@100.102.154.23`, never as a local target. Do not run this inventory
on bare metal or Donatello. Node keys are read from the private controller
directory `/home/infra-lab-user/.config/dark-factory/activity-keys/<node>`.

The playbook includes all three groups. Limit the initial rollout to workers
and the VM; provision `server-debian` separately only after approval:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/node-activity-collectors.yml --limit 'agent_workers:docker_hosts' --check --ask-become-pass
ansible-playbook -i inventory/hosts.yml playbooks/node-activity-collectors.yml --limit 'agent_workers:docker_hosts' --ask-become-pass
```

For an approved bare-metal rollout, run `create_key server-debian` separately,
then use the same playbook with `--limit server-debian --ask-become-pass`.

The playbook installs:

```text
/opt/dark-factory/activity-collector/collector.py
/etc/dark-factory/activity-key
/etc/dark-factory/activity-collector.env
/etc/systemd/system/dark-factory-activity-collector.service
/var/lib/dark-factory-activity/journal.cursor
```

The service runs as root only because system journal access differs across distributions. Its systemd sandbox blocks home-directory access, filesystem writes outside the cursor directory, privilege escalation, kernel-tunable changes, and control-group changes.

## Events

The collector recognizes:

- Successful and failed SSH authentication.
- SSH session open and close messages.
- `sudo` use without forwarding the command.
- Selected Tailscale identity events.
- Agent worker systemd state changes.

Every event ID is derived from the journald cursor and node ID. Agent Manager deduplicates `(node_id, event_id)`. The cursor advances only after Agent Manager accounts for the complete batch, so network failures cause safe replay rather than loss.

## Operations

```bash
sudo systemctl status dark-factory-activity-collector
sudo journalctl -u dark-factory-activity-collector -n 100 --no-pager
```

A collector error should identify connectivity or validation failure without
printing the node key. Confirm current VM connectivity before deployment;
these repository changes do not establish remote availability or deployment.
