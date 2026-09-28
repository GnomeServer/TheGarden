# Ansible

This directory contains the Ansible project for TheGarden. It currently manages two different concerns:

- read-only audits of the Proxmox host;
- deployment of the Docker Compose stacks on `infra-lab-services`.

The Docker deployment is intentionally separate from the audit-oriented `site.yml` playbook.

## Project root

Run Ansible from this directory so `ansible.cfg` is loaded automatically:

```bash
cd /home/infra-lab-user/TheGarden/Ansible
export PATH="$HOME/.local/bin:$PATH"
```

## Inventory

The default inventory is `inventory/hosts.yml`:

```text
server-debian       Proxmox host used by the audit playbooks
infra-lab-services  Docker VM used by docker-services.yml
```

`infra-lab-services` is currently a local Ansible target. It is in the `docker_hosts` group and uses `/usr/bin/python3`.

Inspect the inventory with:

```bash
ansible-inventory --graph
ansible-inventory --host infra-lab-services
```

## Collections

The Docker deployment uses `community.docker.docker_compose_v2`. Install the collection from the project requirements file:

```bash
ansible-galaxy collection install -r requirements.yml
```

## Docker Compose deployment

`playbooks/docker-services.yml` deploys the existing Compose projects in dependency order:

1. `caddy-service`, which creates the external `caddy_proxy` network;
2. `forgejo-service`, `open-webui-service`, `llama-service`, and `grafana-service`;
3. `agent-manager-service`, which needs both `caddy_proxy` and `open-webui_internal`.

The project paths are defined in:

```text
inventory/group_vars/docker_hosts.yml
```

The current live Compose checkouts are outside TheGarden:

```text
/home/infra-lab-user/caddy-service
/home/infra-lab-user/forgejo-service
/home/infra-lab-user/open-webui-service
/home/infra-lab-user/llama-service
/home/infra-lab-user/grafana-service
/home/infra-lab-user/agent-manager-service
```

Each project keeps its own local `.env` file. Those files contain secrets and are not managed by Git. The Ansible playbook currently uses them as-is.

Validate the playbook:

```bash
ansible-playbook playbooks/docker-services.yml --syntax-check
```

Deploy or update the stacks:

```bash
ansible-playbook playbooks/docker-services.yml --ask-become-pass
```

The playbook uses `become: true` because the current user does not have direct permission to access `/var/run/docker.sock`.

The agent manager has `build: always`; the other Compose projects use `build: never`. Images are pulled only when missing.

## Audits

Capture a read-only host snapshot:

```bash
ansible-playbook playbooks/audit.yml
```

Run the existing audit-oriented site playbook:

```bash
ansible-playbook playbooks/site.yml --syntax-check
ansible-playbook playbooks/site.yml --check --diff
```

Generated state is written under `state/` and is ignored by Git because it contains host-specific information.

## Secrets and future layout

Do not add Compose `.env` files, API tokens, passwords, private keys, or vault passwords to TheGarden. The next step for a reproducible rebuild is to store secret values in Ansible Vault and template each Compose `.env` file with mode `0600`.

The current Compose source directories should eventually be moved into TheGarden under `Docker-Documents/`, including the agent manager. Until then, `docker_hosts.yml` deliberately points at the working directories under `/home/infra-lab-user/`.
