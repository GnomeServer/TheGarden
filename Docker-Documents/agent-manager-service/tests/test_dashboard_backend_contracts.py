from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from agent_manager.dashboard import (
    CANONICAL_WORKERS,
    WORKER_SPECS_BY_ID,
    TaskUpdate,
    canonical_worker_id,
    canonical_worker_out,
    list_workers,
    task_snapshot,
    update_task,
)
from agent_manager.main import apply_worker_event
from agent_manager.models import ActivityRecord, AccessEventRecord, TaskAuditRecord, WorkerRecord
from agent_manager.tracking import (
    AccessEventBatch,
    AccessEventIn,
    ActivityPulse,
    EstimateReview,
    IdentityMappingCreate,
    access_out,
    activity_pulse,
    create_identity_mapping,
    estimate_totals,
    ingest_access_events,
    review_activity_estimate,
    undo_dashboard_action,
)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalars(self):
        return self


class Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class SharedTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_can_transition_and_reassign_a_task_owned_by_another_member(self):
        now = datetime.now(timezone.utc)
        task = SimpleNamespace(
            id="task-1", title="Shared task", description="", status="todo", priority="normal",
            due_at=None, assignee_user_id="author", assignee_worker_id=None, repository=None,
            forgejo_issue_number=None, run_id=None, created_by="author", completed_at=None,
            version=1, created_at=now, updated_at=now, deleted_at=None,
        )
        assignee = SimpleNamespace(id="assignee", forgejo_login="inkii", role="member", active=True)
        session = SimpleNamespace(
            get=AsyncMock(side_effect=[task, assignee]),
            add=Mock(), flush=AsyncMock(), refresh=AsyncMock(), commit=AsyncMock(),
        )
        actor = SimpleNamespace(
            id="actor", role="member", forgejo_login="raphael", display_name="Raphael",
        )
        body = TaskUpdate(version=1, status="in_progress", assignee_user_id="assignee")
        with patch("agent_manager.dashboard.live_broker.publish", new_callable=AsyncMock):
            result = await update_task("task-1", body, actor, session)
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(result["assignee_user_id"], "assignee")
        self.assertEqual(result["version"], 2)
        self.assertEqual(task.version, 2)
        self.assertIn(TaskAuditRecord, {type(item) for item in session.add.call_args_list[0].args})
        session.commit.assert_awaited_once()


class ActivityEstimateTests(unittest.IsolatedAsyncioTestCase):
    async def test_estimates_use_foreground_pulses_not_ssh(self):
        day = date(2026, 10, 12)
        opened = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        session = SimpleNamespace(execute=AsyncMock(side_effect=[
            Result([("member-1",)]),
            Result([
                ("member-1", opened + timedelta(minutes=1)),
                ("member-1", opened + timedelta(minutes=2)),
            ]),
        ]))
        estimates = await estimate_totals(session, day, day)
        self.assertEqual(estimates[("member-1", day)], {"ssh": 0, "dashboard": 120})



