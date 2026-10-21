from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_manager.dashboard import run_duration_seconds
from agent_manager.main import reconcile_stale_runs
from agent_manager.models import WorkerRecord


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class CandidateRows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class StaleRunReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_queue_and_offline_run_fail_but_live_worker_is_preserved(self) -> None:
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        old = now - timedelta(minutes=31)
        queued = SimpleNamespace(
            id="queued-1", status="queued", created_at=old, started_at=None,
            completed_at=None, worker_id=None, error=None,
            run_metadata={"operation": "mission", "ledger": {}},
        )
        offline_run = SimpleNamespace(
            id="offline-1", status="running", created_at=old, started_at=old,
            completed_at=None, worker_id="worker-03", error=None,
            run_metadata={"operation": "mission", "ledger": {}, "chatter": []},
        )
        live_run = SimpleNamespace(
            id="live-1", status="running", created_at=old, started_at=old,
            completed_at=None, worker_id="worker-04", error=None,
            run_metadata={"operation": "mission", "ledger": {}, "chatter": []},
        )
        worker = SimpleNamespace(
            id="worker-03", current_run_id=offline_run.id,
            reported_status="busy", last_error=None,
        )

        async def get(model, record_id, **kwargs):
            self.assertIs(model, WorkerRecord)
            self.assertEqual(record_id, "worker-03")
            return worker

        session = SimpleNamespace(
            execute=AsyncMock(return_value=CandidateRows([queued, offline_run, live_run])),
            scalar=AsyncMock(side_effect=[None, None, "worker-04"]),
            get=AsyncMock(side_effect=get),
            commit=AsyncMock(),
        )
        activity = SimpleNamespace()

        with (
            patch("agent_manager.main.SessionLocal", return_value=SessionContext(session)),
            patch("agent_manager.main.record_activity", new_callable=AsyncMock, return_value=activity) as record_activity,
            patch("agent_manager.main.activity_out", return_value={}),
            patch("agent_manager.main.live_broker.publish", new_callable=AsyncMock) as publish,
            patch("agent_manager.main.RUNS_FAILED") as failed_counter,
        ):
            count = await reconcile_stale_runs(now=now)

        self.assertEqual(count, 2)
        self.assertEqual(queued.status, "failed")
        self.assertIsNone(queued.started_at)
        self.assertEqual(queued.completed_at, now)
        self.assertEqual(offline_run.status, "failed")
        self.assertEqual(offline_run.completed_at, now)
        self.assertEqual(
            run_duration_seconds(
                "failed",
                offline_run.started_at,
                offline_run.completed_at,
                now=now + timedelta(hours=2),
            ),
            int((now - offline_run.started_at).total_seconds()),
        )
        self.assertIsNone(worker.current_run_id)
        self.assertEqual(worker.reported_status, "degraded")
        self.assertEqual(live_run.status, "running")
        self.assertEqual(record_activity.await_count, 2)
        session.commit.assert_awaited_once()
        failed_counter.inc.assert_called_once_with(2)
        self.assertEqual(publish.await_count, 4)


if __name__ == "__main__":
    unittest.main()
