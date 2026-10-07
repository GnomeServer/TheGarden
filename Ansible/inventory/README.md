# Inventory

The default inventory is `hosts.yml`.

## Active groups

```text
proxmox_hosts
└── server-debian

docker_hosts
└── infra-lab-services

agent_workers
├── inkii
├── naruto
├── raphael
└── donatello
```

Run the active inventory only as `infra-lab-user` on `infra-lab-services`, the
infrastructure VM (LAN `10.1.10.2`, Tailscale `100.94.49.45`). It is the sole
local target. `server-debian` is the separate bare-metal Proxmox host reached
over SSH as `df-server@100.102.154.23`; audit and site playbooks target
`proxmox_hosts`. Running this inventory on another machine would misdirect
the VM's local tasks. Historical `hosts.yml.backup.*` files are not active
inventories and must not be loaded.

Inspect the active inventory from the Ansible project directory:

```bash
cd TheGarden/Ansible
ansible-inventory --graph
ansible-inventory --host infra-lab-services
```

## Docker target

The `docker_hosts` variables are defined in:

```text
inventory/group_vars/docker_hosts.yml
```

They list the live Compose projects in their required startup order:

```text
/home/infra-lab-user/caddy-service
/home/infra-lab-user/forgejo-service
/home/infra-lab-user/open-webui-service
/home/infra-lab-user/llama-service
/home/infra-lab-user/grafana-service
/home/infra-lab-user/agent-manager-service
```

The Docker deployment uses the `community.docker` collection:

```bash
ansible-galaxy collection install -r requirements.yml
ansible-playbook playbooks/docker-services.yml --syntax-check
ansible-playbook playbooks/docker-services.yml --ask-become-pass
```

Caddy starts first because it creates the external `caddy_proxy` network. Agent Manager starts last because it joins both `caddy_proxy` and `open-webui_internal`.

The Compose project `.env` files remain on the Docker VM and are intentionally not stored in the inventory or committed to Git.

## Agent worker inventory

The approved worker nodes are now in the active `inventory/hosts.yml` under the
`agent_workers` group. This is the correct location when worker playbooks should
be runnable through the default Ansible configuration:

```bash
cd TheGarden/Ansible
ansible-inventory --graph
ansible-inventory --host inkii
ansible agent_workers -m ansible.builtin.ping
```

The current workers are:

| Host | Tailscale address | SSH user | Role |
| --- | --- | --- | --- |
| `inkii` | `100.98.125.127` | `inkii` | `coder` |
| `naruto` | `100.88.92.78` | `naruto` | `coder` |
| `raphael` | `100.116.35.103` | `raphael` | `coder` |
| `donatello` | `100.94.145.104` | `bgurrol4` | `coder` |

The existing `inventory/worker-1.yml` is an optional single-worker inventory
for isolated testing. Do not load it together with `hosts.yml` unless you
intentionally want to maintain duplicate definitions for `inkii`.

The worker runtime is documented in
[`../../agent-worker/README.md`](../../agent-worker/README.md).
`agent-workers.yml` is the one canonical playbook; the captured
`configure-agent-workers.yml` duplicate is intentionally omitted. The role
preserves Pi/LiteLLM provisioning until the planned OMP cutover (not implemented
here). Each worker retains its own controller key at
`/home/infra-lab-user/litellm-service/.worker-XX-api-key`. Activity collector
keys are provisioned separately under
`/home/infra-lab-user/.config/dark-factory/activity-keys/<node>` and are never
reused LiteLLM keys. Use `--limit` during initial rollout:

```bash
ansible-playbook playbooks/agent-workers.yml --limit inkii
```

## Proxmox guest template

`proxmox-guests.example.yml` is not loaded by default. It is an optional SSH-based guest inventory example for VMID `100`, not a second active definition of the VM controller.

Before activating a remote guest entry, verify:

1. its current IP address or MagicDNS name;
2. its operating-system username;
3. SSH key and/or Tailscale SSH authorization;
4. its Python interpreter path;
5. which playbooks are safe to run against it.

From a Debian guest console, install SSH and Python if needed:

```bash
sudo apt update
sudo apt install -y openssh-server python3
sudo systemctl enable --now ssh
```

Use host-specific variables under `host_vars/` for values that differ between machines. Do not put passwords, private keys, API tokens, or vault passwords in inventory files.
