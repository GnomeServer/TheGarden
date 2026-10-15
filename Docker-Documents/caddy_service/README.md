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

- Image: `caddy:2.11.4-alpine`
- HTTP: VM port `80`
- HTTPS: VM port `443` and UDP `443` for HTTP/3
- Docker network: `caddy_proxy`
- Persistent volumes: `caddy_data` and `caddy_config`
- TLS: Caddy's internal CA
- Grafana: `grafana:3000` under `/grafana/`
- Forgejo: `forgejo:3000` under `/forgejo/`, with the `/forgejo` prefix stripped
- Proxmox fallback: `https://10.1.10.156:8006` for every other path on this site
- Not routed: `/litellm/`, `/llama/`, Open WebUI, Agent Manager, NATS, and PostgreSQL
- Not published: port `8443` or any listener other than `80` and `443`

This project creates the `caddy_proxy` network. Grafana, Forgejo, and the other Compose projects join it as an external network. Do not mark this project's network `external: true`.

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
```

A direct request to `https://10.1.10.2/` is not a valid test because it does not provide the configured hostname/SNI.

## Troubleshooting

Inspect the shared network:

```bash
sudo docker network inspect caddy_proxy \
  --format '{{range .Containers}}{{.Name}}{{"\\n"}}{{end}}'
```

The output should include `caddy` and any deployed `grafana` and `forgejo` containers. If Caddy returns `502`, confirm that the target service is running and attached to `caddy_proxy`.

For the Proxmox fallback, test the upstream from the VM:

```bash
curl -kIv https://10.1.10.156:8006/
sudo docker compose exec caddy \
  wget -S -O - --no-check-certificate https://10.1.10.156:8006/
```
