from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Response

from agent_manager.models import ActivityRecord, WorkSessionRecord
from agent_manager.tracking import (
    WorkSessionDecision,
    WorkSessionNotes,
    decide_work_session,
    discard_work_session,
    stop_work_session,
    submit_work_session,
)


class FakeSession:
    def __init__(self, record: SimpleNamespace) -> None:
        self.record = record
        self.added: list[object] = []
        self.commits = 0

    async def get(self, model, record_id: str, **_kwargs):
        if model is not WorkSessionRecord:
            raise AssertionError(f"unexpected model lookup: {model}")
        return self.record if self.record.id == record_id else None

    def add(self, record: object) -> None:
        self.added.append(record)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _record: object) -> None:
        return None


def make_actor(*, user_id: str = "owner", role: str = "member") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        role=role,
        display_name=user_id.title(),
        forgejo_login=user_id,
    )


def make_record(
    *,
    status: str = "running",
    user_id: str = "owner",
    source: str = "timer",
    started_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="timer-1",
        user_id=user_id,
        task_id="task-1",
        started_at=started_at or datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc),
        ended_at=None,
        duration_seconds=None,
        source=source,
        status=status,
        notes="private notes",
        approved_by=None,
        approved_at=None,
    )


def activity_events(session: FakeSession) -> list[ActivityRecord]:
    return [record for record in session.added if isinstance(record, ActivityRecord)]


class WorkSessionDiscardTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_can_discard_running_timer_and_audit_is_retained_idempotently(self) -> None:
        started_at = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        deleted_at = started_at + timedelta(seconds=95)
        record = make_record(started_at=started_at)
        session = FakeSession(record)

        with (
            patch("agent_manager.tracking.utcnow", return_value=deleted_at),
            patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock) as publish,
        ):
            result = await discard_work_session("timer-1", actor=make_actor(), session=session)
            again = await discard_work_session("timer-1", actor=make_actor(), session=session)

        self.assertIsInstance(result, Response)
        self.assertEqual(result.status_code, 204)
        self.assertEqual(again.status_code, 204)
        self.assertEqual(record.status, "deleted")
        self.assertEqual(record.ended_at, deleted_at)
        self.assertEqual(record.duration_seconds, 95)
        self.assertEqual(session.commits, 1)

        [event] = activity_events(session)
        self.assertEqual(event.event_type, "time.discarded")
        self.assertEqual(event.actor_user_id, "owner")
        self.assertEqual(event.task_id, "task-1")
        self.assertEqual(event.payload["previous_status"], "running")
        self.assertEqual(event.payload["duration_seconds"], 95)
        self.assertNotIn("notes", event.payload)
        self.assertEqual([call.args[0] for call in publish.await_args_list], ["time", "activity"])

    async def test_owner_can_discard_stopped_timer_without_changing_its_duration(self) -> None:
        record = make_record(status="stopped")
        record.ended_at = record.started_at + timedelta(seconds=45)
        record.duration_seconds = 45
        session = FakeSession(record)

        with (
            patch("agent_manager.tracking.utcnow", return_value=record.ended_at + timedelta(seconds=5)),
            patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock),
        ):
            result = await discard_work_session("timer-1", actor=make_actor(), session=session)

        self.assertEqual(result.status_code, 204)
        self.assertEqual(record.status, "deleted")
        self.assertEqual(record.duration_seconds, 45)
        [event] = activity_events(session)
        self.assertEqual(event.payload["previous_status"], "stopped")

    async def test_discard_rejects_reviewed_or_submitted_time(self) -> None:
        for status in ("submitted", "rejected", "approved"):
            with self.subTest(status=status):
                record = make_record(status=status)
                session = FakeSession(record)
                with self.assertRaises(HTTPException) as raised:
                    await discard_work_session("timer-1", actor=make_actor(), session=session)
                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(record.status, status)
                self.assertEqual(session.commits, 0)
                self.assertEqual(activity_events(session), [])

    async def test_manager_cannot_discard_another_persons_timer(self) -> None:
        record = make_record(user_id="owner")
        session = FakeSession(record)

        with self.assertRaises(HTTPException) as raised:
            await discard_work_session(
                "timer-1", actor=make_actor(user_id="manager", role="manager"), session=session
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(record.status, "running")
        self.assertEqual(session.commits, 0)
        self.assertEqual(activity_events(session), [])


class WorkSessionActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_submit_and_approval_publish_activity_transitions(self) -> None:
        record = make_record()
        session = FakeSession(record)
        owner = make_actor()
        manager = make_actor(user_id="manager", role="manager")
        transition_times = [
            datetime(2026, 10, 13, 12, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 13, 12, 2, tzinfo=timezone.utc),
            datetime(2026, 10, 13, 12, 3, tzinfo=timezone.utc),
            datetime(2026, 10, 13, 12, 3, tzinfo=timezone.utc),
        ]

        with (
            patch("agent_manager.tracking.utcnow", side_effect=transition_times),
            patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock) as publish,
        ):
            stopped = await stop_work_session(
                "timer-1", WorkSessionNotes(), actor=owner, session=session
            )
            submitted = await submit_work_session(
                "timer-1", WorkSessionNotes(), actor=owner, session=session
            )
            approved = await decide_work_session(
                "timer-1", WorkSessionDecision(approved=True), actor=manager, session=session
            )

        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(
            [event.event_type for event in activity_events(session)],
            ["time.stopped", "time.submitted", "time.approved"],
        )
        self.assertEqual(
            [call.args[0] for call in publish.await_args_list],
            ["time", "activity"] * 3,
        )

    async def test_rejection_is_recorded_in_activity(self) -> None:
        record = make_record(status="submitted")
        session = FakeSession(record)

        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            result = await decide_work_session(
                "timer-1",
                WorkSessionDecision(approved=False, reason="Correct the task link"),
                actor=make_actor(user_id="manager", role="manager"),
                session=session,
            )

        self.assertEqual(result["status"], "rejected")
        [event] = activity_events(session)
        self.assertEqual(event.event_type, "time.rejected")
        self.assertEqual(event.actor_user_id, "manager")
        self.assertEqual(event.summary, "Manager rejected a work-time entry")


if __name__ == "__main__":
    unittest.main()
