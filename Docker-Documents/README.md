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
```

The verified request path is:

```text
client -> Caddy on the VM -> application container
```

Open WebUI, Ollama, llama.cpp, and LiteLLM also have Compose projects on the VM. The Agent
Manager control-plane Compose project is stored in
`TheGarden/Docker-Documents/agent-manager-service` and includes the manager
API, a private PostgreSQL database, and NATS JetStream. Its API is
loopback-bound on port `8090` and is available to containers on `caddy_proxy` at
`http://agent-manager:8000`. The base Compose file does not publish NATS.
Remote workers need `compose.worker-test.yml`, which binds port `4222` to the
VM Tailscale address. That pool is documented in [`../agent-worker/README.md`](../agent-worker/README.md).

The VM has its own Tailscale identity, so other tailnet devices can use the MagicDNS hostname directly. The older `tail494f6d.ts.net` alias is not the canonical service name.

## Documents

- [`caddy_service/caddy-setup.md`](caddy_service/caddy-setup.md) — VM networking, Caddy routes, TLS/SNI testing, Docker network checks, and Proxmox connectivity.
- [`caddy_service/README.md`](caddy_service/README.md) — Caddy Compose deployment, persistent TLS volumes, network membership, and operations.
- [`caddy_service/Caddyfile`](caddy_service/Caddyfile) — `/grafana/`, `/forgejo/`, Tailscale-only `/api-guard/` and `/litellm/`, Agent Manager routes, and the Proxmox fallback.
- [`grafana_services/README.md`](grafana_services/README.md) — Grafana, Prometheus, and Node Exporter deployment and the shared `caddy_proxy` network.
- [`forgejo/README.md`](forgejo/README.md) — Forgejo with PostgreSQL, HTTPS Git access, SSH clone access, and first-run setup.
- [`llama_services/README.md`](llama_services/README.md) — llama.cpp model serving. The API is Docker-network only; Caddy does not publish `/llama/`.
- [`litellm-service/README.md`](litellm-service/README.md) — LiteLLM gateway, virtual keys, and the loopback/`caddy_proxy` endpoints that actually exist.
- [`open_webui_services/README.md`](open_webui_services/README.md) — Open WebUI and Ollama. The Compose file does not publish a host port, and the Caddyfile does not proxy Open WebUI.
- [`api-guard-dashboard/README.md`](api-guard-dashboard/README.md) — the separate administrator-only budget, key-rotation, JEV-advisory, and OMP exposure dashboard.
- [`agent-manager-service/README.md`](agent-manager-service/README.md) — Agent Manager, PostgreSQL, NATS JetStream, and API verification. Ansible deployment is in [`../Ansible/README.md`](../Ansible/README.md), not in that service README.
- [`../agent-worker/README.md`](../agent-worker/README.md) — Remote worker installation, dynamic script prompts, and NATS configuration.

`Docker-Documents/ /` and `Docker-Documents/grafana_services/ /` are duplicate LiteLLM trees created under a directory named with a single space. They are not deployment projects. Use `litellm-service/`.

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

Ansible deployment is documented in [`../Ansible/README.md`](../Ansible/README.md). `Ansible/playbooks/docker-services.yml` deploys `caddy`, `forgejo`, `open-webui`, `llama`, `grafana`, `agent-manager`, and the separate `api-guard-dashboard` from paths in `inventory/group_vars/docker_hosts.yml`. Ensure the API Guard project and its private `.env` are installed at the configured path before running Ansible. It does not deploy `litellm-service` and does not apply `compose.worker-test.yml`. Compose `.env` files remain local and are not committed.

Do not paste `docker compose config` output into chat or tickets. It expands environment variables and can reveal service passwords or API tokens.
