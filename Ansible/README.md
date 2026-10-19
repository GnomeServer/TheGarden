# Ansible

This directory contains the Ansible project for TheGarden. It currently manages two different concerns:

- read-only audits of the Proxmox host;
- deployment of the Docker Compose stacks on `infra-lab-services`.

The Docker deployment is intentionally separate from the audit-oriented `site.yml` playbook.

## Project root

Run Ansible from this directory so `ansible.cfg` is loaded automatically:

```bash
cd Ansible
export PATH="$HOME/.local/bin:$PATH"
```

## Inventory

The default inventory is `inventory/hosts.yml`. `ansible.cfg` loads only that file.

```text
lab_local
└── server-debian          local Proxmox audit target

proxmox_hosts
└── lab_local              child group, not a direct host list

proxmox_guests
└── (empty)

docker_hosts
└── infra-lab-services     local connection; docker-services.yml does not SSH to 10.1.10.2

agent_workers
├── inkii                  100.98.125.127  user inkii     worker-02
├── naruto                 100.88.92.78    user naruto    worker-04
├── raphael                100.116.35.103  user raphael   worker-01
└── donatello              100.94.145.104  user bgurrol4  worker-03
```

Every host above pins `ansible_python_interpreter` to `/usr/bin/python3`. Worker hosts are reached by SSH. `infra-lab-services` is `ansible_connection: local`.
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

`playbooks/docker-services.yml` deploys the projects named in `inventory/group_vars/docker_hosts.yml`, in this order:

1. `caddy` at `/home/infra-lab-user/caddy-service` (`build: never`), which creates `caddy_proxy`
2. `forgejo` at `/home/infra-lab-user/forgejo-service`
3. `open-webui` at `/home/infra-lab-user/open-webui-service`
4. `llama` at `/home/infra-lab-user/llama-service`
5. `grafana` at `/home/infra-lab-user/grafana-service`
6. `agent-manager` at `/home/infra-lab-user/agent-manager-service` (`build: always`)

Those are VM checkout paths, not `Docker-Documents/` paths. The playbook does not copy the repository tree, does not deploy `litellm-service`, and does not add `compose.worker-test.yml`. Because `infra-lab-services` is a local connection, the playbook runs on the controller. It does not SSH to `10.1.10.2`.

Repository source directories:

```text
TheGarden/Docker-Documents/caddy_service
TheGarden/Docker-Documents/forgejo
TheGarden/Docker-Documents/open_webui_services
TheGarden/Docker-Documents/llama_services
TheGarden/Docker-Documents/grafana_services
TheGarden/Docker-Documents/litellm-service
TheGarden/Docker-Documents/agent-manager-service
```

Each project keeps its local `.env` file outside Git. Those files contain
secrets and are not managed by the repository. Set the deployment paths in
`inventory/group_vars/docker_hosts.yml` to the checkout location used by the
host running Ansible; do not commit secret values.

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

## Worker laptops

`playbooks/agent-workers.yml` installs the Python worker from `../agent-worker`,
creates a virtualenv, writes `~/agent-worker/worker.env`, and starts
`start-worker.sh` now and on reboot. It does not install Pi. A missing LiteLLM
key is a warning, not a failure. Generated scripts run as the laptop user.

Add a laptop by copying the commented `michelangelo` block in
`inventory/hosts.yml`, filling the Tailscale address, SSH user, and worker id,
then:

```bash
cd Ansible
ansible agent_workers --limit michelangelo -m ansible.builtin.ping
ansible-playbook playbooks/agent-workers.yml --limit michelangelo --ask-become-pass
```

`--ask-become-pass` is only needed for `python3-venv` and `cron`. Place the
virtual key on the controller at the host's `litellm_key_source` before the
run if dynamic Open WebUI prompts should work. Set `worker_model_base_url` in
`inventory/group_vars/agent_workers.yml` when a Tailscale-reachable `/v1` route
exists. Do not commit key files.
