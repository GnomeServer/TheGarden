# Short-lived Proxmox VM smoke test

This runbook repeats the manual Proxmox lifecycle test from the `infra-lab-services` VM. It clones the cloud-init template, starts one temporary VM, checks that QEMU reports it as running, and deletes the clone and its disks.

This is **not** a mission execution test. It does not install or start `worker.py` in the guest, consume a NATS job, test worker heartbeats, or test automatic provisioning from a mission queue.

## Tested topology

| Component | Tested value |
| --- | --- |
| Machine running the commands | `infra-lab-services` VMID `100` |
| Proxmox node | `server-debian` |
| Proxmox Tailnet IP | `100.102.154.23` |
| Proxmox API port | `8006` |
| API URL used by the test | `https://server-debian:8006` |
| Source template | VMID `101`, `ephemeral-worker-base` |
| Storage | `local` |
| Guest bridge | `vmbr0` |

The VM running this runbook is VMID `100`. Template VMID `101` is on Proxmox and is the clone source. The smoke script chooses a new VMID; it never uses `100` or `101` as the clone target.

Update these values if the Proxmox node, Tailnet, template, storage, or bridge changes on the larger server.

## Safety and token scope

- Use an API token, not the Proxmox root password. Proxmox generates the token secret once; do not put it in this repository, a committed `.env`, chat, or logs.
- A token with `privsep=1` needs token-specific ACLs. Regenerating its secret does not change its permissions. Check the token expiration as well: an expired token returns HTTP 401.
- This smoke test was made to work with VM-management, storage-allocation, and bridge-use privileges. `PVEVMAdmin` on `/vms` can manage **all VMs**, including VMID `100`; it is a temporary test grant, not a production scope.
- The current smoke adapter uses dynamic VMIDs and does not assign a resource pool. Before production use, use a dedicated provisioning identity and a pool-scoped design rather than leaving broad `/vms` VM-admin rights on a service token.
- The script cleans up VMs it has claimed in a `finally` block. If the API connection drops during clone submission, check the Proxmox VM list for a leftover `mission-smoke-*` VM before retrying.

## 1. Verify the Proxmox host

Run these commands as root on the **Proxmox host**, not inside VMID `100`:

```bash
hostname
systemctl is-active pveproxy
ss -lntp 'sport = :8006'
qm config 101
pvesm status
```

Confirm the node is `server-debian`, `pveproxy` is active and listening on port `8006`, VMID `101` has `template: 1`, and storage `local` is active with VM images enabled. Confirm `net0` on the template uses `bridge=vmbr0`.

The host certificate used during the test had `DNS:server-debian` in its subject alternative names (SAN), but did **not** contain the Tailnet IP. Inspect the current certificate and root CA on the host:

```bash
openssl x509 -in /etc/pve/local/pve-ssl.pem \
  -noout -subject -issuer -ext subjectAltName
openssl x509 -in /etc/pve/pve-root-ca.pem -noout -fingerprint -sha256
```

## 2. Verify Tailnet DNS and trust the Proxmox CA

Run these commands in the **`infra-lab-services` VM** where the checkout and smoke script live:

```bash
getent ahostsv4 server-debian
ip route get 100.102.154.23
```

For the tested setup, `server-debian` resolved to `100.102.154.23` and the route used `tailscale0`. Use the certificate hostname in `PROXMOX_API_URL`; connecting to the raw Tailnet IP causes a certificate-name mismatch.

Copy the public Proxmox root CA certificate to the VM, then compare its fingerprint with the one printed on the Proxmox host:

```bash
scp root@100.102.154.23:/etc/pve/pve-root-ca.pem "$HOME/pve-root-ca.pem"
openssl x509 -in "$HOME/pve-root-ca.pem" -noout -fingerprint -sha256
```

If root SSH is unavailable, transfer only `/etc/pve/pve-root-ca.pem` through an approved secure method and compare the fingerprints. Never copy `/etc/pve/priv/pve-root-ca.key`.

## 3. Create or verify a Proxmox API token

On the Proxmox host, list the token for the chosen API user. This does not reveal its secret:

```bash
pveum user token list USER@REALM
```

If a new token is needed, create it on the Proxmox host and save the one-time secret locally:

```bash
pveum user token add USER@REALM TOKEN_ID --privsep 1
```

The token ID used by the script is `USER@REALM!TOKEN_ID`. The secret is only the generated secret value—not the token ID, user password, or full `PVEAPIToken=...` header. Keep the token expiration in the future; for a 90-day test token, the host can set it with:

