# Inventory

The default inventory is `hosts.yml`.

## Active groups

```text
lab_local
└── server-debian

proxmox_hosts
└── lab_local

proxmox_guests
└── (empty)

docker_hosts
└── infra-lab-services

agent_workers
├── inkii
├── naruto
├── raphael
└── donatello
```

`server-debian` is the local Proxmox host used by the audit and Proxmox validation playbooks. `infra-lab-services` is the local Docker VM used by `playbooks/docker-services.yml`.

Both targets currently use a local connection. The Docker VM uses `/usr/bin/python3`.

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

They list the live Compose project paths used by the playbook. These are not the repository source directories:

```text
caddy          /home/infra-lab-user/caddy-service
forgejo        /home/infra-lab-user/forgejo-service
open-webui     /home/infra-lab-user/open-webui-service
llama          /home/infra-lab-user/llama-service
grafana        /home/infra-lab-user/grafana-service
agent-manager  /home/infra-lab-user/agent-manager-service          build: always
```

`litellm-service` is not in this list. The playbook does not SSH to the VM; `infra-lab-services` is a local connection.

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

Configured Ansible worker targets (inventory entries, not proof of deployment):

| Host | Tailscale address | SSH user | Role |
| --- | --- | --- | --- |
| `inkii` | `100.98.125.127` | `inkii` | `coder` |
| `naruto` | `100.88.92.78` | `naruto` | `coder` |
| `raphael` | `100.116.35.103` | `raphael` | `coder` |
| `donatello` | `100.94.145.104` | `bgurrol4` | `coder` |
| `michelangelo` | `100.70.246.104` | `dow_admin` | `coder` |
| `sammy` (`dell-drd17c4`) | `100.79.110.117` | `dow_admin` | `coder` |

All six inventory entries are now configured, but the worker runtime and
systemd egress policy are not active on a host until its playbook run succeeds.

The existing `inventory/worker-1.yml` is an optional single-worker inventory
for isolated testing. Do not load it together with `hosts.yml` unless you
intentionally want to maintain duplicate definitions for `inkii`.

`playbooks/agent-workers.yml` provisions the worker systemd service one host at
a time. Only onboard a host after its owner opts in. Prepare an apt-based
Linux/systemd laptop with SSH, Python 3, Docker access, and Tailscale reachability
to NATS and the Keybroker/LiteLLM endpoints. Add its Tailscale address, SSH user,
unique worker ID, and role in `hosts.yml`; ensure the Caddy root CA is available
to the controller. From `Ansible/`, target only that approved host:

```bash
WORKER_HOST=inkii # replace with the approved inventory host
ansible agent_workers --limit "$WORKER_HOST" -m ansible.builtin.ping
ansible-playbook playbooks/agent-workers.yml --limit "$WORKER_HOST" --ask-become-pass
```

`allow_generated_code` is false by default. That disables only legacy,
unsandboxed `create-python-script` execution; OMP mission coding is unaffected.
An allowlist entry alone does not install or secure a worker; verify the worker
service and systemd egress policy after each successful playbook run. See
`../README.md` for the complete procedure. Do not load
`proxmox-guests.example.yml` beside `hosts.yml`.

## Proxmox guest template

`proxmox-guests.example.yml` is not loaded by default. It documents VMID `100`, the Debian guest named `infra-lab-services`, and the connection values that must be confirmed before using SSH-based management.

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
