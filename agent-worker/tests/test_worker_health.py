from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SPEC = importlib.util.spec_from_file_location(
    "worker_under_test", Path(__file__).parents[1] / "worker.py"
)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class WorkerHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_answers_when_legacy_code_is_disabled(self) -> None:
        message = SimpleNamespace(
            reply="_INBOX.test",
            data=json.dumps({"worker_id": "worker-03"}).encode(),
        )
        publish = AsyncMock()
        with (
            patch.object(worker, "ALLOW_GENERATED_CODE", False),
            patch.object(worker, "WORKER_ID", "worker-03"),
            patch.object(worker, "WORKER_ROLE", "coder"),
        ):
            await worker.respond_to_health(message, publish)
        publish.assert_awaited_once_with(
            "_INBOX.test",
            json.dumps(
                {"worker_id": "worker-03", "worker_role": "coder"}
            ).encode("utf-8"),
        )

    async def test_targeted_probe_does_not_claim_another_worker(self) -> None:
        message = SimpleNamespace(
            reply="_INBOX.test",
            data=json.dumps({"worker_id": "worker-02"}).encode(),
        )
        publish = AsyncMock()
        with patch.object(worker, "WORKER_ID", "worker-03"):
            await worker.respond_to_health(message, publish)
        publish.assert_not_awaited()

    async def test_malformed_probes_are_ignored(self) -> None:
        for data in (b"not-json", b"[]"):
            with self.subTest(data=data):
                message = SimpleNamespace(reply="_INBOX.test", data=data)
                publish = AsyncMock()
                await worker.respond_to_health(message, publish)
                publish.assert_not_awaited()

    async def test_heartbeat_matches_manager_worker_contract(self) -> None:
        presence = {"status": "busy", "run_id": "run-1"}
        with (
            patch.object(worker, "_presence", presence),
            patch.object(worker, "WORKER_ID", "worker-03"),
            patch.object(worker, "WORKER_ROLE", "coder"),
            patch.object(worker, "ALLOW_GENERATED_CODE", False),
            patch.object(worker.socket, "gethostname", return_value="donatello"),
        ):
            event = worker.heartbeat_payload()

        self.assertEqual(event["event"], "worker.heartbeat")
        self.assertTrue(event["event_id"].startswith("heartbeat:worker-03:"))
        self.assertEqual(event["worker_id"], "worker-03")
        self.assertEqual(event["worker_role"], "coder")
        self.assertEqual(event["hostname"], "donatello")
        self.assertEqual(event["status"], "busy")
        self.assertEqual(event["current_run_id"], "run-1")
        self.assertEqual(event["capabilities"], [])
        self.assertNotIn("secret-output", json.dumps(event))


if __name__ == "__main__":
    unittest.main()