```bash
pveum user token modify USER@REALM TOKEN_ID \
  --expire "$(date -u -d '+90 days' +%s)"
```

With `privsep=1`, the token's effective rights are the intersection of the user's rights and the token's ACLs. Grant only the test permissions needed. The tested roles were:

```bash
pveum acl modify /vms \
  -token 'USER@REALM!TOKEN_ID' -role PVEVMAdmin
pveum acl modify /storage/local \
  -token 'USER@REALM!TOKEN_ID' -role PVEDatastoreUser
pveum acl modify /sdn/zones/localnetwork/vmbr0 \
  -token 'USER@REALM!TOKEN_ID' -role PVESDNUser
```

For a non-root API user, the user itself must also have the corresponding rights; a separated token cannot exceed its user's rights. Inspect effective token permissions on the host:

```bash
pveum user token permissions USER@REALM TOKEN_ID
```

For the tested template clone, the relevant privileges are `VM.Clone` on the source, `VM.Allocate` on the dynamic target VMID, `Datastore.AllocateSpace` on `local`, and `SDN.Use` on the template's bridge (`vmbr0`). The bridge ACL is required by Proxmox's clone check even when the VM/storage permissions are present.

## 4. Prepare the Python environment and connection settings

Run in the `infra-lab-services` VM, in a new shell if needed:

```bash
cd ~/TheGarden-ephemeral-mission-vms/agent-worker
mkdir -p "$HOME/.venvs"
python3 -m venv "$HOME/.venvs/proxmox-smoke"
. "$HOME/.venvs/proxmox-smoke/bin/activate"
python -m pip install -r requirements.txt

export PROXMOX_API_URL='https://server-debian:8006'
export PROXMOX_API_TOKEN_ID='USER@REALM!TOKEN_ID'
read -rsp 'Proxmox API token secret: ' PROXMOX_API_TOKEN_SECRET
printf '\n'
export PROXMOX_API_TOKEN_SECRET
export PROXMOX_CA_BUNDLE="$HOME/pve-root-ca.pem"
export PROXMOX_NODE='server-debian'
export PROXMOX_TEMPLATE_VMID='101'
export PROXMOX_STORAGE='local'
export PROXMOX_BRIDGE='vmbr0'
```

Enter the secret at the hidden prompt. All variables are local to this shell; repeat these exports after opening another terminal. Do not use `set -e` in an interactive SSH shell for this runbook: a nonzero command can exit the shell and close the SSH session.

## 5. Run a read-only preflight

This confirms that the configured token can read the template. It creates no VM:

```bash
python3 - <<'PY'
import asyncio
from proxmox import ProxmoxClient
from proxmox_smoke import config_from_env

async def check():
    async with ProxmoxClient(config_from_env()) as client:
        await client.verify_template()

asyncio.run(check())
print("Authenticated; template config is readable.")
PY
```

Proceed only if it prints `Authenticated; template config is readable.` An unauthenticated `curl` to `/api2/json/version` returned HTTP 401 in this setup; use this authenticated preflight instead.

## 6. Run and verify the smoke test

From the same VM and shell:

```bash
python3 proxmox_smoke.py
```

Success prints a message that the isolated VM started, followed by a message that the test VM and cloned disks were deleted. Verify from a separate shell on the Proxmox host:

```bash
qm list
```

There should be no leftover `mission-smoke-*` VM. If the command fails, the API error includes the method and endpoint path. HTTP 401 means the token ID/secret or expiry is wrong; HTTP 403 means an ACL/privilege check failed. A 403 at `/clone` can indicate missing `SDN.Use` for the template's bridge. Do not paste token secrets into troubleshooting logs or chat.

## 7. After the test

If these ACLs were added only for this test and no further smoke tests are planned, remove the token-specific grants and revoke a temporary token. Remove only ACLs created for the test:

```bash
pveum acl delete /vms \
  -token 'USER@REALM!TOKEN_ID' -roles PVEVMAdmin
pveum acl delete /storage/local \
  -token 'USER@REALM!TOKEN_ID' -roles PVEDatastoreUser
pveum acl delete /sdn/zones/localnetwork/vmbr0 \
  -token 'USER@REALM!TOKEN_ID' -roles PVESDNUser
pveum user token remove USER@REALM TOKEN_ID
```

The smoke test validates only the Proxmox clone/start/status/delete path. It does not repair or test the separate NATS heartbeat path for existing worker nodes.
