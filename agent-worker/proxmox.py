from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


class ProxmoxError(RuntimeError):
    """A Proxmox API request failed or returned an invalid result."""


@dataclass(frozen=True)
class ProxmoxConfig:
    base_url: str
    token_id: str
    token_secret: str
    node: str
    template_vmid: int = 101
    storage: str = "local"
    bridge: str = "vmbr0"
    request_timeout: float = 10.0
    task_timeout: float = 300.0
    poll_interval: float = 2.0
    shutdown_timeout: float = 30.0
    ca_bundle: str | None = None

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://"):
            raise ValueError("Proxmox API base_url must use HTTPS")
        if not self.token_id.strip() or not self.token_secret.strip():
            raise ValueError("Proxmox API token ID and secret are required")
        for label, value in (("node", self.node), ("storage", self.storage), ("bridge", self.bridge)):
            if not value or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
                raise ValueError(f"Invalid Proxmox {label}")
        if self.template_vmid <= 100 or self.template_vmid == 100:
            raise ValueError("Template VMID must be greater than protected VMID 100")
        for label, value in (("request_timeout", self.request_timeout), ("task_timeout", self.task_timeout),
                             ("poll_interval", self.poll_interval), ("shutdown_timeout", self.shutdown_timeout)):
            if not math.isfinite(value) or value <= 0 or value > 3600:
                raise ValueError(f"{label} must be between 0 and 3600 seconds")
        if self.ca_bundle is not None and not Path(self.ca_bundle).is_file():
            raise ValueError("CA bundle path must refer to a readable file")


