from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
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



class DockerModelHostMappingTests(unittest.TestCase):
    def test_maps_caddy_magicdns_for_bridge_containers(self) -> None:
        self.assertEqual(
            worker.docker_model_host_args(
                "https://infra-lab-services.tail494f6d.ts.net/litellm/v1"
            ),
            [
                "--add-host",
                "infra-lab-services.tail494f6d.ts.net:100.94.49.45",
            ],
        )

    def test_does_not_map_other_model_hosts(self) -> None:
        self.assertEqual(
            worker.docker_model_host_args("https://gateway.example/v1"),
            [],
        )

class CapturingJetStream:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, dict | None]] = []

    async def publish(
        self, subject: str, data: bytes, headers: dict | None = None
    ) -> None:
        self.events.append((subject, json.loads(data.decode("utf-8")), headers))


class WorkerExecutionNamingTests(unittest.IsolatedAsyncioTestCase):
    async def run_worker(self, workspace: Path, metadata: dict) -> dict:
        event = {
            "run_id": "run-42",
            "goal": "Run a deterministic test script",
            "metadata": {
                "operation": "create-python-script",
                "worker_role": "coder",
                "script": "print('named script ran')",
                **metadata,
            },
        }
        message = SimpleNamespace(
            data=json.dumps(event).encode("utf-8"),
            ack=AsyncMock(),
            nak=AsyncMock(),
        )
        js = CapturingJetStream()
        session = {
            "session_id": "session-42",
            "key": "model-session-key",
            "key_id": "key-42",
            "key_alias": "worker-test",
            "expires_at": "2030-01-01T00:00:00Z",
            "model_base_url": "https://models.example/v1",
            "model": "luna",
        }
        session_request = AsyncMock(return_value=session)
        revoke_request = AsyncMock(return_value=True)
        with (
            patch.object(worker, "WORKSPACE", workspace),
            patch.object(worker, "WORKER_ROLE", "coder"),
            patch.object(worker, "ALLOW_GENERATED_CODE", True),
            patch.object(worker, "request_keybroker_session", session_request),
            patch.object(worker, "revoke_keybroker_session", revoke_request),
        ):
            await worker.process_message(js, message)
        message.ack.assert_awaited_once()
        session_request.assert_awaited_once_with(
            "run-42", "create-python-script"
        )
        revoke_request.assert_awaited_once_with("session-42")
        started_events = [
            payload for subject, payload, _ in js.events
            if subject == "agent.runs.status"
        ]
        self.assertEqual(len(started_events), 1)
        self.assertEqual(started_events[0]["run_id"], "run-42")
        self.assertEqual(started_events[0]["status"], "running")
        completion = next(
            payload
            for subject, payload, _ in js.events
            if subject == "agent.runs.completed"
        )
        self.assertNotIn(session["key"], json.dumps(completion))
        return completion

    async def test_project_directory_replaces_run_id_and_script_executes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = await self.run_worker(
                workspace,
                {
                    "project_name": "Quarterly Reports",
                    "script_name": "audit_report",
                },
            )
            script = Path(result["script"])
            self.assertEqual(
                script.relative_to(workspace),
                Path("quarterly-reports/audit_report.py"),
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["stdout"], "named script ran\n")
            self.assertTrue(script.is_file())

    async def test_duplicate_script_names_get_a_suffix_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            project = {
                "project_name": "Quarterly Reports",
                "script_name": "audit_report.py",
            }
            first = await self.run_worker(workspace, project)
            second = await self.run_worker(
                workspace,
                {**project, "script": "print('second script ran')"},
            )
            self.assertEqual(
                Path(first["script"]),
                workspace / "quarterly-reports" / "audit_report.py",
            )
            self.assertEqual(
                Path(second["script"]),
                workspace / "quarterly-reports" / "audit_report_2.py",
            )
            self.assertEqual(
                Path(first["script"]).read_text(),
                "print('named script ran')\n",
            )
            self.assertEqual(second["stdout"], "second script ran\n")

    async def test_omitted_names_preserve_existing_workspace_and_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = await self.run_worker(workspace, {})
            self.assertEqual(
                Path(result["script"]),
                workspace / "run-42" / "generated_agent.py",
            )
            self.assertEqual(result["status"], "completed")

    async def test_script_name_cannot_escape_the_run_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = await self.run_worker(workspace, {"script_name": "../escape.py"})
            self.assertEqual(result["status"], "failed")
            self.assertIn("not a path", result["error"])
            self.assertIsNone(result["script"])
            self.assertFalse((workspace.parent / "escape.py").exists())


class WorkerCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_nats_cancel_event_stops_matching_active_container(self) -> None:
        run_id = "run-42"
        cancellation = asyncio.Event()
        message = SimpleNamespace(
            data=json.dumps({"run_id": run_id}).encode("utf-8")
        )
        with (
            patch.object(
                worker,
                "ACTIVE_MISSIONS",
                {run_id: ("omp-mission-test", cancellation)},
            ),
            patch.object(worker, "stop_omp_container") as stop_container,
        ):
            await worker.respond_to_cancellation(message)

        self.assertTrue(cancellation.is_set())
        stop_container.assert_called_once_with("omp-mission-test")


if __name__ == "__main__":
    unittest.main()
