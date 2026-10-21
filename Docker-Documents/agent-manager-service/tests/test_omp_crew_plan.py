from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from agent_manager.omp import OmpTurn, accept_or_bounce, mission_status


class CapturingJetStream:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: bytes, headers=None) -> None:
        self.published.append((subject, json.loads(payload.decode("utf-8"))))


class FakeSession:
    def __init__(self) -> None:
        self.records = []

    def add(self, record) -> None:
        self.records.append(record)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


class OmpCrewPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_omp_dispatches_its_selected_crew_counts(self) -> None:
        goal = "Create a hello text artifact with readable content. " * 18
        jetstream = CapturingJetStream()
        request = SimpleNamespace(
            state=SimpleNamespace(auth_method="open-webui"),
            app=SimpleNamespace(state=SimpleNamespace(
                nats=SimpleNamespace(jetstream=lambda: jetstream),
            )),
        )
        session = FakeSession()
        actor = SimpleNamespace(id="member-1")
        body = OmpTurn(
            goal=goal,
            forgejo_repository="lab/report-generator",
            project_name="report-generator",
        )

        with patch("agent_manager.omp.record_activity", new_callable=AsyncMock):
            reply = await accept_or_bounce(body, actor, request, session)

        self.assertEqual(reply["type"], "mission")
        self.assertEqual(len(jetstream.published), 1)
        subject, event = jetstream.published[0]
        self.assertEqual(subject, "agent.runs.created")
        plan = event["metadata"]["mission"]
        self.assertEqual(plan["planners"], 1)
        self.assertEqual(plan["coders"], 2)
        self.assertEqual(plan["reviewers"], 1)
        self.assertEqual(event["metadata"]["crew"]["size"]["size"], "medium")

    async def test_final_result_exposes_crew_plan_and_evidence(self) -> None:
        record = SimpleNamespace(
            id="mission-1",
            status="completed",
            run_metadata={
                "operation": "mission",
                "ledger": {"result_summary": "Supervisor accepted the crew result."},
                "result": {
                    "crew_plan": {"planners": 1, "coders": 3, "reviewers": 2},
                    "attempts": 2,
                    "evidence": {"coder_artifacts": ["candidate-1.txt"]},
                    "crew_outcome": "accepted",
                },
            },
        )

        class StatusSession:
            async def get(self, model, mission_id: str):
                return record

        result = await mission_status("mission-1", SimpleNamespace(), StatusSession())

        self.assertEqual(result["crew_plan"]["coders"], 3)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["evidence"]["coder_artifacts"], ["candidate-1.txt"])
        self.assertEqual(result["crew_outcome"], "accepted")


if __name__ == "__main__":
    unittest.main()
