from __future__ import annotations

import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from agent_manager.artifacts import upload_token_hash
from agent_manager.models import ArtifactBlobRecord, RunRecord
from agent_manager.omp import (
    MissionClarification,
    OmpTurn,
    accept_or_bounce,
    cancel_mission,
    load_modification_source,
    mission_cancel_token_hash,
    mission_control,
    mission_status,
    omp_turn,
    resume_mission,
)


class CapturingJetStream:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, dict | None]] = []

    async def publish(
        self, subject: str, data: bytes, headers: dict | None = None
    ) -> None:
        self.events.append((subject, json.loads(data.decode("utf-8")), headers))


class MissionReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_mission_returns_receipt_for_open_webui_polling(self) -> None:
        jetstream = CapturingJetStream()
        request = SimpleNamespace(
            state=SimpleNamespace(auth_method="open-webui"),
            app=SimpleNamespace(
                state=SimpleNamespace(
                    nats=SimpleNamespace(jetstream=lambda: jetstream)
                )
            ),
        )
        actor = SimpleNamespace(id="open-webui-service")
        session = SimpleNamespace(
            add=lambda _record: None,
            flush=AsyncMock(),
            commit=AsyncMock(),
        )
        body = OmpTurn(
            goal="Build a small Python utility that writes a greeting to a file and includes a short usage note.",
            forgejo_repository="test/smoke",
            project_name="smoke-project",
        )

        with patch("agent_manager.omp.record_activity", new=AsyncMock()):
            reply = await accept_or_bounce(body, actor, request, session)

        self.assertEqual(reply["type"], "mission")
        self.assertEqual(reply["status"], "queued")
        self.assertTrue(reply["mission_id"])
        self.assertGreaterEqual(len(reply["cancel_token"]), 32)
        self.assertEqual(len(jetstream.events), 1)
        subject, event, _headers = jetstream.events[0]
        self.assertEqual(subject, "agent.runs.created")
        self.assertEqual(event["run_id"], reply["mission_id"])
        self.assertEqual(event["metadata"]["operation"], "mission")
        self.assertIn("artifact_control_url", event["metadata"])
        self.assertNotIn("mission_cancel_token_hash", event["metadata"])

    async def test_modification_creates_child_and_checks_parent_owner(self) -> None:
        parent_id = "8a9e15ac-4f53-45b1-a7c8-34f7c8b0e3a1"
        child_id = "abc6aa27-03dc-47e1-9357-f5ca3cb1f6e0"
        artifact_id = "4ba9c3c2-61b8-4b66-b5d5-804547e64ab1"
        actor = SimpleNamespace(id="open-webui-service")
        parent = SimpleNamespace(
            id=parent_id,
            goal="Build a calculator application.",
            forgejo_repository="owner/source",
            base_ref="release",
            model="luna",
            status="completed",
            run_metadata={
                "operation": "mission",
                "source": "open-webui",
                "submitted_by": actor.id,
                "requester_id": "openwebui-user-1",
                "project_name": "Calculator",
                "artifact": {"id": artifact_id},
            },
        )
        blob = SimpleNamespace(
            id=artifact_id,
            run_id=parent_id,
            filename="artifacts.zip",
            content_type="application/zip",
            size_bytes=123,
            sha256="a" * 64,
        )
        child_records = []
        async def get(model, key, **_kwargs):
            if model is RunRecord:
                return parent if key == parent_id else None
            if model is ArtifactBlobRecord:
                return blob if key == artifact_id else None
            raise AssertionError(f"unexpected record lookup: {model}")

        session = SimpleNamespace(
            get=AsyncMock(side_effect=get),
            add=child_records.append,
            flush=AsyncMock(),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )
        jetstream = CapturingJetStream()
        request = SimpleNamespace(
            state=SimpleNamespace(auth_method="open-webui"),
            app=SimpleNamespace(
                state=SimpleNamespace(nats=SimpleNamespace(jetstream=lambda: jetstream))
            ),
        )
        source_descriptor = {
            "id": artifact_id,
            "filename": "artifacts.zip",
            "content_type": "application/zip",
            "size_bytes": 123,
            "sha256": "a" * 64,
            "download_url": (
                "https://infra-lab-services.tail494f6d.ts.net/v1/omp/missions/"
                f"{parent_id}/artifacts/{artifact_id}/download?expires=123&signature=abc"
            ),
        }
        body = OmpTurn(
            goal="Can you add a dark mode to the app?",
            mission_id=uuid.UUID(child_id),
            cancel_token="c" * 40,
            parent_mission_id=uuid.UUID(parent_id),
            requester_id="openwebui-user-1",
        )

        with (
            patch("agent_manager.omp.mission_artifact", return_value=source_descriptor),
            patch("agent_manager.omp.record_activity", new=AsyncMock()),
        ):
            reply = await omp_turn(body, request, actor, session)

        self.assertEqual(reply["mission_id"], child_id)
        self.assertEqual(len(child_records), 1)
        child = child_records[0]
        self.assertEqual(child.status, "queued")
        self.assertEqual(child.forgejo_repository, parent.forgejo_repository)
        self.assertEqual(child.base_ref, parent.base_ref)
        self.assertEqual(child.model, parent.model)
        self.assertEqual(child.run_metadata["parent_mission_id"], parent_id)
        self.assertEqual(child.run_metadata["requester_id"], "openwebui-user-1")
        self.assertEqual(child.run_metadata["parent_artifact_id"], artifact_id)
        self.assertEqual(jetstream.events[0][0], "agent.runs.created")
        event = jetstream.events[0][1]
        self.assertEqual(event["goal"], body.goal)
        self.assertEqual(event["metadata"]["artifact_input"]["parent_mission_id"], parent_id)
        self.assertEqual(event["metadata"]["artifact_input"]["download_url"], source_descriptor["download_url"])

        with patch("agent_manager.omp.mission_artifact", return_value=source_descriptor):
            with self.assertRaises(HTTPException) as caught:
                await load_modification_source(
                    uuid.UUID(parent_id), SimpleNamespace(id="different-user"), session
                )
        self.assertEqual(caught.exception.status_code, 404)
        with self.assertRaises(HTTPException) as caught:
            await load_modification_source(
                uuid.UUID(parent_id), actor, session, "different-openwebui-user"
            )
        self.assertEqual(caught.exception.status_code, 404)


        blob.content_type = "text/html"
        blob.filename = "calculator.html"
        with self.assertRaises(HTTPException) as caught:
            await load_modification_source(
                uuid.UUID(parent_id), actor, session, "openwebui-user-1"
            )
        self.assertEqual(caught.exception.status_code, 409)


    async def test_mission_status_exposes_valid_reviewer_count(self) -> None:
        record = SimpleNamespace(
            id="mission-1",
            status="failed",
            error=None,
            worker_id=None,
            run_metadata={
                "operation": "mission",
                "result": {"reviewer_count": 3},
                "ledger": {"result_summary": "Reviewer requested changes."},
            },
        )
        session = SimpleNamespace(get=AsyncMock(return_value=record))
        response = await mission_status("mission-1", SimpleNamespace(), session)
        self.assertEqual(response["reviewer_count"], 3)

    async def test_needs_input_status_exposes_question_without_artifact(self) -> None:
        record = SimpleNamespace(
            id="mission-clarification",
            status="needs_input",
            error=None,
            worker_id=None,
            run_metadata={
                "operation": "mission",
                "result": {
                    "reviewer_count": 10,
                    "question": "Should the result favor speed or accuracy?",
                },
                "ledger": {"result_summary": "Reviewer limit reached."},
            },
        )
        session = SimpleNamespace(get=AsyncMock(return_value=record))

        response = await mission_status("mission-clarification", SimpleNamespace(), session)

        self.assertEqual(response["status"], "needs_input")
        self.assertEqual(response["reviewer_count"], 10)
        self.assertEqual(
            response["question"], "Should the result favor speed or accuracy?"
        )
        self.assertIsNone(response["artifact"])

    async def test_mission_status_sanitizes_invalid_reviewer_count(self) -> None:
        for value in (True, -1, "3", None):
            with self.subTest(value=value):
                record = SimpleNamespace(
                    id="mission-1",
                    status="failed",
                    error=None,
                    worker_id=None,
                    run_metadata={
                        "operation": "mission",
                        "result": {"reviewer_count": value},
                        "ledger": {},
                    },
                )
                session = SimpleNamespace(get=AsyncMock(return_value=record))
                response = await mission_status("mission-1", SimpleNamespace(), session)
                self.assertIsNone(response["reviewer_count"])

    async def test_queued_mission_cancel_is_idempotent(self) -> None:
        token = "t" * 40
        jetstream = CapturingJetStream()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(nats=SimpleNamespace(jetstream=lambda: jetstream))
            )
        )
        record = SimpleNamespace(
            id="mission-1",
            status="queued",
            completed_at=None,
            run_metadata={
                "operation": "mission",
                "mission_cancel_token_hash": mission_cancel_token_hash(token),
            },
        )
        session = SimpleNamespace(
            get=AsyncMock(return_value=record),
            commit=AsyncMock(),
        )

        first = await cancel_mission(
            "mission-1", request, f"Bearer {token}", session
        )
        second = await cancel_mission(
            "mission-1", request, f"Bearer {token}", session
        )

        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(second["status"], "cancelled")
        self.assertIsNotNone(record.completed_at)
        session.commit.assert_awaited_once()
        self.assertEqual(len(jetstream.events), 1)
        self.assertEqual(jetstream.events[0][0], "agent.runs.cancelled")

    async def test_running_mission_cancel_requests_worker_stop(self) -> None:
        token = "t" * 40
        jetstream = CapturingJetStream()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(nats=SimpleNamespace(jetstream=lambda: jetstream))
            )
        )
        record = SimpleNamespace(
            id="mission-2",
            status="running",
            run_metadata={
                "operation": "mission",
                "mission_cancel_token_hash": mission_cancel_token_hash(token),
            },
        )
        session = SimpleNamespace(
            get=AsyncMock(return_value=record),
            commit=AsyncMock(),
        )

        response = await cancel_mission(
            "mission-2", request, f"Bearer {token}", session
        )

        self.assertEqual(response["status"], "cancel_requested")
        self.assertEqual(record.status, "cancel_requested")
        self.assertEqual(jetstream.events[0][0], "agent.runs.cancelled")

    async def test_mission_control_requires_worker_upload_capability(self) -> None:
        token = "upload-capability"
        record = SimpleNamespace(
            id="mission-3",
            status="cancel_requested",
            run_metadata={
                "operation": "mission",
                "artifact_upload_token_hash": upload_token_hash(token),
            },
        )
        session = SimpleNamespace(get=AsyncMock(return_value=record))

        with self.assertRaises(HTTPException) as caught:
            await mission_control(
                "mission-3", "Bearer wrong", session
            )
        self.assertEqual(caught.exception.status_code, 404)
        response = await mission_control(
            "mission-3", f"Bearer {token}", session
        )
        self.assertTrue(response["cancel_requested"])
        self.assertEqual(response["status"], "cancel_requested")

    async def test_resume_resets_reviewer_count_and_targets_original_worker(self) -> None:
        token = "c" * 40
        jetstream = CapturingJetStream()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(nats=SimpleNamespace(jetstream=lambda: jetstream))
            )
        )
        record = SimpleNamespace(
            id="mission-resume",
            goal="Create a product with clear behavior.",
            forgejo_repository="test/smoke",
            base_ref="main",
            model=None,
            worker_id="worker-03",
            status="needs_input",
            error=None,
            completed_at=None,
            run_metadata={
                "operation": "mission",
                "project_name": "smoke",
                "worker_role": "coder",
                "mission_cancel_token_hash": mission_cancel_token_hash(token),
                "result": {
                    "status": "needs_input",
                    "reviewer_count": 10,
                    "question": "Should the result favor speed or accuracy?",
                },
                "ledger": {"result_summary": "Reviewer limit reached."},
            },
        )
        session = SimpleNamespace(
            get=AsyncMock(return_value=record),
            commit=AsyncMock(),
        )

        reply = await resume_mission(
            record.id,
            MissionClarification(clarification="Prioritize accuracy."),
            request,
            f"Bearer {token}",
            session,
        )

        self.assertEqual(reply["mission_id"], record.id)
        self.assertEqual(reply["status"], "queued")
        self.assertEqual(record.status, "queued")
        self.assertEqual(record.run_metadata["result"]["reviewer_count"], 0)
        self.assertEqual(record.run_metadata["resume_count"], 1)
        self.assertEqual(len(jetstream.events), 1)
        subject, event, _headers = jetstream.events[0]
        self.assertEqual(subject, "agent.runs.resume.worker-03")
        self.assertEqual(event["run_id"], record.id)
        self.assertTrue(event["metadata"]["resume_from_checkpoint"])
        self.assertEqual(event["metadata"]["reviewer_count_reset"], 0)
        self.assertEqual(event["metadata"]["clarification"], "Prioritize accuracy.")
    async def test_waiting_for_input_mission_can_be_explicitly_cancelled(self) -> None:
        token = "c" * 40
        jetstream = CapturingJetStream()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(nats=SimpleNamespace(jetstream=lambda: jetstream))
            )
        )
        record = SimpleNamespace(
            id="mission-waiting",
            status="needs_input",
            completed_at=None,
            run_metadata={
                "operation": "mission",
                "mission_cancel_token_hash": mission_cancel_token_hash(token),
            },
        )
        session = SimpleNamespace(
            get=AsyncMock(return_value=record),
            commit=AsyncMock(),
        )

        response = await cancel_mission(
            record.id, request, f"Bearer {token}", session
        )

        self.assertEqual(response["status"], "cancelled")
        self.assertIsNotNone(record.completed_at)
        self.assertEqual(jetstream.events[0][0], "agent.runs.cancelled")

if __name__ == "__main__":
    unittest.main()
