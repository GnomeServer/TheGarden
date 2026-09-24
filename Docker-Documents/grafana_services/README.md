# Grafana services

This Compose project deploys Grafana, Prometheus, and Node Exporter inside `infra-lab-services`. The Compose project name is `grafana_services`.

The containers join the existing Docker network used by Caddy instead of creating a private network. The default network name is:

```text
caddy_proxy
```

The network name can be changed with `CADDY_NETWORK` in `.env`, but it must be the exact network that the Caddy container joins. Docker Compose registers the Grafana service name as `grafana` on this network, allowing Caddy to resolve `grafana:3000` through Docker DNS.

No service ports are published directly to the VM LAN. Caddy is the HTTPS entry point.

## Current status

The stack is running on `infra-lab-services` and shares `caddy_proxy` with Caddy. The verified URL is:

```text
https://tail494f6d.ts.net/grafana/
```

A request using `--resolve tail494f6d.ts.net:443:10.1.10.2` returned `HTTP/2 200`, `Via: 1.1 Caddy`, and Grafana HTML. The live VM checkout is currently `~/grafana-service`; the repository directory is `grafana_services`. The current VM Compose output has used both `grafana-services` and the older `grafana-service` project names during migration. The project name affects container and volume names, not Docker DNS. Use one Compose project at a time and remove old `monitoring-*`/`grafana-service-*` containers from `caddy_proxy`.

## Deploy on `infra-lab-services`

Copy this directory to the VM, then run:

```bash
cd ~/grafana_services  # use the directory where this checkout was copied
cp .env.example .env
chmod 600 .env
nano .env
```

Set a strong `GRAFANA_ADMIN_PASSWORD`. Do not commit `.env`. Do not paste the output of `docker compose config` into tickets or chat because Compose expands and prints the password. If a password has already been exposed, rotate it.

Validate and start the stack:

```bash
sudo docker compose config
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

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

The services use these internal Docker names on the shared Caddy network:

```text
prometheus:9090
node-exporter:9100
grafana:3000
```

Grafana is provisioned automatically with Prometheus as its default data source.

## Caddy route

The initial setup uses Grafana under the existing Caddy hostname at:

```text
https://tail494f6d.ts.net/grafana/
```

The Caddyfile route is:

```caddyfile
@grafana {
    path /grafana /grafana/*
}

handle @grafana {
    reverse_proxy grafana:3000
}
```

Grafana is configured for the `/grafana/` subpath through `GF_SERVER_ROOT_URL` and `GF_SERVER_SERVE_FROM_SUB_PATH`.

Caddy and the Grafana container must be attached to the same Docker network. The Grafana service name `grafana` is automatically resolvable from Caddy on that network. If Caddy logs `lookup grafana ... no such host`, inspect the network membership before changing the upstream hostname:

```bash
sudo docker network inspect caddy_proxy \
  --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}'
```

The output must include both the Caddy container and the Grafana container. Do not leave old `monitoring-*` or `grafana-service-*` containers attached to the network, because multiple Grafana containers can create ambiguous Docker DNS results.

To remove stale containers without removing volumes:

```bash
sudo docker ps -a --format '{{.Names}}' | grep -E '^(monitoring-|grafana-service-)' | xargs -r sudo docker rm -f
```

Do not combine `docker ps -q` with `docker ps --format`; Docker ignores the custom format when `-q` is present.

If either current container is missing, make the Caddy Compose project join the external network explicitly:

```yaml
services:
  caddy:
    networks:
      - caddy_proxy

networks:
  caddy_proxy:
    external: true
    name: caddy_proxy
```

Use the same network name in both Compose projects, then recreate the containers:

```bash
sudo docker compose up -d --force-recreate
```

Then validate and reload Caddy. Run the following from the Caddy Compose project directory:

```bash
sudo docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose restart caddy
sudo docker compose logs --tail=100 caddy
```

Do not use the Grafana container name or a container IP as the permanent upstream. Docker service DNS on the shared network is the stable route.

This `grafana:3000` route assumes Caddy is also running in Docker. A native systemd Caddy process cannot use Docker's service DNS. In that setup, either move Caddy into the shared Docker network or deliberately publish Grafana only on loopback and change the native Caddy upstream to `127.0.0.1:3000`. Do not publish Grafana on the VM's LAN address.

Do not publish Grafana's port `3000` unless there is a specific temporary testing requirement.

## Test the Grafana URL

When testing, use the complete command on one physical line. A newline without a final backslash runs `curl` without a URL and then tries to execute the URL as a shell command:

```bash
curl -4 -k -i -L --connect-timeout 5 --resolve tail494f6d.ts.net:443:10.1.10.2 https://tail494f6d.ts.net/grafana/login
```

A successful response contains `HTTP/2 200`, `via: 1.1 Caddy`, and Grafana HTML with `<base href="/grafana/" />`. Do not test with only `https://10.1.10.2/`; Caddy requires the hostname/SNI `tail494f6d.ts.net`.

The `--resolve` option only overrides DNS for one request. The current `tail494f6d.ts.net` record identifies the Proxmox/Tailscale host, not the Caddy VM. For browser access without `--resolve`, create a DNS/hosts entry pointing the client to `10.1.10.2`, configure a subnet route through `server-debian`, or give the VM its own Tailscale identity. A hosts entry on `server-debian` does not affect `donatello` or any other client.

## Upgrade notes

Image tags are pinned in `.env.example`. Review release notes, back up the Grafana and Prometheus volumes, then change tags deliberately. The `.env` file contains the Grafana administrator password and must remain untracked.
