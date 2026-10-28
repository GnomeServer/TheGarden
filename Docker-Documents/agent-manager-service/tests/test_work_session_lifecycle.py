from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Response

from agent_manager.models import ActivityRecord, WorkSessionRecord
from agent_manager.dashboard import activity_out
from agent_manager.tracking import (
    WorkSessionDecision,
    WorkSessionEdit,
    WorkSessionNotes,
    WorkSessionStart,
    decide_work_session,
    discard_work_session,
    edit_work_session,
    start_work_session,
    stop_work_session,
    submit_work_session,
    undo_dashboard_action,
    work_session_snapshot,
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
        created_at=datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc),
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

    async def test_owner_can_discard_rejected_timer(self) -> None:
        record = make_record(status="rejected")
        record.ended_at = record.started_at + timedelta(seconds=30)
        record.duration_seconds = 30
        session = FakeSession(record)

        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            result = await discard_work_session("timer-1", actor=make_actor(), session=session)

        self.assertEqual(result.status_code, 204)
        self.assertEqual(record.status, "deleted")
        self.assertEqual(record.duration_seconds, 30)
        [event] = activity_events(session)
        self.assertEqual(event.payload["previous_status"], "rejected")

    async def test_discard_rejects_submitted_or_approved_time(self) -> None:
        for status in ("submitted", "approved"):
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

    async def test_member_cannot_review_time_entries(self) -> None:
        record = make_record(status="submitted", user_id="entry-owner")
        session = FakeSession(record)
        with self.assertRaises(HTTPException) as caught:
            await decide_work_session(
                "timer-1",
                WorkSessionDecision(approved=False),
                actor=make_actor(user_id="reviewer", role="member"),
                session=session,
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(record.status, "submitted")
        self.assertEqual(session.commits, 0)
        own_record = make_record(status="submitted", user_id="owner")
        own_session = FakeSession(own_record)
        with self.assertRaises(HTTPException) as caught:
            await decide_work_session(
                "timer-1",
                WorkSessionDecision(approved=True),
                actor=make_actor(user_id="owner", role="member"),
                session=own_session,
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(own_record.status, "submitted")
        self.assertEqual(own_session.commits, 0)

    async def test_viewer_cannot_mutate_time_entries(self) -> None:
        viewer = make_actor(user_id="viewer", role="viewer")
        actions = (
            lambda session: start_work_session(WorkSessionStart(), actor=viewer, session=session),
            lambda session: stop_work_session(
                "timer-1", WorkSessionNotes(), actor=viewer, session=session
            ),
            lambda session: submit_work_session(
                "timer-1", WorkSessionNotes(), actor=viewer, session=session
            ),
            lambda session: discard_work_session("timer-1", actor=viewer, session=session),
        )
        for action in actions:
            with self.subTest(action=action):
                session = FakeSession(make_record())
                with self.assertRaises(HTTPException) as caught:
                    await action(session)
                self.assertEqual(caught.exception.status_code, 403)
                self.assertEqual(session.commits, 0)

    def test_timer_activity_exposes_only_bounded_metadata_and_no_notes(self) -> None:
        event = SimpleNamespace(
            id="event-id",
            source="time",
            event_type="time.discarded",
            actor_user_id="owner",
            payload={
                "work_session_id": "timer-1",
                "status": "deleted",
                "duration_seconds": 95,
                "previous_status": "running",
                "notes": "private timer note",
            },
            occurred_at=datetime.now(timezone.utc),
        )
        output = activity_out(event)
        self.assertEqual(output["event_type"], "time.discarded")
        self.assertEqual(output["payload"], {
            "session_id": "timer-1",
            "status": "deleted",
            "duration_seconds": 95,
            "previous_status": "running",
        })
        self.assertNotIn("private timer note", repr(output))


class UndoSession(FakeSession):
    def __init__(self, record: SimpleNamespace, event: SimpleNamespace) -> None:
        super().__init__(record)
        self.event = event

    async def get(self, model, record_id: str, **kwargs):
        if model is ActivityRecord:
            return self.event if record_id == self.event.id else None
        return await super().get(model, record_id, **kwargs)

    async def scalar(self, _statement):
        return None

    async def flush(self) -> None:
        return None


class UndoWorkSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_undo_stopped_timer_resumes_original_timer_state(self) -> None:
        started_at = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        ended_at = started_at + timedelta(minutes=2)
        before_record = make_record(started_at=started_at)
        after_record = make_record(status="stopped", started_at=started_at)
        after_record.ended_at = ended_at
        after_record.duration_seconds = 120
        record = make_record(status="stopped", started_at=started_at)
        record.ended_at = ended_at
        record.duration_seconds = 120
        event = SimpleNamespace(
            id="stop-event",
            actor_user_id="owner",
            task_id="task-1",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "undo": {
                    "kind": "work_session",
                    "before": work_session_snapshot(before_record),
                    "after": work_session_snapshot(after_record),
                }
            },
        )
        session = UndoSession(record, event)

        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            result = await undo_dashboard_action("stop-event", make_actor(), session)

        self.assertEqual(result, {"undone": True})
        self.assertEqual(record.status, "running")
        self.assertIsNone(record.ended_at)
        self.assertIsNone(record.duration_seconds)
        self.assertEqual(session.commits, 1)

    async def test_undo_timer_start_discards_the_new_running_timer(self) -> None:
        record = make_record(started_at=datetime.now(timezone.utc))
        event = SimpleNamespace(
            id="start-event",
            actor_user_id="owner",
            task_id="task-1",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "undo": {
                    "kind": "work_session",
                    "before": None,
                    "after": work_session_snapshot(record),
                }
            },
        )
        session = UndoSession(record, event)

        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            result = await undo_dashboard_action("start-event", make_actor(), session)

        self.assertEqual(result, {"undone": True})
        self.assertEqual(record.status, "deleted")
        self.assertEqual(record.duration_seconds, 0)
        self.assertEqual(session.commits, 1)

    async def test_undo_refuses_to_overwrite_a_later_time_change(self) -> None:
        started_at = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        ended_at = started_at + timedelta(minutes=2)
        before_record = make_record(started_at=started_at)
        after_record = make_record(status="stopped", started_at=started_at)
        after_record.ended_at = ended_at
        after_record.duration_seconds = 120
        record = make_record(status="submitted", started_at=started_at)
        record.ended_at = ended_at
        record.duration_seconds = 120
        event = SimpleNamespace(
            id="stop-event",
            actor_user_id="owner",
            task_id="task-1",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "undo": {
                    "kind": "work_session",
                    "before": work_session_snapshot(before_record),
                    "after": work_session_snapshot(after_record),
                }
            },
        )
        session = UndoSession(record, event)

        with self.assertRaises(HTTPException) as caught:
            await undo_dashboard_action("stop-event", make_actor(), session)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(record.status, "submitted")
        self.assertEqual(session.commits, 0)


class WorkSessionEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_can_stop_a_forgotten_timer_with_an_end_time(self) -> None:
        started_at = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        ended_at = started_at + timedelta(hours=2)
        now = started_at + timedelta(hours=5)
        record = make_record(started_at=started_at)
        session = FakeSession(record)

        with (
            patch("agent_manager.tracking.utcnow", return_value=now),
            patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock),
        ):
            result = await edit_work_session(
                "timer-1",
                WorkSessionEdit(ended_at=ended_at),
                actor=make_actor(),
                session=session,
            )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(record.status, "stopped")
        self.assertEqual(record.ended_at, ended_at)
        self.assertEqual(record.duration_seconds, 7200)
        self.assertEqual(activity_events(session)[0].event_type, "time.edited")

    async def test_owner_can_correct_stopped_times(self) -> None:
        started_at = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        record = make_record(status="stopped", started_at=started_at)
        record.ended_at = started_at + timedelta(hours=8)
        record.duration_seconds = 28800
        session = FakeSession(record)
        new_end = started_at + timedelta(hours=1)

        with (
            patch("agent_manager.tracking.utcnow", return_value=started_at + timedelta(hours=9)),
            patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock),
        ):
            result = await edit_work_session(
                "timer-1",
                WorkSessionEdit(started_at=started_at, ended_at=new_end),
                actor=make_actor(),
                session=session,
            )

        self.assertEqual(result["duration_seconds"], 3600)
        self.assertEqual(record.duration_seconds, 3600)
        self.assertEqual(record.status, "stopped")

    async def test_submitted_and_approved_entries_cannot_be_edited(self) -> None:
        for status in ("submitted", "approved"):
            with self.subTest(status=status):
                record = make_record(status=status)
                session = FakeSession(record)
                with self.assertRaises(HTTPException) as raised:
                    await edit_work_session(
                        "timer-1",
                        WorkSessionEdit(ended_at=datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)),
                        actor=make_actor(),
                        session=session,
                    )
                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(session.commits, 0)

    async def test_owner_cannot_edit_another_persons_timer(self) -> None:
        record = make_record(user_id="owner")
        session = FakeSession(record)
        with self.assertRaises(HTTPException) as raised:
            await edit_work_session(
                "timer-1",
                WorkSessionEdit(ended_at=datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc)),
                actor=make_actor(user_id="other"),
                session=session,
            )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(record.status, "running")
        self.assertEqual(session.commits, 0)


if __name__ == "__main__":
    unittest.main()
