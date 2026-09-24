# Caddy reverse proxy on `infra-lab-services`

This document records the Caddy and Proxmox connectivity work completed for the first infrastructure VM.

## Current topology

```text
Physical Proxmox host: server-debian
  LAN address:       10.1.10.156
  Tailscale address: 100.102.154.23
  Proxmox UI:        https://10.1.10.156:8006

VM 100: infra-lab-services
  Debian guest
  Interface:         ens18
  LAN address:       10.1.10.2/24
  Gateway:           10.1.10.1
  Caddy:             ports 80 and 443
```

The VM is connected to the Proxmox bridge and has its own network identity. The VM can reach the Proxmox host over the LAN address after the host firewall path was allowed. Tailscale is now also installed on the VM.

The address confirmed on the running VM is `10.1.10.2`. The Ansible guest inventory template and related documentation use this address; the guest remains inactive as an Ansible target until SSH credentials are intentionally configured.

## Current validated status

The current deployment uses Docker Compose on the VM:

```text
Caddy project:    ~/caddy-service
Grafana project:  ~/grafana-service
Forgejo project:  ~/forgejo-service (prepared, not yet deployed)
Docker network:   caddy_proxy
Caddy hostname:   infra-lab-services.tail494f6d.ts.net
VM Tailscale:     100.94.49.45
Grafana URL:      https://infra-lab-services.tail494f6d.ts.net/grafana/
Forgejo URL:      https://infra-lab-services.tail494f6d.ts.net/forgejo/ (after deploy)
```

Caddy and the Grafana containers are attached to `caddy_proxy`. The configured application hostname is `infra-lab-services.tail494f6d.ts.net`, with Grafana under `/grafana/`.

The VM is a Tailscale node named `infra-lab-services` at `100.94.49.45`. Other tailnet devices should use the VM's MagicDNS hostname directly; no hosts override or `--resolve` option is required when MagicDNS is working.

A direct request to the VM's Tailscale MagicDNS name returned `HTTP/2 200`, `Via: 1.1 Caddy`, and Grafana HTML with `<base href="/grafana/" />`. This verifies TLS, Caddy routing, Docker service discovery, and Grafana's `/grafana/` subpath configuration.

## Important architecture note

Proxmox administration does not need to pass through Caddy. The preferred administration path is direct access over Tailscale:

```text
Administrator laptop
  -> Tailscale
  -> server-debian / 100.102.154.23:8006
  -> Proxmox web interface
```

Caddy is intended to be the HTTPS entry point for application services running in the VM, such as Forgejo, Grafana, and Open WebUI. The Proxmox route below is useful as a connectivity test while bringing up the VM and Caddy.

## Files

The repository source files are:

```text
Docker-Documents/Caddyfile
Docker-Documents/caddy-setup.md
```

On the VM, the Caddyfile should be mounted into the Caddy container, normally at:

```text
/etc/caddy/Caddyfile
```

A Compose project should contain the Caddyfile and a `compose.yml`, for example:

```text
caddy-service/
├── Caddyfile
└── compose.yml
```

## Current Caddy routes

The Caddyfile routes Grafana under `/grafana/`, Forgejo under `/forgejo/`, and keeps Proxmox as the fallback route:

```caddyfile
infra-lab-services.tail494f6d.ts.net {
    tls internal

    @grafana {
        path /grafana /grafana/*
    }

    handle @grafana {
        reverse_proxy grafana:3000
    }

    @forgejo {
        path /forgejo /forgejo/*
    }

    handle @forgejo {
        reverse_proxy forgejo:3000
    }

    handle {
        reverse_proxy https://10.1.10.156:8006 {
            transport http {
                tls_insecure_skip_verify
            }
        }
    }
}
```

Important details:

- `infra-lab-services.tail494f6d.ts.net` must match the hostname used in the request's SNI. The old `tail494f6d.ts.net` alias and `server-debian.tail494f6d.ts.net` are not the configured Caddy site addresses.
- `grafana:3000` and `forgejo:3000` are Docker service names. They resolve only when Caddy shares `caddy_proxy` with the corresponding service.
- `10.1.10.156` is the Proxmox host, not `10.1.10.1`.
- `10.1.10.1` is the LAN gateway/router.
- Proxmox serves HTTPS on port `8006`, so the upstream URL uses `https://`.
- `tls_insecure_skip_verify` is required because the Proxmox certificate is locally issued/self-signed.
- `tls internal` causes Caddy to issue a private certificate. Clients must trust Caddy's local CA or use `curl -k` during testing.

