# Infrastructure Docker documents

This directory documents the Docker services running on the `infra-lab-services` VM and the Caddy reverse proxy in front of them.

## Current deployment

```text
Proxmox host:       server-debian / 10.1.10.156
Caddy VM:           infra-lab-services / 10.1.10.2
VM Tailscale:       100.94.49.45
Shared Docker net:  caddy_proxy
Caddy hostname:     infra-lab-services.tail494f6d.ts.net
Grafana URL:        https://infra-lab-services.tail494f6d.ts.net/grafana/
Dashboard URL:      https://infra-lab-services.tail494f6d.ts.net/dashboard/
```

The verified request path is:

```text
client -> Caddy on the VM -> application container
```

A direct request to `https://infra-lab-services.tail494f6d.ts.net/grafana/login` returned `HTTP/2 200`, `Via: 1.1 Caddy`, and Grafana HTML. Grafana, Prometheus, and Node Exporter are deployed by the `grafana_services` Compose project and Caddy is deployed separately. Forgejo and PostgreSQL are deployed by the `forgejo/` Compose project and Forgejo is available under `/forgejo/`; its first administrator and private-service settings must be configured during initial setup.

Open WebUI, Ollama, and llama.cpp are also running on the VM. The Agent
Manager control-plane Compose project is stored in
`TheGarden/Docker-Documents/agent-manager-service` and includes the manager
API, a private PostgreSQL database, and NATS JetStream. Its loopback port is
`8090`; Caddy publishes only the dashboard, authentication, and API routes.
The remote worker pool reaches NATS on the VM's Tailscale address and is
documented in [`../agent-worker/README.md`](../agent-worker/README.md).

The VM now has its own Tailscale identity, so other tailnet devices can use the MagicDNS hostname directly. The older `tail494f6d.ts.net` alias is not the canonical service name.

## Documents

- [`caddy_service/caddy-setup.md`](caddy_service/caddy-setup.md) — VM networking, Caddy routes, TLS/SNI testing, Docker network checks, and Proxmox connectivity.
- [`grafana_services/README.md`](grafana_services/README.md) — Grafana, Prometheus, and Node Exporter deployment and the shared `caddy_proxy` network.
- [`forgejo/README.md`](forgejo/README.md) — Forgejo with PostgreSQL, HTTPS Git access, SSH clone access, and first-run setup.
- [`llama_services/README.md`](llama_services/README.md) — llama.cpp model serving and OpenAI-compatible API access.
- [`open_webui_services/README.md`](open_webui_services/README.md) — Open WebUI and Ollama deployment.
- [`caddy_service/README.md`](caddy_service/README.md) — Caddy Compose deployment, persistent TLS volumes, network membership, and operations.
- [`agent-manager-service/README.md`](agent-manager-service/README.md) — Agent Manager, PostgreSQL, NATS JetStream, API verification, and Ansible deployment.
- [`../agent-worker/README.md`](../agent-worker/README.md) — Remote worker installation, dynamic script prompts, and NATS/LiteLLM configuration.
- [`../docs/dark-factory-dashboard.md`](../docs/dark-factory-dashboard.md) — dashboard deployment, OAuth, webhooks, tasks, workers, backups, and troubleshooting.
- [`../docs/mcp-dashboard.md`](../docs/mcp-dashboard.md) — safe MCP tool and resource contract for dashboard automation.
- [`../docs/github-handoff-dark-factory.md`](../docs/github-handoff-dark-factory.md) — complete GitHub implementation, recovery, security, deployment, and acceptance-test handoff.
- [`caddy_service/Caddyfile`](caddy_service/Caddyfile) — Caddy routes for Grafana, Forgejo, Agent Manager, and the Proxmox fallback.

## Important test command

From any tailnet device, use the hostname and keep this command on one physical line:

```bash
curl -4 -k -i -L --connect-timeout 5 https://infra-lab-services.tail494f6d.ts.net/grafana/login
```

To test the LAN path from `server-debian` while preserving the correct hostname/SNI:

```bash
curl -4 -k -i -L --connect-timeout 5 --resolve infra-lab-services.tail494f6d.ts.net:443:10.1.10.2 https://infra-lab-services.tail494f6d.ts.net/grafana/login
```

Do not test with only `https://10.1.10.2/`. Caddy uses the hostname/SNI `infra-lab-services.tail494f6d.ts.net`; a direct IP request can fail during the TLS handshake even when the service is healthy.

Ansible deployment is documented in [`../Ansible/README.md`](../Ansible/README.md). Run `Ansible/playbooks/docker-services.yml` from the Ansible project to deploy the Compose projects in dependency order. The Compose source directories are under `Docker-Documents/`; their `.env` files remain local deployment files and are not committed.

Do not paste `docker compose config` output into chat or tickets. It expands environment variables and can reveal service passwords or API tokens.
