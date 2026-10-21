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


class OmpMissionPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_omp_dispatches_the_real_calculator_goal(self) -> None:
        goal = (
            "Could you generate a GUI of a calculator that computes differences, "
            "additions, subtractions, multiplication, and dividing?"
        )
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
            forgejo_repository="lab/calculator",
            project_name="calculator",
        )

        with patch("agent_manager.omp.record_activity", new_callable=AsyncMock):
            reply = await accept_or_bounce(body, actor, request, session)

        self.assertEqual(reply["type"], "mission")
        self.assertEqual(len(jetstream.published), 1)
        subject, event = jetstream.published[0]
        self.assertEqual(subject, "agent.runs.created")
        plan = event["metadata"]["mission"]
        self.assertEqual(plan["planners"], 1)
        self.assertEqual(plan["coders"], 1)
        self.assertEqual(plan["reviewers"], 1)
        self.assertEqual(event["metadata"]["crew"]["engine"], "worker-local-model")

    async def test_final_result_exposes_artifacts_and_worker_identity(self) -> None:
        record = SimpleNamespace(
            id="mission-1",
            worker_id="worker-03",
            status="completed",
            run_metadata={
                "operation": "mission",
                "ledger": {"result_summary": "Generated calculator.html."},
                "result": {
                    "crew_plan": {"planners": 1, "coders": 1, "reviewers": 1},
                    "attempts": 1,
                    "evidence": {"artifacts": ["calculator.html"]},
                    "artifacts": [{"path": "calculator.html", "content": "<html></html>"}],
                    "crew_outcome": "accepted",
                },
            },
        )

        class StatusSession:
            async def get(self, model, mission_id: str):
                return record

        result = await mission_status("mission-1", SimpleNamespace(), StatusSession())

        self.assertEqual(result["crew_plan"]["coders"], 1)
        self.assertEqual(result["worker_id"], "worker-03")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["evidence"]["artifacts"], ["calculator.html"])
        self.assertEqual(result["artifacts"][0]["path"], "calculator.html")
        self.assertEqual(result["crew_outcome"], "accepted")



if __name__ == "__main__":
    unittest.main()
