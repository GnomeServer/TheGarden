from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_manager.main import RunOut, apply_run_completion, apply_run_status
from agent_manager.models import RunRecord, WorkerRecord


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def make_run(*, status="queued", started_at=None, completed_at=None):
    return SimpleNamespace(
        id="run-1",
        goal="Write a test script",
        forgejo_repository="smoke/test",
        base_ref="main",
        model="test-model",
        status=status,
        error=None,
        run_metadata={},
        created_at=datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc),
        worker_id=None,
        started_at=started_at,
        completed_at=completed_at,
    )


def make_event_session(run, worker):
    async def get(model, _record_id):
        if model is RunRecord:
            return run
        if model is WorkerRecord:
            return worker
        raise AssertionError(f"unexpected model lookup: {model}")

    return SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )


class RunOutputTests(unittest.TestCase):
    def test_finished_run_exposes_agent_and_execution_duration(self):
        started_at = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        completed_at = started_at + timedelta(seconds=67)
        run = make_run(status="completed", started_at=started_at, completed_at=completed_at)
        run.worker_id = "worker-03"

        result = RunOut.from_record(run)

        self.assertEqual(result.worker_id, "worker-03")
        self.assertEqual(result.started_at, started_at)
        self.assertEqual(result.completed_at, completed_at)
        self.assertEqual(result.duration_seconds, 67)

    def test_queued_run_has_no_owner_or_execution_duration(self):
        result = RunOut.from_record(make_run())

        self.assertIsNone(result.worker_id)
        self.assertIsNone(result.started_at)
        self.assertIsNone(result.duration_seconds)


class RunEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_event_persists_owner_and_worker_start_time(self):
        run = make_run()
        worker = SimpleNamespace(id="worker-03", reported_status="idle", current_run_id=None)
        session = make_event_session(run, worker)
        started_at = "2026-10-13T12:00:05+00:00"

        with (
            patch("agent_manager.main.SessionLocal", return_value=SessionContext(session)),
            patch("agent_manager.main.record_activity", new_callable=AsyncMock, return_value=object()),
            patch("agent_manager.main.activity_out", return_value={}),
            patch("agent_manager.main.live_broker.publish", new_callable=AsyncMock),
        ):
            await apply_run_status(
                {
                    "run_id": run.id,
                    "worker_id": "worker-03",
                    "status": "running",
                    "sent_at": started_at,
                    "event_id": "status-1",
                }
            )

        self.assertEqual(run.status, "running")
        self.assertEqual(run.worker_id, "worker-03")
        self.assertEqual(run.started_at, datetime.fromisoformat(started_at))
        self.assertEqual(worker.reported_status, "busy")
        self.assertEqual(worker.current_run_id, run.id)

    async def test_completed_completion_persists_owner_and_terminal_time(self):
        run = make_run(status="running")
        worker = SimpleNamespace(id="worker-02", reported_status="busy", current_run_id=run.id, last_error=None)
        session = make_event_session(run, worker)
        started_at = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        completed_at = started_at + timedelta(seconds=4)

        with (
            patch("agent_manager.main.SessionLocal", return_value=SessionContext(session)),
            patch("agent_manager.main.record_activity", new_callable=AsyncMock, return_value=object()),
            patch("agent_manager.main.activity_out", return_value={}),
            patch("agent_manager.main.live_broker.publish", new_callable=AsyncMock),
            patch("agent_manager.main.RUNS_COMPLETED"),
        ):
            await apply_run_completion(
                {
                    "run_id": run.id,
                    "worker_id": "worker-02",
                    "status": "completed",
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                    "returncode": 0,
                    "event_id": "completion-2",
                }
            )

        result = RunOut.from_record(run)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.worker_id, "worker-02")
        self.assertEqual(result.duration_seconds, 4)
        self.assertIsNone(result.error)
        self.assertEqual(run.run_metadata["result"]["returncode"], 0)
        self.assertEqual(worker.reported_status, "idle")
        self.assertIsNone(worker.current_run_id)


    async def test_failed_completion_persists_timing_owner_and_error(self):
        run = make_run(status="running")
        worker = SimpleNamespace(id="worker-03", reported_status="busy", current_run_id=run.id, last_error=None)
        session = make_event_session(run, worker)
        started_at = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        completed_at = started_at + timedelta(seconds=29)

        with (
            patch("agent_manager.main.SessionLocal", return_value=SessionContext(session)),
            patch("agent_manager.main.record_activity", new_callable=AsyncMock, return_value=object()),
            patch("agent_manager.main.activity_out", return_value={}),
            patch("agent_manager.main.live_broker.publish", new_callable=AsyncMock),
            patch("agent_manager.main.RUNS_FAILED"),
        ):
            await apply_run_completion(
                {
                    "run_id": run.id,
                    "worker_id": "worker-03",
                    "status": "failed",
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                    "error": "script exited nonzero",
                    "event_id": "completion-1",
                }
            )

        result = RunOut.from_record(run)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.worker_id, "worker-03")
        self.assertEqual(result.started_at, started_at)
        self.assertEqual(result.completed_at, completed_at)
        self.assertEqual(result.duration_seconds, 29)
        self.assertEqual(result.error, "script exited nonzero")
        self.assertEqual(worker.reported_status, "degraded")
        self.assertIsNone(worker.current_run_id)


if __name__ == "__main__":
    unittest.main()
