# Agent Manager

The Agent Manager is the first control-plane service for agentic coding runs on `infra-lab-services`.

The current live Compose project is:

```text
/home/infra-lab-user/agent-manager-service
```

The source directory is currently outside TheGarden. This document keeps the deployment topology and verification steps with the rest of the Docker documentation until the service is moved into `Docker-Documents/`.

## Services

The Compose project contains:

```text
agent-manager  FastAPI API and run-state publisher
postgres       private PostgreSQL database for run state
nats           NATS with JetStream and persistent event storage
```

Agent Manager joins these networks:

```text
caddy_proxy          Forgejo API and optional Caddy routing
open-webui_internal  Ollama access
agent_manager_internal  PostgreSQL and NATS
```

It does not join `forgejo_internal`.

## API

The API is bound to loopback on the VM by default:

```text
http://127.0.0.1:8090
```

Containers on `caddy_proxy` can reach it at:

```text
http://agent-manager:8000
```

The current endpoints are:

```text
GET  /healthz
GET  /readyz
POST /v1/runs
GET  /v1/runs
GET  /v1/runs/{run_id}
POST /v1/runs/{run_id}/cancel
```

All `/v1` endpoints require the `AGENT_MANAGER_API_TOKEN` bearer token.

## NATS

JetStream is enabled with the stream:

```text
AGENT_RUNS
```

Run creation events use:

```text
agent.runs.created
```

Cancellation events use:

```text
agent.runs.cancelled
```

There is not yet a planner or coding worker consuming these events. A newly submitted run is therefore expected to remain in the `queued` state.

## Deployment through Ansible

The Agent Manager is deployed last by:

```text
Ansible/playbooks/docker-services.yml
```

The inventory entry is in:

```text
Ansible/inventory/group_vars/docker_hosts.yml
```

Deploy from the repository:

```bash
cd /home/infra-lab-user/TheGarden/Ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook playbooks/docker-services.yml --syntax-check
ansible-playbook playbooks/docker-services.yml --ask-become-pass
```

The Compose project uses its local environment file:

```text
/home/infra-lab-user/agent-manager-service/.env
```

This file contains the database password and API token and must not be committed.

## Verification

The live Compose project contains runnable verification snippets:

```bash
cd /home/infra-lab-user/agent-manager-service

bash verify-health.txt
bash verify-auth.txt
bash submit-test-run.txt
bash get-test-run.txt
bash verify-postgres.txt
bash verify-nats.txt
bash consume-test-event.txt
```

A successful smoke test confirms:

```text
HTTP request -> manager authentication -> PostgreSQL persistence -> NATS JetStream event
```

## Future work

- Move this Compose project into `TheGarden/Docker-Documents/agent_manager/`.
- Add a planner and worker consumer for `agent.runs.created`.
- Add Ansible Vault templates for the manager `.env` file.
- Add Forgejo API integration and model endpoint health checks.
