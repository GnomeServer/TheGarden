from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from agent_manager.auth import _decode, create_session
from agent_manager.dashboard import TaskCreate, compact_forgejo_payload, forgejo_summary


class SessionContractTests(unittest.TestCase):
    def test_tampered_session_is_rejected(self) -> None:
        token, csrf = create_session("user-1")
        self.assertEqual(_decode(token)["csrf"], csrf)
        replacement = "A" if token[0] != "A" else "B"
        self.assertIsNone(_decode(replacement + token[1:]))


class TaskContractTests(unittest.TestCase):
    def test_deadline_requires_timezone(self) -> None:
        with self.assertRaises(ValidationError):
            TaskCreate(title="naive deadline", due_at=datetime(2030, 1, 2, 15, 0))

        task = TaskCreate(
            title="absolute deadline",
            due_at=datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(task.due_at.utcoffset().total_seconds(), 0)


class ForgejoContractTests(unittest.TestCase):
    def test_push_normalization_discards_free_text_and_secrets(self) -> None:
        payload = {
            "ref": "refs/heads/main",
            "authorization": "must-not-be-copied",
            "commits": [
                {"id": str(index), "message": "secret prompt", "secret": "drop"}
                for index in range(105)
            ],
        }
        compact = compact_forgejo_payload("push", payload)
        self.assertNotIn("authorization", compact)
        self.assertEqual(compact["commit_count"], 105)
        self.assertNotIn("commits", compact)
        self.assertNotIn("secret prompt", repr(compact))
        self.assertEqual(
            forgejo_summary("push", payload, "Alice", "owner/project"),
            "Alice pushed 105 commits to owner/project:main",
        )


class DashboardSanitizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_run_response_never_returns_goal_or_diagnostics(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from agent_manager.dashboard import dashboard_runs

        record = SimpleNamespace(
            id="run-1",
            run_metadata={
                "worker_role": "coder",
                "project_name": "Quarterly report",
                "operation": "create-python-script",
                "source": "open-webui",
                "script_name": "audit_report.py",
                "goal": "private prompt",
                "result": {"source": "model", "stdout": "secret command output", "returncode": 1},
            },
            status="failed",
            worker_id="worker-01",
            error="secret command output",
            goal="private prompt",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [record])
        )
        session = SimpleNamespace(execute=AsyncMock(return_value=result))
        output = await dashboard_runs(100, SimpleNamespace(), session)
        self.assertEqual(
            set(output[0]),
            {"id", "request_summary", "worker_role", "operation", "source", "script_name",
             "ledger", "bounce", "result_summary", "chatter", "phase", "status", "worker_id",
             "result_code", "error_summary", "created_at", "started_at", "updated_at"},
        )
        self.assertNotIn("private prompt", repr(output))
        self.assertEqual(output[0]["request_summary"], "Project: Quarterly report")
        self.assertEqual(output[0]["source"], "open-webui")
        self.assertEqual(output[0]["operation"], "create-python-script")
        self.assertEqual(output[0]["script_name"], "audit_report.py")
        self.assertEqual(output[0]["phase"], "Stopped after a worker failure. Raw output is withheld.")
        self.assertNotIn("secret command output", repr(output))

    async def test_worker_admission_status_uses_core_health_not_heartbeat_presence(self) -> None:
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from nats.errors import NoRespondersError

        from agent_manager.dashboard import worker_admission_status

        worker = SimpleNamespace(id="worker-01", role="coder")
        response = SimpleNamespace(data=json.dumps({
            "worker_id": worker.id, "worker_role": worker.role,
        }).encode())
        connection = SimpleNamespace(request=AsyncMock(return_value=response))
        self.assertEqual(await worker_admission_status(connection, worker), "available")
        connection.request.assert_awaited_once()

        connection.request = AsyncMock(side_effect=NoRespondersError)
        self.assertEqual(await worker_admission_status(connection, worker), "unavailable")
        self.assertEqual(await worker_admission_status(None, worker), "unverified")

    def test_activity_dto_strips_raw_worker_diagnostics(self) -> None:
        from types import SimpleNamespace

        from agent_manager.dashboard import activity_out

        event = SimpleNamespace(
            id="event-id",
            source="worker",
            event_type="run.failed",
            actor_user_id=None,
            payload={"error": "secret command output", "stderr": "private prompt"},
            occurred_at=datetime.now(timezone.utc),
        )
        output = activity_out(event)
        self.assertNotIn("secret command output", repr(output))
        self.assertNotIn("private prompt", repr(output))
        self.assertEqual(output["summary"], "A worker failed a run")

    def test_activity_dto_never_exposes_private_undo_snapshots(self) -> None:
        from types import SimpleNamespace

        from agent_manager.dashboard import activity_out

        event = SimpleNamespace(
            id="event-id",
            source="task",
            event_type="task.updated",
            actor_user_id="member-1",
            payload={
                "changed_fields": ["status"],
                "undo": {
                    "before": {"description": "private draft text"},
                    "after": {"description": "updated private draft text"},
                },
            },
            occurred_at=datetime.now(timezone.utc),
        )
        output = activity_out(event)
        self.assertEqual(output["payload"], {"changed_fields": ["status"]})
        self.assertNotIn("private draft text", repr(output))


if __name__ == "__main__":
    unittest.main()
