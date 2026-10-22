from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_manager.omp import OmpTurn, accept_or_bounce, mission_status


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
        self.assertEqual(len(jetstream.events), 1)
        subject, event, _headers = jetstream.events[0]
        self.assertEqual(subject, "agent.runs.created")
        self.assertEqual(event["run_id"], reply["mission_id"])
        self.assertEqual(event["metadata"]["operation"], "mission")


    async def test_mission_status_exposes_valid_reviewer_count(self) -> None:
        record = SimpleNamespace(
            id="mission-1",
            status="failed",
            run_metadata={
                "operation": "mission",
                "result": {"reviewer_count": 3},
                "ledger": {"result_summary": "Reviewer requested changes."},
            },
        )
        session = SimpleNamespace(get=AsyncMock(return_value=record))
        response = await mission_status("mission-1", SimpleNamespace(), session)
        self.assertEqual(response["reviewer_count"], 3)

    async def test_mission_status_sanitizes_invalid_reviewer_count(self) -> None:
        for value in (True, -1, "3", None):
            with self.subTest(value=value):
                record = SimpleNamespace(
                    id="mission-1",
                    status="failed",
                    run_metadata={
                        "operation": "mission",
                        "result": {"reviewer_count": value},
                        "ledger": {},
                    },
                )
                session = SimpleNamespace(get=AsyncMock(return_value=record))
                response = await mission_status("mission-1", SimpleNamespace(), session)
                self.assertIsNone(response["reviewer_count"])

if __name__ == "__main__":
    unittest.main()
