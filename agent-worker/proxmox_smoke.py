#!/usr/bin/env python3
"""Clone, boot, and delete one isolated Proxmox VM from the configured template."""

from __future__ import annotations

import asyncio
import os
import uuid

from proxmox import ProxmoxClient, ProxmoxConfig


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} in the local environment")
    return value


def config_from_env() -> ProxmoxConfig:
    return ProxmoxConfig(
        base_url=required_env("PROXMOX_API_URL"),
        token_id=required_env("PROXMOX_API_TOKEN_ID"),
        token_secret=required_env("PROXMOX_API_TOKEN_SECRET"),
        node=os.getenv("PROXMOX_NODE", "server-debian"),
        template_vmid=int(os.getenv("PROXMOX_TEMPLATE_VMID", "101")),
        storage=os.getenv("PROXMOX_STORAGE", "local"),
        bridge=os.getenv("PROXMOX_BRIDGE", "vmbr0"),
        ca_bundle=os.getenv("PROXMOX_CA_BUNDLE") or None,
    )


async def smoke() -> None:
    config = config_from_env()
    run_id = f"smoke-{uuid.uuid4()}"
    async with ProxmoxClient(config) as client:
        try:
            vmid = await client.provision(run_id, cores=1, memory_mb=1024)
            status = await client.vm_status(vmid)
            if status.get("status") != "running":
                raise RuntimeError("Proxmox VM did not reach running state")
            print(f"Started isolated test VMID {vmid} from template {config.template_vmid}.")
        finally:
            for owned_vmid in sorted(client.owned_vmids):
                await client.cleanup(owned_vmid)
                print(f"Deleted test VMID {owned_vmid} and its cloned disks.")


if __name__ == "__main__":
    asyncio.run(smoke())
