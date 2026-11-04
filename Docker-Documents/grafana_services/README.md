# Grafana services

This Compose project deploys Grafana, Prometheus, and Node Exporter inside `infra-lab-services`. The Compose project name is `grafana_services`.

The containers join the shared application Docker network `caddy_proxy`.
Host-networked Caddy is not attached to that bridge; it proxies to Grafana
through the loopback-only mapping `127.0.0.1:18000:3000`.

No application ports are published to the VM's LAN interfaces. Caddy remains
the HTTPS entry point.

## Current status

The stack joins `caddy_proxy`; Caddy reaches Grafana via its loopback-only port. The verified URL is:

```text
https://infra-lab-services.tail494f6d.ts.net/grafana/
```

The VM has the Tailscale identity `infra-lab-services.tail494f6d.ts.net` (`100.94.49.45`), which is also the application hostname used by Caddy and Grafana. The live VM checkout is currently `~/grafana-service`; the repository directory is `grafana_services`. The current VM Compose output has used both `grafana-services` and the older `grafana-service` project names during migration. The project name affects container and volume names, not Docker DNS. Use one Compose project at a time and remove old `monitoring-*`/`grafana-service-*` containers from `caddy_proxy`.

## Deploy on `infra-lab-services`

Copy this directory to the VM, then run:

```bash
cd ~/grafana_services  # use the directory where this checkout was copied
cp .env.example .env
chmod 600 .env
nano .env
```

Set a strong `GRAFANA_ADMIN_PASSWORD` and keep `GRAFANA_DOMAIN=infra-lab-services.tail494f6d.ts.net` unless the application hostname is deliberately changed. Do not commit `.env`. Do not paste the output of `docker compose config` into tickets or chat because Compose expands and prints the password. If a password has already been exposed, rotate it.

Validate and start the stack:

```bash
sudo docker compose config
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

Validate the Prometheus configuration from the Compose project directory:

```bash
cd ~/grafana-service
sudo docker compose exec prometheus /bin/promtool check config /etc/prometheus/prometheus.yml
```

The command is intentionally shown on one line. If using a multiline shell command, the backslash must be the final character on the line with no following spaces. A command such as `\\ promtool` passes a leading-space command name to Docker.

If the VM checkout is still named `~/grafana-service`, that path is fine; the directory name does not control Docker DNS. Confirm that the deployed file is the updated one:

```bash
grep -nE '^(name:|    name:)' compose.yml
```

It should show the intended `grafana_services` project name (the current VM may still show `grafana-services`) and the shared Caddy network. The service itself is named `grafana`, which provides the Docker DNS name. Start the complete stack after changing the file:

```bash
sudo docker compose up -d --force-recreate
```

View logs:

```bash
sudo docker compose logs --tail=100 prometheus
sudo docker compose logs --tail=100 node-exporter
sudo docker compose logs --tail=100 grafana
```

Docker DNS names on the shared Caddy network are `prometheus:9090`, `node-exporter:9100`, and `grafana:3000`. Compose does not publish those ports. Node Exporter uses `pid: host` and `--path.rootfs=/host`. It does not set `network_mode: host` or `extra_hosts`.

`prometheus/prometheus.yml` scrapes `host.docker.internal:9100` for
`infra-lab-services`, `agent-manager:8000/metrics`, `nats-exporter:7777`, and
the worker Tailscale addresses on port `9100`. Their `host` labels are
`inkii`, `naruto`, `raphael`, `donatello`, `michelangelo`, and `sammy`, so
node-health cards use hostnames rather than worker IDs. The NATS JetStream
scrape is preserved in the same configuration.

`node-exporter:9100` is not used for the VM host target. Run
`Ansible/playbooks/node-exporters.yml` to install Node Exporter on the
approved workers and copy, validate, and apply this Prometheus configuration.
The Ansible role binds each worker exporter to its Tailscale address only.
Node Exporter has no authentication: Tailscale policy or host firewall rules
must limit access to the monitoring VM. These OS metrics do not replace the
Agent Manager worker heartbeat.

Grafana is provisioned automatically with Prometheus as its default data source.

## Caddy route

Grafana is served under the existing Caddy hostname at:

```text
https://infra-lab-services.tail494f6d.ts.net/grafana/
```

The Caddyfile route is:

```caddyfile
@grafana {
    path /grafana /grafana/*
}

handle @grafana {
    reverse_proxy 127.0.0.1:18000
}
```

Grafana is configured for the `/grafana/` subpath through `GF_SERVER_ROOT_URL` and `GF_SERVER_SERVE_FROM_SUB_PATH`. Its host port is bound to loopback only; Caddy uses host networking and does not join `caddy_proxy`.

If the route returns 502, check Grafana's loopback mapping and health, then
inspect the application network to confirm Grafana is connected:

```bash
sudo docker network inspect caddy_proxy \
  --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}'
```

The output should include the Grafana and other application containers; Caddy
runs in host network mode and is not listed on `caddy_proxy`.

To remove stale containers without removing volumes:

```bash
sudo docker ps -a --format '{{.Names}}' | grep -E '^(monitoring-|grafana-service-)' | xargs -r sudo docker rm -f
```

Do not combine `docker ps -q` with `docker ps --format`; Docker ignores the custom format when `-q` is present.

Caddy no longer creates or joins `caddy_proxy`. The deployment playbook creates
the shared application network before deploying the Compose projects. For a
manual deployment, create it once before starting any dependent stack:

```bash
sudo docker network inspect caddy_proxy >/dev/null 2>&1 || sudo docker network create caddy_proxy
sudo docker compose up -d --force-recreate
```

Then validate and reload Caddy. Run the following from the Caddy Compose project directory:

```bash
sudo docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose restart caddy
sudo docker compose logs --tail=100 caddy
```

Do not use container IPs or Docker service names as Caddy upstreams. Host-networked
Caddy uses `127.0.0.1:18000`; containers on `caddy_proxy` continue to use Docker
service DNS for container-to-container requests.

Grafana's container port `3000` is not exposed to the LAN. Compose maps it only
to `127.0.0.1:18000` for Caddy's upstream; Caddy's host-network listener
preserves the original client IP.

## Test the Grafana URL

When testing, use the complete command on one physical line. A newline without a final backslash runs `curl` without a URL and then tries to execute the URL as a shell command:

```bash
curl -4 -k -i -L --connect-timeout 5 https://infra-lab-services.tail494f6d.ts.net/grafana/login
```

A successful response contains `HTTP/2 200`, `via: 1.1 Caddy`, and Grafana HTML with `<base href="/grafana/" />`. Do not test with only `https://10.1.10.2/`; Caddy requires the hostname/SNI `infra-lab-services.tail494f6d.ts.net`.

MagicDNS resolves the VM directly for tailnet devices, so `--resolve` is not required for normal access. To test the LAN path from `server-debian`, use `--resolve infra-lab-services.tail494f6d.ts.net:443:10.1.10.2` while keeping the hostname in the URL.

## Upgrade notes

Image tags are pinned in `.env.example`. Review release notes, back up the Grafana and Prometheus volumes, then change tags deliberately. The `.env` file contains the Grafana administrator password and must remain untracked.
