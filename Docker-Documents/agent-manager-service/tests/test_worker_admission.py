from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError

from agent_manager.main import (
    RUN_STREAM,
    WORKER_STREAM,
    WORKER_SUBJECT,
    RunCreate,
    create_run,
    ensure_stream,
    ensure_worker_consumer_filter,
    find_available_worker,
)
from agent_manager.models import ActivityRecord, RunRecord


class RunAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_worker_rejects_without_creating_run(self) -> None:
        connection = SimpleNamespace(request=AsyncMock(side_effect=NoRespondersError))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(nats=connection)))
        session = SimpleNamespace(add=Mock(), flush=AsyncMock(), commit=AsyncMock())
        body = RunCreate(goal="private prompt that must not enter activity", forgejo_repository="lab/test")
        with patch("agent_manager.main.live_broker.publish", new_callable=AsyncMock) as publish:
            with self.assertRaises(HTTPException) as caught:
                await create_run(body, request, SimpleNamespace(id="human"), session)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "NO_WORKER_AVAILABLE")
        self.assertEqual(caught.exception.headers["Retry-After"], "15")
        self.assertEqual(session.add.call_count, 1)
        event = session.add.call_args.args[0]
        self.assertIsInstance(event, ActivityRecord)
        self.assertEqual(event.event_type, "run.rejected")
        self.assertNotIn("private prompt", event.summary)
        self.assertEqual(event.payload, {})
        self.assertNotIsInstance(event, RunRecord)
        session.commit.assert_awaited_once()
        session.flush.assert_not_awaited()
        publish.assert_awaited_once()

    def test_heartbeat_stream_is_exact_and_distinct_from_core_health(self) -> None:
        self.assertEqual(WORKER_STREAM, "AGENT_WORKERS")
        self.assertEqual(WORKER_SUBJECT, "agent.workers.heartbeat")
    async def test_invalid_role_or_target_does_not_reach_queue(self) -> None:
        for metadata in (
            {"worker_role": "coder.*"}, {"worker_role": ""},
            {"worker_role": []}, {"worker_id": ""}, {"worker_id": 3},
        ):
            with self.subTest(metadata=metadata):
                connection = SimpleNamespace(request=AsyncMock())
                request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(nats=connection)))
                session = SimpleNamespace(add=Mock())
                body = RunCreate(goal="controlled task", forgejo_repository="lab/test", metadata=metadata)
                with self.assertRaises(HTTPException) as caught:
                    await create_run(body, request, SimpleNamespace(id="human"), session)
                self.assertEqual(caught.exception.status_code, 422)
                connection.request.assert_not_awaited()
                session.add.assert_not_called()

    async def test_storage_ack_wrong_role_and_wrong_target_are_not_workers(self) -> None:
        for payload in (
            {"stream": "AGENT_WORKERS", "seq": 1}, [],
            {"worker_id": "worker-03", "worker_role": "reviewer"},
            {"worker_id": "worker-02", "worker_role": "coder"},
            {"worker_id": None, "worker_role": "coder"},
        ):
            with self.subTest(payload=payload):
                connection = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(
                    data=json.dumps(payload).encode(),
                )))
                self.assertIsNone(await find_available_worker(connection, "coder", "worker-03"))

    async def test_unanswered_target_does_not_accept_run(self) -> None:
        connection = SimpleNamespace(request=AsyncMock(side_effect=NatsTimeoutError))
        self.assertIsNone(await find_available_worker(connection, "coder", "worker-03"))


class WorkerStreamMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wildcard_stream_and_durable_are_narrowed_without_deletion(self):
        stream_config = SimpleNamespace(subjects=["agent.workers.>"])
        consumer_config = SimpleNamespace(filter_subject="agent.workers.>")
        manager = SimpleNamespace(
            stream_info=AsyncMock(return_value=SimpleNamespace(config=stream_config)),
            update_stream=AsyncMock(),
            add_stream=AsyncMock(),
            consumer_info=AsyncMock(return_value=SimpleNamespace(config=consumer_config)),
            update_consumer=AsyncMock(),
        )
        connection = SimpleNamespace(jetstream=lambda: manager)
        await ensure_stream(connection, WORKER_STREAM, [WORKER_SUBJECT])
        await ensure_worker_consumer_filter(connection)
        self.assertEqual(stream_config.subjects, [WORKER_SUBJECT])
        self.assertEqual(consumer_config.filter_subject, WORKER_SUBJECT)
        manager.update_stream.assert_awaited_once()
        manager.update_consumer.assert_awaited_once()
        manager.add_stream.assert_not_awaited()

    async def test_unsupported_stream_update_reports_safe_operator_migration(self):
        manager = SimpleNamespace(
            stream_info=AsyncMock(return_value=SimpleNamespace(
                config=SimpleNamespace(subjects=["agent.workers.>"])
            )),
            update_stream=AsyncMock(side_effect=RuntimeError("consumer conflict")),
            add_stream=AsyncMock(),
        )
        connection = SimpleNamespace(jetstream=lambda: manager)
        with self.assertRaises(RuntimeError) as caught:
            await ensure_stream(connection, WORKER_STREAM, [WORKER_SUBJECT])
        self.assertIn("operator migration", str(caught.exception))
        manager.add_stream.assert_not_awaited()


WORKER_SOURCE = Path(os.environ.get(
    "TEST_WORKER_SOURCE", str(Path(__file__).resolve().parents[3] / "agent-worker" / "worker.py")
))


class WorkerHealthTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("worker_under_test", WORKER_SOURCE)
        cls.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.worker)

    async def test_disabled_worker_never_advertises_execution(self) -> None:
        message = SimpleNamespace(reply="_INBOX.test", data=b"{}", respond=AsyncMock())
        with patch.object(self.worker, "ALLOW_GENERATED_CODE", False):
            await self.worker.respond_to_health(message)
        message.respond.assert_not_awaited()

    async def test_other_targets_and_malformed_requests_are_ignored(self) -> None:
        for data in (b"{", b"[]", b"null", b'{"worker_id":"worker-02"}'):
            with self.subTest(data=data):
                message = SimpleNamespace(reply="_INBOX.test", data=data, respond=AsyncMock())
                with patch.object(self.worker, "ALLOW_GENERATED_CODE", True), patch.object(self.worker, "WORKER_ID", "worker-03"):
                    await self.worker.respond_to_health(message)
                message.respond.assert_not_awaited()
