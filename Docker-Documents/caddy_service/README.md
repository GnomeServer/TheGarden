# Caddy service

This Compose project runs Caddy on the `infra-lab-services` VM.

The expected VM layout is:

```text
~/caddy-service/
├── Caddyfile
├── compose.yml
└── site/
```

The Caddyfile for this Compose project is tracked beside `compose.yml`. Keep those files together when copying the project into the VM's separate `~/caddy-service` folder.

## Current configuration

- Image: `caddy:2.11.4-alpine`
- HTTP: VM port `80`
- HTTPS: VM port `443` and UDP `443` for HTTP/3
- Open WebUI HTTPS: VM port `8443`
- Docker network: `caddy_proxy`
- Persistent volumes: `caddy_data` and `caddy_config`
- TLS: Caddy's internal CA
- Grafana: `grafana:3000` under `/grafana/`
- Forgejo: `forgejo:3000` under `/forgejo/`
- llama.cpp: `llama:8080` under `/llama/`, stripping the route prefix
- LiteLLM: `litellm:4000` under `/litellm/`, stripping the route prefix
- Open WebUI: `open-webui:8080` at `https://infra-lab-services.tail494f6d.ts.net:8443`
- Agent Manager: `agent-manager:8000` under `/dashboard/`, `/auth/*`, `/v1/*`, `/docs`, `/docs/*`, and `/openapi.json`
- Proxmox fallback: `https://10.1.10.156:8006`

The `caddy_proxy` network is intentionally named so Grafana, Forgejo, Agent Manager, llama.cpp, LiteLLM, and Open WebUI Compose projects can join it as an external network. Caddy creates the network when this project starts.

The captured live model routes and dedicated Open WebUI listener coexist with
the dashboard and authentication routes; do not replace the Caddyfile with a
model-only or dashboard-only version. Both HTTPS listeners use Caddy's internal
CA. Port `8443` is published by this Compose file, not directly by Open WebUI.
The manager's NATS exporter shares `caddy_proxy` for Prometheus access only;
there is no public Caddy metrics route or host port for that exporter.

## Deploy

Copy `Caddyfile`, `compose.yml`, and the empty `site/` directory to `~/caddy-service` on the VM. Then run:

```bash
cd ~/caddy-service
sudo docker compose config
sudo docker compose up -d
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
curl -4 -k -i -L --connect-timeout 5 https://infra-lab-services.tail494f6d.ts.net/dashboard/
curl -4 -k -i -L --connect-timeout 5 https://infra-lab-services.tail494f6d.ts.net/llama/
curl -4 -k -i -L --connect-timeout 5 https://infra-lab-services.tail494f6d.ts.net/litellm/
curl -4 -k -i -L --connect-timeout 5 https://infra-lab-services.tail494f6d.ts.net:8443/
```

A direct request to `https://10.1.10.2/` is not a valid test because it does not provide the configured hostname/SNI.

These commands are manual diagnostics, not evidence of a deployment. Model
services may require authentication; an unauthenticated response is not a
successful authenticated API test. Use the trusted Caddy CA instead of `-k`
for normal clients.

## Troubleshooting

Inspect the shared network:

```bash
sudo docker network inspect caddy_proxy \
  --format '{{range .Containers}}{{.Name}}{{"\\n"}}{{end}}'
```

The output should include `caddy` and each deployed upstream (`grafana`, `forgejo`, `agent-manager`, `llama`, `litellm`, and `open-webui`). If Caddy returns `502`, confirm that the target service is running and attached to `caddy_proxy`.

For the Proxmox fallback, test the upstream from the VM:

```bash
curl -kIv https://10.1.10.156:8006/
sudo docker compose exec caddy \
  wget -S -O - --no-check-certificate https://10.1.10.156:8006/
```
