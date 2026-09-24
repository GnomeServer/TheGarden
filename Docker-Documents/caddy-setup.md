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

The VM is connected to the Proxmox bridge and has its own network identity. Tailscale installed on the Proxmox host does not automatically make the VM a Tailscale node. The VM can currently reach the Proxmox host over the LAN address after the host firewall path was allowed.

The repository previously documented the VM as `10.1.0.2`. The address confirmed on the running VM is `10.1.10.2`; update the Ansible inventory and other documentation before enabling automated guest management.

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

## Current Proxmox test route

The working test configuration is:

```caddyfile
tail494f6d.ts.net {
    tls internal

    reverse_proxy https://10.1.10.156:8006 {
        transport http {
            tls_insecure_skip_verify
        }
    }
}
```

Notes:

- `10.1.10.156` is the Proxmox host, not `10.1.10.1`.
- `10.1.10.1` is the LAN gateway/router.
- The upstream URL uses `https://` because Proxmox serves HTTPS on port `8006`.
- Caddy's transport module remains `http`; the upstream URL controls the TLS connection.
- `tls_insecure_skip_verify` is required because the Proxmox certificate is locally issued/self-signed.
- `tls internal` causes Caddy to issue a private certificate. Clients must trust Caddy's local CA or show a browser warning.
- The domain used is currently the MagicDNS provided by Tailscale. This will need to be changed once other services can expand from caddy (grafana, etc).

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

## Testing the Caddy frontend

From the Proxmox host, force the Caddy hostname to resolve to the VM:

```bash
curl -k --resolve tail494f6d.ts.net:443:10.1.10.2 \
  https://tail494f6d.ts.net/
```

From inside the VM, if Caddy publishes port `443` on the VM host:

```bash
curl -k --resolve tail494f6d.ts.net:443:127.0.0.1 \
  https://tail494f6d.ts.net/
```

A successful response should be the Proxmox page returned through Caddy. A `502 Bad Gateway` means the Caddy frontend is running but cannot reach the configured upstream.

The `--resolve` option is only a test override. For normal browser access, DNS, Tailscale MagicDNS, or a local hosts/DNS record must resolve the chosen hostname to `10.1.10.2` or to the VM's future Tailscale address.

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

## Optional: give the VM its own Tailscale identity

The architecture expects the infrastructure VM to have its own Tailscale node. This is useful for accessing Caddy from the tailnet and avoids depending on LAN routing:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=infra-01
tailscale status
tailscale ip -4
```

After authentication, test the Proxmox host over Tailscale:

```bash
curl -4 -vk --connect-timeout 5 https://100.102.154.23:8006/
```

If this path is used, the Caddy upstream can be changed to:

```caddyfile
reverse_proxy https://100.102.154.23:8006 {
    transport http {
        tls_insecure_skip_verify
    }
}
```

Tailscale ACLs must allow the VM's identity to reach the Proxmox host on port `8006`.

## Next services

Once the Caddy frontend and VM networking are stable, deploy the control-plane services in this order:

1. PostgreSQL
2. Forgejo
3. Forgejo OCI registry
4. Prometheus
5. Grafana
6. NATS JetStream
7. Open WebUI

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
