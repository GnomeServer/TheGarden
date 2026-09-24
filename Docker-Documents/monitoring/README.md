# Monitoring stack

This Compose project deploys Prometheus, Node Exporter, and Grafana inside `infra-lab-services`.

The project joins the existing Caddy network instead of creating a new network:

```text
caddy-service_default: 172.18.0.0/16
```

No monitoring ports are published directly to the VM LAN. Caddy is the intended HTTPS entry point.

## Deploy on `infra-lab-services`

Copy this directory to the VM, then run:

```bash
cd ~/monitoring
cp .env.example .env
chmod 600 .env
nano .env
```

Set a strong `GRAFANA_ADMIN_PASSWORD`. Do not commit `.env`.

Validate and start the stack:

```bash
sudo docker compose config
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

View logs:

```bash
sudo docker compose logs --tail=100 prometheus
sudo docker compose logs --tail=100 node-exporter
sudo docker compose logs --tail=100 grafana
```

The services use these internal Docker names:

```text
prometheus:9090
node-exporter:9100
grafana:3000
```

Grafana is provisioned automatically with Prometheus as its default data source.

## Caddy route

After choosing a hostname that resolves to the Caddy VM, add a route to the Caddyfile:

```caddyfile
grafana.example.internal {
    reverse_proxy grafana:3000
}
```

The Caddy container and the monitoring containers are on `caddy-service_default`, so Caddy can resolve `grafana` through Docker DNS.

Validate and reload Caddy:

```bash
cd ~/caddy-service
sudo docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose restart caddy
sudo docker compose logs --tail=100 caddy
```

Do not publish Grafana's port `3000` unless there is a specific temporary testing requirement.

## Upgrade notes

Image tags are pinned in `.env.example`. Review release notes, back up the Grafana and Prometheus volumes, then change tags deliberately. The `.env` file contains the Grafana administrator password and must remain untracked.
