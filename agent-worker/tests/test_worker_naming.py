from __future__ import annotations

import importlib.util
import sys
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).parents[1]))

SPEC = importlib.util.spec_from_file_location(
    "worker_under_test", Path(__file__).parents[1] / "worker.py"
)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class CapturingJetStream:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.events.append((subject, json.loads(data.decode("utf-8"))))


class WorkerNamingTests(unittest.IsolatedAsyncioTestCase):
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
        )
        js = CapturingJetStream()
        session = {
            "session_id": "test-session",
            "key": "opaque-session-key",
            "key_id": "test-key-id",
            "key_alias": "test-alias",
            "expires_at": "2030-01-01T00:00:00Z",
            "model_base_url": "https://models.example/v1",
            "model": "luna",
        }
        with (
            patch.object(worker, "WORKSPACE", workspace),
            patch.object(worker, "ALLOW_GENERATED_CODE", True),
            patch.object(worker, "request_keybroker_session", AsyncMock(return_value=session)),
            patch.object(worker, "revoke_keybroker_session", AsyncMock(return_value=True)),
        ):
            await worker.process_message(js, message)
        message.ack.assert_awaited_once()
        return next(
            payload
            for subject, payload in js.events
            if subject == "agent.runs.completed"
        )

    async def test_expired_mission_is_failed_without_starting_worker_execution(self) -> None:
        event = {
            "run_id": "expired-run",
            "expires_at": "2020-01-01T00:00:00+00:00",
            "metadata": {"operation": "mission", "worker_role": "coder"},
        }
        message = SimpleNamespace(
            data=json.dumps(event).encode("utf-8"),
            ack=AsyncMock(),
        )
        js = CapturingJetStream()

        await worker.process_message(js, message)

        self.assertEqual([subject for subject, _ in js.events], ["agent.runs.completed"])
        completion = js.events[0][1]
        self.assertEqual(completion["status"], "failed")
        self.assertIn("expired", completion["summary"])
        self.assertNotIn("started_at", completion)
        message.ack.assert_awaited_once()

    async def test_running_status_is_published_on_the_manager_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event = {
                "run_id": "run-42",
                "goal": "Run a deterministic test script",
                "metadata": {
                    "operation": "create-python-script",
                    "worker_role": "coder",
                    "script": "print('named script ran')",
                },
            }
            message = SimpleNamespace(data=json.dumps(event).encode("utf-8"), ack=AsyncMock())
            js = CapturingJetStream()
            session = {
                "session_id": "test-session",
                "key": "opaque-session-key",
                "key_id": "test-key-id",
                "key_alias": "test-alias",
                "expires_at": "2030-01-01T00:00:00Z",
                "model_base_url": "https://models.example/v1",
                "model": "luna",
            }
            with (
                patch.object(worker, "WORKSPACE", Path(directory)),
                patch.object(worker, "ALLOW_GENERATED_CODE", True),
                patch.object(worker, "request_keybroker_session", AsyncMock(return_value=session)),
                patch.object(worker, "revoke_keybroker_session", AsyncMock(return_value=True)),
            ):
                await worker.process_message(js, message)
        subjects = [subject for subject, _payload in js.events]
        self.assertIn("agent.runs.status", subjects)
        status = next(payload for subject, payload in js.events if subject == "agent.runs.status")
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["run_id"], "run-42")
        self.assertEqual(worker._presence["status"], "idle")

    async def test_project_and_script_names_define_saved_path(self) -> None:
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

    async def test_duplicate_script_names_do_not_overwrite(self) -> None:
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
                Path(first["script"]).read_text(encoding="utf-8"),
                "print('named script ran')\n",
            )
            self.assertEqual(second["stdout"], "second script ran\n")

    async def test_unnamed_direct_api_run_keeps_uuid_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = await self.run_worker(workspace, {})
            self.assertEqual(
                Path(result["script"]),
                workspace / "run-42" / "generated_agent.py",
            )
            self.assertEqual(result["status"], "completed")

    async def test_script_name_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = await self.run_worker(
                workspace,
                {"project_name": "Quarterly Reports", "script_name": "../escape.py"},
            )
            self.assertEqual(result["status"], "failed")
            self.assertIn("not a path", result["error"])
            self.assertIsNone(result["script"])
            self.assertFalse((workspace.parent / "escape.py").exists())


if __name__ == "__main__":
    unittest.main()
