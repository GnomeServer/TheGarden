from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from agent_manager.dashboard import (
    aggregate_worker_execution,
    dashboard_summary,
    dashboard_worker_execution,
)


class WorkerExecutionAggregationTests(unittest.TestCase):
    def test_completed_and_running_runs_are_split_at_utc_day_boundaries(self) -> None:
        day_one = date(2026, 10, 20)
        day_two = date(2026, 10, 21)
        now = datetime(2026, 10, 21, 0, 10, tzinfo=timezone.utc)
        rows = [
            (
                "worker-03",
                "completed",
                datetime(2026, 10, 20, 23, 50, tzinfo=timezone.utc),
                now,
            ),
            (
                "worker-03",
                "running",
                datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc),
                None,
            ),
            ("worker-03", "queued", None, None),
            (None, "running", now, None),
        ]

        result = aggregate_worker_execution(rows, day_one, day_two, now=now)

        self.assertEqual(
            result,
            [
                {
                    "worker_id": "worker-03",
                    "day": "2026-10-21",
                    "duration_seconds": 1200,
                    "run_count": 2,
                    "active_runs": 1,
                },
                {
                    "worker_id": "worker-03",
                    "day": "2026-10-20",
                    "duration_seconds": 600,
                    "run_count": 1,
                    "active_runs": 0,
                },
            ],
        )

    def test_range_is_limited_to_thirty_one_utc_days(self) -> None:
        with self.assertRaises(ValueError):
            aggregate_worker_execution(
                [], date(2026, 10, 1), date(2026, 11, 2)
            )


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class WorkerExecutionEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_returns_worker_duration_summary(self) -> None:
        day = date(2026, 10, 21)
        now = datetime(2026, 10, 21, 12, 10, tzinfo=timezone.utc)
        session = SimpleNamespace(execute=AsyncMock(return_value=Result([
            (
                "worker-03",
                "completed",
                datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 21, 12, 5, tzinfo=timezone.utc),
            ),
            (
                "worker-03",
                "running",
                datetime(2026, 10, 21, 12, 8, tzinfo=timezone.utc),
                None,
            ),
        ])))

        with patch("agent_manager.dashboard.utcnow", return_value=now):
            result = await dashboard_worker_execution(
                day, day, SimpleNamespace(), session
            )

        self.assertEqual(result[0]["worker_id"], "worker-03")
        self.assertEqual(result[0]["duration_seconds"], 420)
        self.assertEqual(result[0]["run_count"], 2)
        self.assertEqual(result[0]["active_runs"], 1)
        session.execute.assert_awaited_once()

    async def test_summary_counts_worker_run_time_not_access_activity(self) -> None:
        now = datetime(2026, 10, 21, 12, 10, tzinfo=timezone.utc)
        session = SimpleNamespace(
            execute=AsyncMock(side_effect=[
                Result([]),
                Result([("completed", 1)]),
                Result([(
                    "worker-03",
                    "completed",
                    datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
                    datetime(2026, 10, 21, 12, 5, tzinfo=timezone.utc),
                )]),
            ]),
            scalar=AsyncMock(side_effect=[1, 0]),
        )

        with patch("agent_manager.dashboard.utcnow", return_value=now):
            summary = await dashboard_summary(SimpleNamespace(), session)

        self.assertEqual(summary["worker_execution_seconds_today"], 300)
        self.assertEqual(summary["worker_runs_today"], 1)
        self.assertNotIn("observed_seconds_today", summary)

    async def test_endpoint_rejects_reversed_date_range(self) -> None:
        session = SimpleNamespace(execute=AsyncMock())

        with self.assertRaises(HTTPException) as caught:
            await dashboard_worker_execution(
                date(2026, 10, 22), date(2026, 10, 21), SimpleNamespace(), session
            )

        self.assertEqual(caught.exception.status_code, 422)
        session.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
