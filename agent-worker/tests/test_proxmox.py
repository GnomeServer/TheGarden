from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parents[1]))
from proxmox import ProxmoxClient, ProxmoxConfig, ProxmoxError


UPID = "UPID:node:123:abc:qmclone:101:root@pam:"


def config(**kwargs):
    values = dict(base_url="https://pve.example:8006", token_id="worker@pve!ephemeral",
                  token_secret="secret-token", node="server-debian", template_vmid=101,
                  storage="local", bridge="vmbr0", poll_interval=0.001)
    values.update(kwargs)
    return ProxmoxConfig(**values)


class ProxmoxTests(unittest.IsolatedAsyncioTestCase):
    async def test_provision_clone_task_config_and_start(self):
        calls = []
        def handler(request):
            calls.append(request)
            path = request.url.path
            if path.endswith("/config") and request.method == "GET":
                data = {"template": 1}
            elif path.endswith("/cluster/nextid"):
                data = 321
            elif path.endswith("/clone"):
                self.assertEqual(dict(request.url.params), {})
                body = request.read().decode()
                self.assertIn("full=1", body)
                self.assertIn("newid=321", body)
                data = UPID
            elif path.endswith("/tasks/" + UPID.replace(":", "%3A") + "/status") or "/tasks/" in path:
                data = {"status": "stopped", "exitstatus": "OK"}
            elif path.endswith("/status/start"):
                data = ""
            elif path.endswith("/qemu/321/config") and request.method == "PUT":
                body = request.read().decode()
                for expected in ("cores=2", "memory=2048", "ipconfig0=ip%3Ddhcp", "bridge%3Dvmbr0"):
                    self.assertIn(expected, body)
                data = None
            else:
                self.fail(f"Unexpected request {request.method} {path}")
            return httpx.Response(200, json={"data": data})
        client = ProxmoxClient(config(), transport=httpx.MockTransport(handler))
        vmid = await client.provision("run-7")
        self.assertEqual(vmid, 321)
        self.assertEqual([r.method for r in calls], ["GET", "GET", "POST", "GET", "PUT", "POST"])
        await client.aclose()

    async def test_cleanup_waits_for_shutdown_then_deletes_and_purges(self):
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith("status/current"):
                data = {"status": "running"}
            elif request.url.path.endswith("status/shutdown"):
                data = UPID
            elif "/tasks/" in request.url.path:
                data = {"status": "stopped", "exitstatus": "OK"}
            elif request.method == "DELETE":
                self.assertEqual(request.url.params["purge"], "1")
                self.assertEqual(request.url.params["destroy-unreferenced-disks"], "1")
                data = UPID
            else:
                self.fail(request.url.path)
            return httpx.Response(200, json={"data": data})
        client = ProxmoxClient(config(), transport=httpx.MockTransport(handler))
        client._owned_vmids.add(321)
        await client.cleanup(321)
        self.assertTrue(any(p.endswith("status/shutdown") for p in paths))
        self.assertTrue(any(p.endswith("/qemu/321") for p in paths))
        await client.aclose()

    async def test_failed_task_and_api_errors_fail_closed(self):
        def failed(request):
            if request.url.path.endswith("/config"):
                return httpx.Response(200, json={"data": {"template": 1}})
            if request.url.path.endswith("nextid"):
                return httpx.Response(200, json={"data": 321})
            if request.url.path.endswith("clone"):
                return httpx.Response(200, json={"data": UPID})
            return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": "ERROR"}})
        client = ProxmoxClient(config(), transport=httpx.MockTransport(failed))
        with self.assertRaises(ProxmoxError):
            await client.provision("run-8")
        self.assertIn(321, client._owned_vmids)
        await client.aclose()

    async def test_protected_or_unowned_cleanup_refused_without_api_call(self):
        calls = []
        client = ProxmoxClient(config(), transport=httpx.MockTransport(lambda req: calls.append(req)))
        for vmid in (100, 101, 102):
            with self.assertRaises(ValueError):
                await client.cleanup(vmid)
        self.assertEqual(calls, [])
        await client.aclose()

    async def test_non_template_and_insecure_tls_configuration_rejected(self):
        client = ProxmoxClient(config(), transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"data": {"template": 0}})))
        with self.assertRaises(ProxmoxError):
            await client.verify_template()
        await client.aclose()
        with self.assertRaises(ValueError):
            config(base_url="http://pve.example")


    async def test_api_status_error_reports_status_without_response_body(self):
        client = ProxmoxClient(
            config(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401,
                    json={"error": "sensitive response body"},
                )
            ),
        )
        with self.assertRaisesRegex(
            ProxmoxError,
            "GET /nodes/server-debian/status returned HTTP 401",
        ) as raised:
            await client._request("GET", "/nodes/server-debian/status")
        self.assertNotIn("sensitive response body", str(raised.exception))
        await client.aclose()

if __name__ == "__main__":
    unittest.main()
