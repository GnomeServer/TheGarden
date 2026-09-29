# Inventory

The default inventory is `hosts.yml`.

## Active groups

```text
lab_local
└── server-debian

proxmox_hosts
└── server-debian

docker_hosts
└── infra-lab-services
```

`server-debian` is the local Proxmox host used by the audit and Proxmox validation playbooks. `infra-lab-services` is the local Docker VM used by `playbooks/docker-services.yml`.

Both targets currently use a local connection. The Docker VM uses `/usr/bin/python3`.

Inspect the active inventory from the Ansible project directory:

```bash
cd /home/infra-lab-user/TheGarden/Ansible
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

The first remote worker is defined separately in `inventory/worker-1.yml` so it
does not affect the default local audit or Docker deployment inventory:

```text
inkii
├── Tailscale address: 100.98.125.127
├── SSH user: inkii
└── worker_role: coder
```

Inspect or test that inventory explicitly:

```bash
cd /home/infra-lab-user/TheGarden/Ansible
ansible-inventory -i inventory/worker-1.yml --host inkii
ansible -i inventory/worker-1.yml inkii -m ansible.builtin.ping
```

The current worker runtime is a manual smoke-test deployment rather than an
Ansible role. Install the virtual environment, NATS connection settings,
LiteLLM worker key, and `worker.py` according to
[`../../agent-worker/README.md`](../../agent-worker/README.md). Do not add the
worker inventory to `hosts.yml` until SSH authorization and the desired worker
playbooks have been reviewed.

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
