# Forgejo

This Compose project deploys Forgejo with PostgreSQL on `infra-lab-services`.

Forgejo is served through Caddy at:

```text
https://infra-lab-services.tail494f6d.ts.net/forgejo/
```

The Forgejo web service and PostgreSQL do not publish HTTP or database ports to the VM LAN. Forgejo SSH clone access is bound only to the VM's Tailscale address on port `2222`.

## Deployment

The Caddy project must already be running and must own the external Docker network `caddy_proxy`. The repository Caddyfile already includes the `/forgejo/` route, but the updated Caddyfile must be copied to the VM and reloaded before Forgejo will be reachable.

Copy this directory to the VM, then run:

```bash
cd ~/forgejo-service
cp .env.example .env
chmod 600 .env
nano .env
```

Set both secrets before starting:

```dotenv
POSTGRES_PASSWORD=use-a-long-random-database-password
```

The `.env` file contains database credentials and must not be committed. Do not paste the output of `docker compose config` into chat or tickets because it expands the secrets.

Validate and start the project:

```bash
sudo docker compose config
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

Check the initial logs:

```bash
sudo docker compose logs --tail=100 postgres
sudo docker compose logs --tail=100 forgejo
```

## First Forgejo setup

Open the following URL from a Tailscale device:

```text
https://infra-lab-services.tail494f6d.ts.net/forgejo/
```

The database settings are already supplied through Compose. If the installer displays them, use:

```text
Database type: PostgreSQL
Host:          postgres:5432
Database name: forgejo
User:          forgejo
Password:      POSTGRES_PASSWORD from .env
```

Create the first administrator account during the initial setup. After confirming that the administrator can log in, change these values in `.env`:

```dotenv
FORGEJO_DISABLE_REGISTRATION=true
FORGEJO_REQUIRE_SIGNIN_VIEW=true
```

Apply the security settings:

```bash
sudo docker compose up -d --force-recreate forgejo
```

Keep new repositories private by default. Create separate human and automation accounts rather than sharing the administrator account with workers.

## Git access

HTTPS clone URLs use the `/forgejo/` path:

```text
https://infra-lab-services.tail494f6d.ts.net/forgejo/<owner>/<repository>.git
```

SSH clone access uses the VM's Tailscale hostname and port `2222`:

```text
ssh://git@infra-lab-services.tail494f6d.ts.net:2222/<owner>/<repository>.git
```

The host's normal SSH service remains on port `22`. Forgejo's embedded SSH server is published separately on `2222` and only on `100.94.49.45`.

Test the Forgejo SSH endpoint after adding an SSH key to the Forgejo account:

```bash
ssh -T -p 2222 git@infra-lab-services.tail494f6d.ts.net
```

## Caddy route

Add the Forgejo route before Caddy's fallback route:

```caddyfile
@forgejo {
    path /forgejo /forgejo/*
}

handle @forgejo {
    reverse_proxy forgejo:3000
}
```

Caddy and Forgejo must both be attached to `caddy_proxy`. Validate and reload Caddy from the Caddy project directory:

```bash
sudo docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose restart caddy
sudo docker compose logs --tail=100 caddy
```

Do not route PostgreSQL, Forgejo SSH, or the Forgejo administration API separately through Caddy.

## Backups

Forgejo requires both volumes for a complete restore:

```text
forgejo_data      Git repositories, attachments, packages, configuration
postgres_data     Forgejo database
```

Back up both volumes and export the Compose `.env` values through the lab's secret-management process. A volume backup alone without the database is not a complete Forgejo backup.

## Future work

- Add a dedicated Forgejo OCI registry workflow.
- Add Forgejo Actions runners on worker machines.
- Add PostgreSQL backup and restore verification.
- Add branch protection and separate bot permissions.