class ProxmoxClient:
    def __init__(self, config: ProxmoxConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._owned_vmids: set[int] = set()
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/api2/json",
            headers={"Authorization": f"PVEAPIToken={config.token_id}={config.token_secret}"},
            verify=config.ca_bundle if config.ca_bundle else True,
            timeout=httpx.Timeout(config.request_timeout),
            transport=transport,
        )

    async def __aenter__(self) -> "ProxmoxClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def owned_vmids(self) -> frozenset[int]:
        """VMIDs created by this client instance and eligible for cleanup."""
        return frozenset(self._owned_vmids)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProxmoxError(
                f"Proxmox API {method} {path} returned HTTP {exc.response.status_code}"
            ) from None
        except httpx.TimeoutException:
            raise ProxmoxError(f"Proxmox API {method} {path} request timed out") from None
        except httpx.ConnectError:
            raise ProxmoxError(f"Proxmox API {method} {path} connection failed") from None
        except httpx.HTTPError as exc:
            raise ProxmoxError(
                f"Proxmox API {method} {path} transport failed ({type(exc).__name__})"
            ) from None
        except ValueError:
            raise ProxmoxError(f"Proxmox API {method} {path} returned invalid JSON") from None
        if not isinstance(payload, dict) or "data" not in payload:
            raise ProxmoxError("Proxmox API returned an invalid response")
        return payload["data"]

    async def verify_template(self) -> None:
        data = await self._request("GET", f"/nodes/{quote(self.config.node)}/qemu/{self.config.template_vmid}/config")
        if not isinstance(data, dict) or str(data.get("template", "0")) not in ("1", "true", "True"):
            raise ProxmoxError("Configured source VM is not a template")

    async def _next_vmid(self) -> int:
        value = await self._request("GET", "/cluster/nextid")
        try:
            vmid = int(value)
        except (TypeError, ValueError):
            raise ProxmoxError("Proxmox API returned an invalid VMID") from None
        if vmid <= 100 or vmid == self.config.template_vmid:
            raise ProxmoxError("Proxmox API returned a protected VMID")
        return vmid

    async def wait_task(self, upid: str, *, timeout: float | None = None) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + (timeout or self.config.task_timeout)
        path = f"/nodes/{quote(self.config.node)}/tasks/{quote(upid, safe='')}/status"
        while True:
            data = await self._request("GET", path)
            if not isinstance(data, dict):
                raise ProxmoxError("Proxmox returned invalid task status")
            if data.get("status") == "stopped":
                if data.get("exitstatus") != "OK":
                    raise ProxmoxError("Proxmox task did not complete successfully")
                return data
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProxmoxError("Timed out waiting for Proxmox task")
            await asyncio.sleep(min(self.config.poll_interval, remaining))

    async def task_status(self, upid: str) -> dict[str, Any]:
        data = await self._request("GET", f"/nodes/{quote(self.config.node)}/tasks/{quote(upid, safe='')}/status")
        if not isinstance(data, dict):
            raise ProxmoxError("Proxmox returned invalid task status")
        return data

    async def vm_status(self, vmid: int) -> dict[str, Any]:
        data = await self._request("GET", f"/nodes/{quote(self.config.node)}/qemu/{int(vmid)}/status/current")
        if not isinstance(data, dict):
            raise ProxmoxError("Proxmox returned invalid VM status")
        return data

    async def provision(self, run_id: str, *, cores: int = 2, memory_mb: int = 2048) -> int:
        if not run_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", run_id):
            raise ValueError("run_id must be a safe non-empty identifier")
        if not isinstance(cores, int) or isinstance(cores, bool) or not 1 <= cores <= 64:
            raise ValueError("cores must be between 1 and 64")
        if not isinstance(memory_mb, int) or isinstance(memory_mb, bool) or not 256 <= memory_mb <= 262144:
            raise ValueError("memory_mb must be between 256 and 262144")
        await self.verify_template()
        vmid = await self._next_vmid()
        name = f"mission-{run_id}-{vmid}"
        data = await self._request("POST", f"/nodes/{quote(self.config.node)}/qemu/{self.config.template_vmid}/clone",
                                   data={"newid": vmid, "name": name, "full": 1, "storage": self.config.storage})
        if not isinstance(data, str) or not data:
            raise ProxmoxError("Proxmox clone response did not contain a task UPID")
        # Claim ownership as soon as clone is accepted so failures can still be cleaned up.
        self._owned_vmids.add(vmid)
        await self.wait_task(data)
        await self._request("PUT", f"/nodes/{quote(self.config.node)}/qemu/{vmid}/config", data={
            "name": name, "cores": cores, "memory": memory_mb, "ipconfig0": "ip=dhcp",
            "net0": f"virtio,bridge={self.config.bridge}", "agent": 1,
        })
        started = await self._request("POST", f"/nodes/{quote(self.config.node)}/qemu/{vmid}/status/start")
        if isinstance(started, str) and started:
            await self.wait_task(started)
        return vmid

    async def cleanup(self, vmid: int) -> None:
        if not isinstance(vmid, int) or isinstance(vmid, bool) or vmid in (100, self.config.template_vmid) or vmid not in self._owned_vmids:
            raise ValueError("Refusing cleanup of VM not created by this adapter")
        try:
            status = await self.vm_status(vmid)
            if status.get("status") == "running":
                try:
                    task = await self._request("POST", f"/nodes/{quote(self.config.node)}/qemu/{vmid}/status/shutdown",
                                               data={"timeout": int(self.config.shutdown_timeout)})
                    if isinstance(task, str) and task:
                        await self.wait_task(task, timeout=self.config.shutdown_timeout)
                except ProxmoxError:
                    task = await self._request("POST", f"/nodes/{quote(self.config.node)}/qemu/{vmid}/status/stop")
                    if isinstance(task, str) and task:
                        await self.wait_task(task)
            deletion = await self._request("DELETE", f"/nodes/{quote(self.config.node)}/qemu/{vmid}", params={"purge": 1, "destroy-unreferenced-disks": 1})
            if isinstance(deletion, str) and deletion:
                await self.wait_task(deletion)
        except Exception:
            raise
        else:
            self._owned_vmids.remove(vmid)