class IdentityAndIngestTests(unittest.IsolatedAsyncioTestCase):

    def test_access_output_preserves_tailscale_actor_and_ssh_source_ip(self):
        occurred_at = datetime.now(timezone.utc)
        record = SimpleNamespace(
            id="access-1",
            node_id="infra-lab-services",
            user_id=None,
            identity="inkii+lab@example.com",
            identity_source="tailscale",
            remote_identity=None,
            event_metadata={"target_name": "inkii-laptop", "diff": "private policy"},
            source="tailscale",
            service="tailscale",
            event_type="login",
            occurred_at=occurred_at,
            received_at=occurred_at,
        )

        output = access_out(record)
        self.assertEqual(output["identity"], "inkii+lab@example.com")
        self.assertEqual(output["identity_source"], "tailscale")
        self.assertEqual(output["target_node"], "inkii-laptop")
        self.assertIsNone(output["remote_identity"])
        self.assertNotIn("private policy", repr(output))

        ssh_record = SimpleNamespace(
            id="access-2",
            node_id="server-debian",
            user_id=None,
            identity="df-server",
            identity_source="linux",
            remote_identity="100.98.125.127",
            event_metadata={},
            source="sshd",
            service="ssh",
            event_type="login",
            occurred_at=occurred_at,
            received_at=occurred_at,
        )
        self.assertEqual(access_out(ssh_record)["remote_identity"], "100.98.125.127")

    async def test_tailscale_login_uses_exact_actor_mapping(self):
        event = AccessEventIn(
            event_id="tailscale-login-1",
            source="tailscale",
            service="tailscale",
            event_type="login",
            occurred_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            identity="raphael@example.com",
            identity_source="tailscale",
            summary="Tailscale node login",
            metadata={"target_name": "raphael-laptop"},
        )
        batch = AccessEventBatch(node_id="infra-lab-services", events=[event])
        credential = SimpleNamespace(node_id="infra-lab-services", last_used_at=None)
        session = SimpleNamespace(
            scalar=AsyncMock(side_effect=[None, "member-raphael"]),
            add=Mock(),
            flush=AsyncMock(),
            commit=AsyncMock(),
            begin_nested=Mock(return_value=Savepoint()),
        )
        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            result = await ingest_access_events(batch, credential, session)

        access = next(
            item for item in session.add.call_args_list
            if isinstance(item.args[0], AccessEventRecord)
        ).args[0]
        self.assertEqual(result, {"accepted": 1, "duplicates": 0})
        self.assertEqual(access.user_id, "member-raphael")
        self.assertEqual(access_out(access)["target_node"], "raphael-laptop")

    async def test_shared_ssh_account_can_be_attributed_by_explicit_tailnet_ip_mapping(self):
        event = AccessEventIn(
            event_id="collector-event-tailnet-ip",
            source="sshd",
            service="ssh",
            event_type="login",
            occurred_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            identity="infra-lab-user",
            identity_source="linux",
            remote_identity="100.98.125.127",
            summary="SSH authentication accepted",
        )
        batch = AccessEventBatch(node_id="infra-lab-services", events=[event])
        credential = SimpleNamespace(node_id="infra-lab-services", last_used_at=None)
        session = SimpleNamespace(
            scalar=AsyncMock(side_effect=[None, "member-inkii"]),
            add=Mock(),
            flush=AsyncMock(),
            commit=AsyncMock(),
            begin_nested=Mock(return_value=Savepoint()),
        )
        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            result = await ingest_access_events(batch, credential, session)

        access = next(
            item for item in session.add.call_args_list
            if isinstance(item.args[0], AccessEventRecord)
        ).args[0]
        self.assertEqual(result, {"accepted": 1, "duplicates": 0})
        self.assertEqual(access.user_id, "member-inkii")
        self.assertEqual(session.scalar.await_count, 2)



class CanonicalWorkerRosterTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_heartbeat_ids_group_under_the_physical_host(self) -> None:
        self.assertEqual(
            canonical_worker_id(SimpleNamespace(id="inkii", hostname="inkii")),
            "worker-02",
        )
        self.assertEqual(
            canonical_worker_id(SimpleNamespace(id="worker-1", hostname="inkii")),
            "worker-02",
        )
        self.assertEqual(
            canonical_worker_id(SimpleNamespace(id="worker-1", hostname="raphael")),
            "worker-01",
        )
        self.assertIsNone(
            canonical_worker_id(SimpleNamespace(id="worker-1", hostname="unknown-host")),
        )
        self.assertIsNone(
            canonical_worker_id(SimpleNamespace(id="factory-test-worker", hostname="6bf83f")),
        )
        self.assertEqual(
            {worker["display_name"] for worker in CANONICAL_WORKERS},
            {"Donatello", "Raphael", "Naruto", "Inkii", "Sammy", "Michelangelo"},
        )

    async def test_canonical_worker_card_keeps_legacy_ids_as_metadata(self) -> None:
        now = datetime.now(timezone.utc)
        canonical = SimpleNamespace(
            id="worker-02",
            role="coder",
            hostname="inkii",
            last_seen_at=now,
            capabilities=["mission"],
            reported_status="idle",
            current_run_id=None,
            version="test",
        )
        legacy = SimpleNamespace(
            id="inkii",
            role="coder",
            hostname="inkii",
            last_seen_at=now - timedelta(days=2),
        )
        with patch(
            "agent_manager.dashboard.worker_admission_status",
            new=AsyncMock(return_value="available"),
        ):
            result = await canonical_worker_out(
                WORKER_SPECS_BY_ID["worker-02"], [legacy, canonical], None
            )
        self.assertEqual(result["id"], "worker-02")
        self.assertTrue(result["has_record"])
        self.assertEqual(result["display_name"], "Inkii")
        self.assertEqual(result["reported_worker_ids"], ["inkii", "worker-02"])
        self.assertEqual(result["legacy_worker_ids"], ["inkii"])
        self.assertTrue(result["assignable"])


    async def test_fresh_legacy_heartbeat_supplies_presence_without_losing_canonical_admission(self) -> None:
        now = datetime.now(timezone.utc)
        canonical = SimpleNamespace(
            id="worker-02",
            role="coder",
            hostname="inkii",
            last_seen_at=now - timedelta(days=2),
            capabilities=["mission-crew"],
            reported_status="idle",
            current_run_id=None,
            version="canonical",
        )
        legacy = SimpleNamespace(
            id="inkii",
            role="coder",
            hostname="inkii",
            last_seen_at=now,
            capabilities=["create-python-script"],
            reported_status="idle",
            current_run_id=None,
            version="legacy-current",
        )
        with patch(
            "agent_manager.dashboard.worker_admission_status",
            new=AsyncMock(return_value="available"),
        ) as admission:
            result = await canonical_worker_out(
                WORKER_SPECS_BY_ID["worker-02"], [canonical, legacy], None
            )
        self.assertEqual(result["id"], "worker-02")
        self.assertEqual(result["version"], "legacy-current")
        self.assertEqual(result["capabilities"], ["create-python-script"])
        self.assertEqual(result["presence_status"], "online")
        self.assertEqual(result["legacy_online_worker_ids"], ["inkii"])
        self.assertTrue(result["has_canonical_identity"])
        self.assertTrue(result["assignable"])
        admission.assert_awaited_once_with(None, canonical)
    async def test_worker_endpoint_returns_only_six_canonical_hosts_and_groups_old_ids(self) -> None:
        now = datetime.now(timezone.utc)
        workers = [
            SimpleNamespace(
                id="worker-02",
                role="coder",
                hostname="inkii",
                last_seen_at=now,
                capabilities=["mission"],
                reported_status="idle",
                current_run_id=None,
                version="new",
            ),
            SimpleNamespace(
                id="inkii",
                role="coder",
                hostname="inkii",
                last_seen_at=now - timedelta(days=2),
                capabilities=["create-python-script"],
                reported_status="idle",
                current_run_id=None,
                version="legacy",
            ),
            SimpleNamespace(
                id="worker-1",
                role="coder",
                hostname="inkii",
                last_seen_at=now - timedelta(days=3),
                capabilities=["python-script"],
                reported_status="offline",
                current_run_id=None,
                version="legacy-1",
            ),
            SimpleNamespace(
                id="worker-2",
                role="coder",
                hostname="inkii",
                last_seen_at=now - timedelta(days=4),
                capabilities=["python-script"],
                reported_status="offline",
                current_run_id=None,
                version="legacy-2",
            ),
            SimpleNamespace(
                id="factory-test-worker",
                role="coder",
                hostname="6bf83f057a6c",
                last_seen_at=now - timedelta(days=5),
                capabilities=["mission"],
                reported_status="idle",
                current_run_id=None,
                version="test",
            ),
        ]
        session = SimpleNamespace(execute=AsyncMock(return_value=Result(workers)))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(nats=None)))
        with patch(
            "agent_manager.dashboard.worker_admission_status",
            new=AsyncMock(return_value="available"),
        ):
            output = await list_workers(request, SimpleNamespace(), session)
        self.assertEqual(len(output), 6)
        self.assertEqual({worker["id"] for worker in output}, set(WORKER_SPECS_BY_ID))
        inkii = next(worker for worker in output if worker["id"] == "worker-02")
        self.assertEqual(inkii["display_name"], "Inkii")
        self.assertEqual(
            inkii["reported_worker_ids"],
            ["inkii", "worker-02", "worker-1", "worker-2"],
        )
        self.assertTrue(inkii["assignable"])
        self.assertNotIn("factory-test-worker", {worker["id"] for worker in output})
        sammy = next(worker for worker in output if worker["id"] == "worker-05")
        self.assertFalse(sammy["has_record"])
        self.assertFalse(sammy["assignable"])

class WorkerHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_persists_presence_without_exposing_errors(self):
        now = datetime.now(timezone.utc)
        session = SimpleNamespace(
            get=AsyncMock(return_value=None), scalar=AsyncMock(return_value=None),
            add=Mock(), commit=AsyncMock(), refresh=AsyncMock(),
        )
        payload = {
            "event": "worker.heartbeat", "event_id": "heartbeat-1",
            "worker_id": "worker-03", "worker_role": "coder", "hostname": "donatello",
            "status": "idle", "current_run_id": None, "version": "0.2.0",
            "capabilities": ["script_execution", "api-key=secret"],
            "error": "private diagnostic", "sent_at": now.isoformat(),
        }
        with patch("agent_manager.main.SessionLocal", return_value=SessionContext(session)), patch(
            "agent_manager.main.live_broker.publish", new_callable=AsyncMock,
        ) as publish:
            await apply_worker_event(payload)
        records = [call.args[0] for call in session.add.call_args_list]
        worker = next(item for item in records if isinstance(item, WorkerRecord))
        self.assertEqual(worker.id, "worker-03")
        self.assertEqual(worker.role, "coder")
        self.assertEqual(worker.last_seen_at, now)
        self.assertEqual(worker.capabilities, ["script_execution"])
        self.assertNotIn("private diagnostic", repr(publish.await_args_list[0].args[1]))
        session.commit.assert_awaited_once()


