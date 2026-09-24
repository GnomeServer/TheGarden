# Forgejo

This Compose project deploys Forgejo 16.0.5 with PostgreSQL on `infra-lab-services`.
The deployment is served through the Caddy reverse proxy at:

```text
https://infra-lab-services.tail494f6d.ts.net/forgejo/
```

## Architecture

- Caddy and Forgejo share the external Docker network `caddy_proxy`.
- Caddy reaches Forgejo at the Docker service name `forgejo:3000`.
- Forgejo's HTTP port `3000` is not published to the VM or LAN.
- PostgreSQL is reachable only on the private `forgejo_internal` network.
- Git SSH access is bound only to `100.94.49.45:2222`, the VM's Tailscale address.
- The Forgejo image's OpenSSH service listens on container port `22`.

The Compose file disables Forgejo's embedded SSH server because the image already
starts OpenSSH on port `22`. Enabling both SSH servers causes a bind failure and
makes the Forgejo container restart continuously:

```text
Failed to start SSH server: listen tcp :22: bind: address already in use
```

The host's normal SSH service on port `22` is unrelated to the container port.
The published mapping is:

```text
100.94.49.45:2222 -> forgejo:22
```

## Deployment

The Caddy project must be running first and must own the external Docker network
`caddy_proxy`.

For a new deployment, copy this directory to the VM and create the environment
file:

```bash
cd ~/forgejo-service
cp .env.example .env
chmod 600 .env
nano .env
```

Set a unique, long random database password:

```dotenv
POSTGRES_PASSWORD=use-a-long-random-database-password
```

The `.env` file contains database credentials and must not be committed. Do not
paste the output of `docker compose config` into chat or tickets because it
expands secrets.

Validate and start the project:

```bash
sudo docker compose config
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps -a
```

Check the services and shared network:

```bash
sudo docker compose logs --tail=100 postgres
sudo docker compose logs --tail=100 forgejo
sudo docker network inspect caddy_proxy --format '{{range .Containers}}{{println .Name}}{{end}}'
```

The network output should include `caddy` and `forgejo-forgejo-1`. Keep the
network-inspect command on one physical line; a backslash followed by spaces
causes the shell to run `--format` as a separate command.

## First Forgejo setup

Open the following URL from a Tailscale device:

```text
https://infra-lab-services.tail494f6d.ts.net/forgejo/
```

The database settings are supplied through Compose. If the installer displays
them, use:

```text
Database type: PostgreSQL
Host:          postgres:5432
Database name: forgejo
User:          forgejo
Password:      POSTGRES_PASSWORD from .env
```

Create the first administrator account during initial setup. After confirming
that the administrator can log in, set these values in `.env`:

```dotenv
FORGEJO_DISABLE_REGISTRATION=true
FORGEJO_REQUIRE_SIGNIN_VIEW=true
```

Apply the security settings:

```bash
sudo docker compose up -d --force-recreate forgejo
```

Keep new repositories private by default. Create separate human and automation
accounts rather than sharing the administrator account with workers.

## Git access

HTTPS clone URLs use the `/forgejo/` path:

```text
https://infra-lab-services.tail494f6d.ts.net/forgejo/<owner>/<repository>.git
```

SSH clone access uses the VM's Tailscale hostname and port `2222`:

```text
ssh://git@infra-lab-services.tail494f6d.ts.net:2222/<owner>/<repository>.git
```

Test the Forgejo SSH endpoint after adding an SSH key to the Forgejo account:

```bash
ssh -T -p 2222 git@infra-lab-services.tail494f6d.ts.net
```

`FORGEJO__server__SSH_PORT=2222` controls the port advertised in clone URLs;
it does not change the container's internal SSH listener. The container's
OpenSSH service listens on port `22`, which is published as host port `2222`.

## Caddy route

The Forgejo route must appear before Caddy's fallback route:

```caddyfile
@forgejo {
    path /forgejo /forgejo/*
}

handle @forgejo {
    uri strip_prefix /forgejo
    reverse_proxy forgejo:3000
}
```

The prefix is stripped before proxying because Forgejo's `ROOT_URL` includes
`/forgejo/`. Caddy and Forgejo must both be attached to `caddy_proxy`.

Validate and reload Caddy from the Caddy project directory:

```bash
cd ~/caddy-service
sudo docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
sudo docker compose logs --tail=100 caddy
```

Do not route PostgreSQL, Forgejo SSH, or the Forgejo administration API
separately through Caddy.

## Troubleshooting

Check whether Forgejo is stable:

```bash
sudo docker compose ps -a
sudo docker compose logs --tail=200 --timestamps forgejo
```

If the logs contain `address already in use` for port `22`, confirm that the
Compose environment contains:

```yaml
FORGEJO__server__START_SSH_SERVER: "false"
```

Then recreate the container:

```bash
sudo docker compose up -d --force-recreate forgejo
```

If Caddy returns `502 Bad Gateway`, verify that Forgejo is running and present
on the shared network:

```bash
sudo docker network inspect caddy_proxy --format '{{range .Containers}}{{println .Name}}{{end}}'
```

From the Caddy project, inspect the proxy error and test Docker DNS:

```bash
cd ~/caddy-service
sudo docker compose logs --tail=100 caddy
sudo docker compose exec caddy getent hosts forgejo
sudo docker compose exec caddy wget -S -O- http://forgejo:3000/
```

`no such host` indicates a Docker network or service-name problem. `connection
refused` indicates that Forgejo is restarting or is not listening on port `3000`.

## Backups

Forgejo requires both volumes for a complete restore:

```text
forgejo_data      Git repositories, attachments, packages, configuration
postgres_data     Forgejo database
```

Back up both volumes and export the Compose `.env` values through the lab's
secret-management process. A volume backup without the database is not a
complete Forgejo backup.

## Future work

- Add a dedicated Forgejo OCI registry workflow.
- Add Forgejo Actions runners on worker machines.
- Add PostgreSQL backup and restore verification.
- Add branch protection and separate bot permissions.
