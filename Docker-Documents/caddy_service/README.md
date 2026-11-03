# Caddy service

This Compose project runs Caddy on the `infra-lab-services` VM.

The expected VM layout is:

```text
~/caddy-service/
├── Caddyfile
├── compose.yml
└── site/
```

The repository Caddyfile is `Caddyfile` in this directory, mounted by `compose.yml` at `/etc/caddy/Caddyfile`. Copy this directory to `~/caddy-service` on the VM; do not look for a Caddyfile in the parent `Docker-Documents/` directory.

## Current configuration

- Compose project name: `caddy-service`, fixed so deployment from a worktree reuses the existing Caddy volumes
- Image: `caddy:2.11.4-alpine`
- HTTP: VM port `80`
- HTTPS: VM port `443` and UDP `443` for HTTP/3
- Open WebUI HTTPS: VM port `8443`
- Network mode: host, so Caddy sees the original client IP for its Tailnet ACLs
- Persistent volumes: `caddy-service_caddy_data` and `caddy-service_caddy_config`
- TLS: Caddy's internal CA
- Grafana: `127.0.0.1:18000` under `/grafana/`
- Forgejo: `127.0.0.1:18001` under `/forgejo/`, with the `/forgejo` prefix stripped
- Keybroker: `/keybroker/`, Tailnet-only; Caddy injects the proxy token and original peer IP before proxying to `127.0.0.1:18002`
- API Guard: `/api-guard/` at `127.0.0.1:18003`, restricted to Tailnet source addresses and authenticated separately through Forgejo OAuth
- Agent Manager dashboard and APIs: `127.0.0.1:8090` for `/dashboard/`, `/v1/*`, `/auth/*`, `/healthz`, and `/readyz`
- LiteLLM: `127.0.0.1:4000` for `/litellm/v1`, restricted to Tailnet source addresses
- Requests outside Tailscale to `/keybroker/*`, `/api-guard/*`, and `/litellm/*` receive HTTP 403
- Proxmox fallback: `https://10.1.10.156:8006` for other paths on port 443

Application containers share the `caddy_proxy` bridge network for private
container-to-container traffic. Caddy runs in host network mode and reaches
upstreams only through host-loopback ports. The deployment playbook creates
`caddy_proxy` before deploying the stacks; manual deployments must create it
before starting application containers.

## Deploy

The Compose file fixes the project name so deploying from a worktree does not
create new Caddy data volumes. The Caddyfile and site directory are bind-mounted,
so deploy from a persistent checkout (not `/tmp`) to keep the configuration
available after reboot. From the checked-out repository:

Set a high-entropy `KEYBROKER_PROXY_TOKEN` in the Caddy deployment `.env` and
the Keybroker `.env`; the values must match. The checked-in `.env.example`
contains a placeholder, not a usable token. For a new deployment, copy it to
`.env`, replace the placeholder, and set mode `0600`. For an existing Caddy
deployment, add the variable to its existing `.env` without replacing other
live settings. Caddy uses this token only on the internal reverse-proxy
request; remote agents do not receive it. Compose refuses to start if the
variable is missing.

```bash
cd Docker-Documents/caddy_service
sudo docker network inspect caddy_proxy >/dev/null 2>&1 || sudo docker network create caddy_proxy
sudo docker compose config --quiet
sudo docker compose up -d --force-recreate caddy
sudo docker compose ps
```

Validate the active Caddyfile:

```bash
sudo docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
```

After changing the Caddyfile:

```bash
sudo docker compose up -d --force-recreate caddy
sudo docker compose logs --tail=100 caddy
```

Do not delete the `caddy_data` volume during normal updates. It contains Caddy's certificate authority and TLS state.

## Test

From a Tailscale device:

```bash
curl -4 -k -i -L --connect-timeout 5 https://infra-lab-services.tail494f6d.ts.net/grafana/login
curl -4 -k -i -L --connect-timeout 5 https://infra-lab-services.tail494f6d.ts.net/forgejo/
```

A direct request to `https://10.1.10.2/` is not a valid test because it does not provide the configured hostname/SNI.

## Troubleshooting

Inspect the shared network:

```bash
sudo docker network inspect caddy_proxy \
  --format '{{range .Containers}}{{.Name}}{{"\\n"}}{{end}}'
```

The output should include application containers such as Grafana and Forgejo;
Caddy runs in host network mode and is not attached to `caddy_proxy`. If Caddy
returns `502`, check the corresponding loopback port mapping and service health.

For the Proxmox fallback, test the upstream from the VM:

```bash
curl -kIv https://10.1.10.156:8006/
sudo docker compose exec caddy \
  wget -S -O - --no-check-certificate https://10.1.10.156:8006/
```