class UndoActionTests(unittest.IsolatedAsyncioTestCase):
    def task(self, *, version: int, status: str) -> SimpleNamespace:
        now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        return SimpleNamespace(
            id="task-undo-1", title="Restore me", description="details", status=status,
            priority="normal", due_at=None, assignee_user_id=None, assignee_worker_id=None,
            repository=None, forgejo_issue_number=None, run_id=None, created_by="member-1",
            completed_at=None, version=version, created_at=now, updated_at=now,
            deleted_at=None,
        )

    def event(self, before: dict, after: dict) -> SimpleNamespace:
        return SimpleNamespace(
            id="undo-event-1", actor_user_id="member-1", task_id="task-undo-1",
            source="task", payload={"undo": {"kind": "task", "before": before, "after": after}},
            occurred_at=datetime.now(timezone.utc),
        )

    async def test_task_update_undo_restores_snapshot_with_new_version(self):
        before_task = self.task(version=1, status="todo")
        after_task = self.task(version=2, status="in_progress")
        before = task_snapshot(before_task)
        after = task_snapshot(after_task)
        task = self.task(version=2, status="in_progress")
        session = SimpleNamespace(
            get=AsyncMock(side_effect=[self.event(before, after), task]),
            scalar=AsyncMock(return_value=None),
            add=Mock(),
            flush=AsyncMock(),
            commit=AsyncMock(),
        )
        actor = SimpleNamespace(id="member-1", role="member")
        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            result = await undo_dashboard_action("undo-event-1", actor, session)
        self.assertEqual(result, {"undone": True})
        self.assertEqual(task.status, "todo")
        self.assertEqual(task.version, 3)
        session.commit.assert_awaited_once()
        self.assertTrue(any(isinstance(call.args[0], TaskAuditRecord) for call in session.add.call_args_list))

    async def test_task_undo_refuses_to_overwrite_a_later_edit(self):
        before = task_snapshot(self.task(version=1, status="todo"))
        after = task_snapshot(self.task(version=2, status="in_progress"))
        task = self.task(version=3, status="done")
        session = SimpleNamespace(
            get=AsyncMock(side_effect=[self.event(before, after), task]),
            scalar=AsyncMock(return_value=None),
            add=Mock(),
            flush=AsyncMock(),
            commit=AsyncMock(),
        )
        with self.assertRaises(HTTPException) as caught:
            await undo_dashboard_action(
                "undo-event-1", SimpleNamespace(id="member-1", role="member"), session
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(task.status, "done")
        session.commit.assert_not_awaited()

    async def test_task_creation_undo_archives_only_the_unchanged_task(self):
        task = self.task(version=1, status="todo")
        after = task_snapshot(task)
        event = self.event(None, after)
        session = SimpleNamespace(
            get=AsyncMock(side_effect=[event, task]),
            scalar=AsyncMock(return_value=None),
            add=Mock(), flush=AsyncMock(), commit=AsyncMock(),
        )
        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            await undo_dashboard_action("undo-event-1", SimpleNamespace(id="member-1", role="member"), session)
        self.assertIsNotNone(task.deleted_at)
        self.assertEqual(task.version, 2)
        session.commit.assert_awaited_once()

    async def test_archived_task_undo_restores_the_same_task(self):
        before = task_snapshot(self.task(version=2, status="in_progress"))
        task = self.task(version=3, status="in_progress")
        task.deleted_at = datetime.now(timezone.utc)
        event = self.event(before, {"deleted": True, "version": 3})
        session = SimpleNamespace(
            get=AsyncMock(side_effect=[event, task]),
            scalar=AsyncMock(return_value=None),
            add=Mock(), flush=AsyncMock(), commit=AsyncMock(),
        )
        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            await undo_dashboard_action("undo-event-1", SimpleNamespace(id="member-1", role="member"), session)
        self.assertIsNone(task.deleted_at)
        self.assertEqual(task.version, 4)
        session.commit.assert_awaited_once()

    async def test_identity_mapping_creation_undo_removes_mapping(self):
        mapping = {
            "id": "mapping-1", "user_id": "member-1",
            "source": "tailscale", "external_identity": "inkii@example.com",
        }
        event = SimpleNamespace(
            id="mapping-event", actor_user_id="admin-1", task_id=None,
            payload={"undo": {"kind": "identity_mapping", "before": None, "after": mapping}},
            occurred_at=datetime.now(timezone.utc),
        )
        record = SimpleNamespace(**mapping)
        session = SimpleNamespace(
            get=AsyncMock(return_value=event),
            scalar=AsyncMock(side_effect=[None, record]),
            delete=AsyncMock(), add=Mock(), flush=AsyncMock(), commit=AsyncMock(),
        )
        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            result = await undo_dashboard_action(
                "mapping-event", SimpleNamespace(id="admin-1", role="admin"), session,
            )
        self.assertEqual(result, {"undone": True})
        session.delete.assert_awaited_once_with(record)
        session.commit.assert_awaited_once()

    async def test_estimate_review_creation_undo_removes_review(self):
        reviewed_at = datetime.now(timezone.utc)
        after = {
            "id": "review-1", "user_id": "member-1", "day": "2026-10-12",
            "status": "approved", "reviewed_by": "admin-1", "note": None,
            "adjusted_seconds": 180, "reviewed_at": reviewed_at.isoformat(),
        }
        event = SimpleNamespace(
            id="review-event", actor_user_id="admin-1", task_id=None,
            payload={"undo": {"kind": "estimate_review", "before": None, "after": after}},
            occurred_at=reviewed_at,
        )
        review = SimpleNamespace(
            id="review-1", user_id="member-1", day=date(2026, 10, 12),
            status="approved", reviewed_by="admin-1", note=None,
            adjusted_seconds=180, reviewed_at=reviewed_at,
        )
        session = SimpleNamespace(
            get=AsyncMock(return_value=event),
            scalar=AsyncMock(side_effect=[None, review]),
            delete=AsyncMock(), add=Mock(), flush=AsyncMock(), commit=AsyncMock(),
        )
        with patch("agent_manager.tracking.live_broker.publish", new_callable=AsyncMock):
            result = await undo_dashboard_action(
                "review-event", SimpleNamespace(id="admin-1", role="admin"), session,
            )
        self.assertEqual(result, {"undone": True})
        session.delete.assert_awaited_once_with(review)
        session.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