## Connecting to the VM

The VM can be reached from the Proxmox host after SSH is enabled:

```bash
ssh infra-lab-user@10.1.10.2
```

Confirm that the shell is inside the VM before testing the backend:

```bash
hostname
ip -br addr
```

Expected values include:

```text
hostname: infra-lab-services
address:  10.1.10.2
interface: ens18
```

The Proxmox host itself has `vmbr0` and `tailscale0`; those interfaces should not be mistaken for the guest's interfaces.

## Backend connectivity test

Run this **inside the Caddy VM**:

```bash
ping -c 3 10.1.10.156
curl -4 -vk --connect-timeout 5 https://10.1.10.156:8006/
```

A successful result contains:

```text
HTTP/1.1 200 OK
Server: pve-api-daemon/3.0
```

and HTML containing:

```text
Proxmox Virtual Environment
```

`ping` alone is not enough. It only tests ICMP. The `curl` command verifies TCP port `8006`, TLS, and HTTP.

Use `-k` because Proxmox's certificate is not trusted by the Debian guest by default. Do not use `curl -I` for this test; Proxmox may return `501 method 'HEAD' not available` even when the connection is healthy.

## Caddy operations

Run these commands from the directory containing the Caddy Compose file:

```bash
docker compose ps
docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
docker compose restart caddy
docker compose logs --tail=100 caddy
```

If Caddy is installed as a native system service instead of a container, use:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo journalctl -u caddy -n 100 --no-pager
```

A container showing `Started` does not prove that Caddy loaded the intended configuration or completed the TLS handshake. For an error such as `curl: (35) ... tlsv1 alert internal error`, inspect Caddy before changing the Grafana route:

```bash
sudo docker compose ps
sudo docker compose logs --tail=200 caddy
sudo docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
openssl s_client -connect 10.1.10.2:443 -servername infra-lab-services.tail494f6d.ts.net -brief </dev/null
```

The mounted Caddyfile must contain `infra-lab-services.tail494f6d.ts.net`. A direct request to `https://10.1.10.2/` can also produce a TLS alert because it does not send the hostname/SNI that Caddy is configured to serve. Always test with the MagicDNS hostname or use `--resolve` when deliberately targeting the LAN address.

The TLS error occurs before Caddy contacts Grafana, so it is separate from Docker DNS. If Caddy logs a storage, certificate, or permission error, fix that error first.

## Testing the Caddy frontend

From any tailnet device, use the VM's MagicDNS hostname and keep this command on one physical line:

```bash
curl -4 -k -i -L --connect-timeout 5 https://infra-lab-services.tail494f6d.ts.net/grafana/login
```

To test the LAN path from `server-debian` while preserving the correct SNI, use:

```bash
curl -4 -k -i -L --connect-timeout 5 --resolve infra-lab-services.tail494f6d.ts.net:443:10.1.10.2 https://infra-lab-services.tail494f6d.ts.net/grafana/login
```

A successful response contains:

```text
HTTP/2 200
via: 1.1 Caddy
content-type: text/html; charset=UTF-8
```

and Grafana HTML with:

```html
<base href="/grafana/" />
```

A `502 Bad Gateway` means Caddy is running but cannot reach Grafana. A TLS alert when using the IP directly is expected because the request does not provide the configured hostname/SNI:

```bash
# This is not a valid Grafana test:
curl -k -vk https://10.1.10.2/
```

The `--resolve` option is only a LAN-path test override. The current canonical MagicDNS name resolves directly to the VM's Tailscale address. A normal browser request from a tailnet device should use `https://infra-lab-services.tail494f6d.ts.net/grafana/login` without a hosts entry.

If Caddy logs `lookup grafana ... no such host`, inspect the shared network:

```bash
sudo docker network inspect caddy_proxy --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}'
```

The output should contain `caddy` and the current `grafana-services-*` containers. Do not leave old `monitoring-*` or `grafana-service-*` containers attached to the network, because multiple Grafana containers can create ambiguous Docker DNS results.

## Firewall findings

During testing:

- `pveproxy` was listening on `*:8006`.
- `sudo pve-firewall status` reported `disabled/running`.
- `sudo ufw status verbose` reported `Status: inactive`.
- The nftables ruleset nevertheless contained an `INPUT` chain with a default `drop` policy and explicit Tailscale rules.
- The Proxmox host could access `10.1.10.156:8006` locally, while the VM initially timed out.
- This demonstrated that a local host test does not prove that a guest can reach the host.

### Firewall and iptables commands used during troubleshooting

