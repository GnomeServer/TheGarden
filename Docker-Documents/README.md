# Infrastructure Docker documents

This directory documents the Docker services running on the `infra-lab-services` VM and the Caddy reverse proxy in front of them.

## Current deployment

```text
Proxmox host:       server-debian / 10.1.10.156
Caddy VM:           infra-lab-services / 10.1.10.2
Shared Docker net:  caddy_proxy
Caddy hostname:     tail494f6d.ts.net
Grafana URL:        https://tail494f6d.ts.net/grafana/
```

The verified request path is:

```text
client -> Caddy on the VM -> grafana:3000
```

A request using `--resolve tail494f6d.ts.net:443:10.1.10.2` returned `HTTP/2 200`, `Via: 1.1 Caddy`, and Grafana HTML. Grafana, Prometheus, and Node Exporter are deployed by the `grafana_services` Compose project and Caddy is deployed separately.

The VM does not currently have its own Tailscale identity. The existing Tailscale node is `server-debian` at `100.102.154.23`. Access from other tailnet devices therefore requires a hosts/DNS override, a subnet route, or a future Tailscale identity for the VM. The Tailscale identity work is currently paused.

## Documents

- [`caddy-setup.md`](caddy-setup.md) — VM networking, Caddy routes, TLS/SNI testing, Docker network checks, and Proxmox connectivity.
- [`grafana_services/README.md`](grafana_services/README.md) — Grafana, Prometheus, and Node Exporter deployment and the shared `caddy_proxy` network.
- [`Caddyfile`](Caddyfile) — The Caddy route for `/grafana/` and the Proxmox fallback route.

## Important test command

Use the hostname and keep this command on one physical line:

```bash
curl -4 -k -i -L --connect-timeout 5 --resolve tail494f6d.ts.net:443:10.1.10.2 https://tail494f6d.ts.net/grafana/login
```

Do not test with only `https://10.1.10.2/`. Caddy uses the hostname/SNI `tail494f6d.ts.net`; a direct IP request can fail during the TLS handshake even when the service is healthy.

Do not paste `docker compose config` output into chat or tickets. It expands environment variables and can reveal the Grafana administrator password.
