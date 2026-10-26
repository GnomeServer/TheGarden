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
├── inkii                  100.98.125.127  user inkii       worker-02
├── naruto                 100.88.92.78    user naruto      worker-04
├── raphael                100.116.35.103  user raphael     worker-01
└── donatello              100.94.145.104  user bgurrol4    worker-03
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

`playbooks/agent-workers.yml` is the canonical deployment for the laptop queue
consumer and pinned OMP job image. Mission model traffic uses the Tailnet-only
Caddy endpoint at
`https://infra-lab-services.tail494f6d.ts.net/litellm/v1`.

The worker process polls the shared durable `agent-workers-coder` consumer and
sends a heartbeat every 15 seconds. The default `worker_nats_url` is
`nats://100.94.49.45:4222`; that listener must be reachable over Tailscale.
Mission jobs run in the pinned OMP image, which delegates to the planner, coder,
and reviewer definitions. After explicit reviewer approval, the worker packages
the files and uploads them to Agent Manager's PostgreSQL-backed artifact store;
the temporary job workspace is removed after upload. The worker does not clone
or push the configured Forgejo repository.

`playbooks/agent-workers.yml` installs the Python queue consumer, writes
`~/agent-worker/worker.env`, copies `agent-worker/omp-image/`, builds the pinned
OMP job image, and manages `dark-factory-agent-worker` with systemd. The play
deploys one host at a time. Source, environment, certificate, and image changes
restart that host's service. Legacy cron-started workers are stopped during the
one-time cutover. The worker user needs Docker access; membership in the Docker
group is root-equivalent. The playbook does not install Docker or Tailscale.

`allow_generated_code` is false by default. OMP `mission` jobs still run in the
pinned image and perform normal code generation and review. Only the separate
`create-python-script` operation is blocked; it runs generated code unsandboxed
if explicitly enabled.

Only onboard workers after their owners opt in. Do not run the playbook against
`michelangelo` or `dell-drd17c4` until their owners approve and provide SSH login
and a worker-scoped LiteLLM key. For a new worker, prepare an apt-based
Linux/systemd host with SSH, Python 3, Docker access, and Tailscale reachability
to NATS and LiteLLM. Add its Tailscale address, SSH user, unique `worker_id`,
role, and controller-side `litellm_key_source` to `inventory/hosts.yml`. Ensure
the Caddy root CA is available at `caddy_root_ca_source`.

From `Ansible/`, ping and deploy only the approved target:

```bash
WORKER_HOST=inkii # replace with the approved inventory host
ansible agent_workers --limit "$WORKER_HOST" -m ansible.builtin.ping
ansible-playbook playbooks/agent-workers.yml --limit "$WORKER_HOST" --ask-become-pass
ansible "$WORKER_HOST" -m ansible.builtin.command -a "systemctl is-active dark-factory-agent-worker"
```

`--ask-become-pass` is needed for package installation, system CA trust, and
systemd management. Before deployment, export the Caddy root CA with
`../scripts/export_caddy_root_ca.sh` from `Ansible/` and place the worker-scoped
key at the host's `litellm_key_source`. Do not commit key files.