These commands were run on `server-debian` while diagnosing the VM-to-Proxmox connection:

Check that Proxmox is listening:

```bash
sudo ss -lntp | grep ':8006'
```

Check the Proxmox firewall and UFW status:

```bash
sudo pve-firewall status
sudo ufw status verbose
```

Inspect the nftables rules that were actually present:

```bash
sudo nft list ruleset | grep -E -C 4 '8006|drop|reject'
```

The host showed a default-drop `INPUT` policy, even though UFW reported inactive. This is why a successful ping did not prove that TCP port `8006` was allowed.

Temporary allow-rule commands were tested for the Caddy VM (`10.1.10.2`) to reach Proxmox (`10.1.10.156`):

```bash
sudo iptables -I INPUT 1 -i vmbr0 -s 10.1.10.2 -d 10.1.10.156 -p tcp --dport 8006 -m conntrack --ctstate NEW -j ACCEPT
```

Because VM traffic may enter through Proxmox firewall bridge interfaces rather than `vmbr0`, a source/destination-specific rule without an interface restriction was also tested:

```bash
sudo iptables -I INPUT 1 -s 10.1.10.2 -d 10.1.10.156 -p tcp --dport 8006 -j ACCEPT
```

The system reported both legacy and nftables iptables tooling. These inspection commands were used to identify which backend contained the active rules:

```bash
sudo iptables -L INPUT -n -v --line-numbers
sudo iptables -L OUTPUT -n -v --line-numbers
sudo iptables-save | grep 8006
sudo iptables-nft -L INPUT -n -v --line-numbers
sudo iptables-legacy -L INPUT -n -v --line-numbers
sudo nft list chain ip filter INPUT
```

A direct nftables rule was also tested as a temporary diagnostic when the active nftables chain appeared to contain the default drop policy:

```bash
sudo nft insert rule ip filter INPUT ip saddr 10.1.10.2 ip daddr 10.1.10.156 tcp dport 8006 counter accept
```

The complete command must be entered on one line. Do not press Enter after `tcp dport` or paste Markdown fences into the shell.

These rules were diagnostic/runtime changes, not a confirmed persistent firewall configuration. Before rebooting, select one authoritative firewall system and persist the minimum required rule. Do not mix UFW, legacy iptables, iptables-nft, direct nftables, and Proxmox firewall configuration without documenting which system owns the rules.

Do not assume that `ufw inactive` means that no filtering is active. Before making firewall changes, inspect the active ruleset:

```bash
sudo nft list ruleset
sudo iptables-nft -L INPUT -n -v --line-numbers
sudo iptables-legacy -L INPUT -n -v --line-numbers
```

The system showed both legacy and nftables/iptables-nft tooling. Use one deliberate firewall management method and make the final rule persistent. Avoid mixing temporary `iptables`, `iptables-nft`, direct `nft`, UFW, and Proxmox firewall changes without documenting which backend is authoritative.

For an initial test, allow only the Caddy VM to reach the Proxmox HTTPS port. The source and destination are:

```text
source:      10.1.10.2
host:        10.1.10.156
port:        TCP 8006
```

After confirming the rule works, persist it using the selected host firewall system. Do not expose port `8006` publicly.

## Tailscale status

The VM now has its own Tailscale identity:

```text
hostname:  infra-lab-services.tail494f6d.ts.net
address:   100.94.49.45
```

Other tailnet devices can reach Caddy directly through this MagicDNS name. Check the identity and reachability with:

```bash
tailscale status
tailscale ping infra-lab-services
```

Test Grafana from another tailnet device with:

```bash
curl -k -i -L https://infra-lab-services.tail494f6d.ts.net/grafana/login
```

Caddy's internal CA still needs to be trusted by browsers, or `curl -k` can be used for testing.

## Next services

Caddy, Grafana, Prometheus, and Node Exporter are deployed and the Grafana route has been verified. The Forgejo/PostgreSQL Compose project is now prepared; deploy it before adding the OCI registry and automation services:

1. Forgejo with PostgreSQL
2. Forgejo OCI registry validation
3. NATS JetStream
4. Open WebUI

Caddy should then route application hostnames to internal Compose services, for example:

```caddyfile
forgejo.example.internal {
    reverse_proxy forgejo:3000
}

grafana.example.internal {
    reverse_proxy grafana:3000
}

ai.example.internal {
    reverse_proxy open-webui:8080
}
```

PostgreSQL, NATS, Docker APIs, and Garage administrative interfaces should not be exposed through Caddy. Garage and the backup workflow are deferred until the basic control plane is working.
