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
client -> Caddy on the VM -> grafana:3000
```

A direct request to `https://infra-lab-services.tail494f6d.ts.net/grafana/login` returned `HTTP/2 200`, `Via: 1.1 Caddy`, and Grafana HTML. Grafana, Prometheus, and Node Exporter are deployed by the `grafana_services` Compose project and Caddy is deployed separately. A Forgejo/PostgreSQL Compose project is prepared under `forgejo/`; it still needs to be deployed and its first administrator configured.

The VM now has its own Tailscale identity, so other tailnet devices can use the MagicDNS hostname directly. The older `tail494f6d.ts.net` alias is not the canonical service name.

## Documents

- [`caddy_service/caddy-setup.md`](caddy-setup.md) — VM networking, Caddy routes, TLS/SNI testing, Docker network checks, and Proxmox connectivity.
- [`grafana_services/README.md`](grafana_services/README.md) — Grafana, Prometheus, and Node Exporter deployment and the shared `caddy_proxy` network.
- [`forgejo/README.md`](forgejo/README.md) — Forgejo with PostgreSQL, HTTPS Git access, SSH clone access, and first-run setup.
- [`caddy_service/README.md`](caddy_service/README.md) — Caddy Compose deployment, persistent TLS volumes, network membership, and operations.
- [`caddy_service/Caddyfile`](Caddyfile) — The Caddy routes for `/grafana/`, `/forgejo/`, and the Proxmox fallback route.

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

Do not paste `docker compose config` output into chat or tickets. It expands environment variables and can reveal the Grafana administrator password.
