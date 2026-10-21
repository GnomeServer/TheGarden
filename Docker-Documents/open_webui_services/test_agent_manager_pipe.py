from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import agent_manager_pipe
from agent_manager_pipe import Pipe


class RunNamingTests(unittest.TestCase):
    def test_directives_are_removed_from_the_task(self) -> None:
        self.assertEqual(
            Pipe._extract_run_directives(
                "Project: Quarterly Reports\nScript: audit_report\n\nBuild a report."
            ),
            ("Build a report.", "Quarterly Reports", "audit_report"),
        )

    def test_script_name_comes_from_request_content(self) -> None:
        self.assertEqual(
            Pipe._suggest_script_name(
                "Create a dependency-free Python script that writes a report.json file."
            ),
            "dependency_free_report_json.py",
        )


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeAgentManagerClient:
    def __init__(self) -> None:
        self.url: str | None = None
        self.request_body: dict | None = None
        self.states = [
            {
                "type": "mission",
                "mission_id": "mission-1",
                "status": "queued",
                "summary": "OMP handed the job to a worker laptop. Crew: 1 planner, 2 coders, 2 reviewers.",
            },
            {
                "type": "mission",
                "mission_id": "mission-1",
                "status": "running",
                "summary": "OMP handed the job to a worker laptop. Crew is running.",
            },
            {
                "type": "mission",
                "mission_id": "mission-1",
                "status": "completed",
                "summary": "Supervisor accepted 2 coder artifacts after 1 attempt.",
                "question": None,
                "attempts": 1,
                "evidence": {
                    "coder_artifacts": ["candidate-1.txt", "candidate-2.txt"],
                    "reviewers": [
                        {"reviewer_id": 1, "decision": "approved"},
                        {"reviewer_id": 2, "decision": "approved"},
                    ],
                    "supervisor_checks": ["artifacts", "reviews", "policy"],
                },
            },
        ]
    async def __aenter__(self) -> FakeAgentManagerClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.url = url
        self.request_body = json
        if not json.get("project_name"):
            return FakeResponse({"type": "question", "text": "What project name should this mission use?"})
        return FakeResponse(
            {
                "type": "mission",
                "mission_id": "mission-1",
                "status": "accepted",
                "summary": "Accepted by the orchestrator. A crew has not started.",
            }
        )

    async def get(self, url: str, *, headers: dict) -> FakeResponse:
        self.get_url = url
        return FakeResponse(self.states.pop(0))


class PipeSubmissionTests(unittest.IsolatedAsyncioTestCase):
    def configured_pipe(self) -> Pipe:
        pipe = Pipe()
        pipe.valves.AGENT_MANAGER_API_TOKEN = "test-token"
        pipe.valves.FORGEJO_REPOSITORY = "owner/repository"
        pipe.valves.POLL_INTERVAL_SECONDS = 0.5
        return pipe

    async def test_missing_project_is_a_question_not_a_run(self) -> None:
        pipe = self.configured_pipe()
        event_call = AsyncMock()
        client = FakeAgentManagerClient()
        prompt = "Create a dependency-free Python script that writes a report.json file."

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            result = await pipe.pipe(
                {"messages": [{"role": "user", "content": prompt}]},
                __event_call__=event_call,
            )

        event_call.assert_not_awaited()
        self.assertTrue(client.url.endswith("/v1/omp/turns"))
        self.assertNotIn("/v1/runs", client.url)
        self.assertEqual(result, "What project name should this mission use?")
        self.assertNotIn("script_name", client.request_body)

    async def test_explicit_names_go_to_omp_as_a_mission(self) -> None:
        pipe = self.configured_pipe()
        event_call = AsyncMock()
        client = FakeAgentManagerClient()
        prompt = (
            "Project: Quarterly Reports\nScript: audit_report.py\n\nBuild a report."
        )

        with patch.object(
            agent_manager_pipe.httpx, "AsyncClient", return_value=client
        ):
            await pipe.pipe(
                {"messages": [{"role": "user", "content": prompt}]},
                __event_call__=event_call,
            )

        event_call.assert_not_awaited()
        self.assertTrue(client.url.endswith("/v1/omp/turns"))
        self.assertEqual(client.request_body["goal"], "Build a report.")
        self.assertEqual(client.request_body["project_name"], "Quarterly Reports")
        self.assertNotIn("script_name", client.request_body)

    async def test_project_reply_keeps_the_previous_goal(self) -> None:
        pipe = self.configured_pipe()
        client = FakeAgentManagerClient()
        event_emitter = AsyncMock()
        messages = [
            {"role": "user", "content": "Write a short status note for the lab."},
            {"role": "assistant", "content": "What project name should this mission use?"},
            {
                "role": "user",
                "content": "The Garden is the project name and I want you to write a short status note for the lab.",
            },
        ]

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            result = await pipe.pipe(
                {"messages": messages},
                __event_emitter__=event_emitter,
            )

        self.assertEqual(client.request_body["project_name"], "The Garden")
        self.assertEqual(client.request_body["goal"], "write a short status note for the lab")
        self.assertIn("Reviewer verdicts: 2/2 approved.", result)
        self.assertIn("Coder artifacts checked: 2.", result)
        self.assertIn("Attempts: 1.", result)
        progress = [
            call.args[0]["data"]["description"]
            for call in event_emitter.await_args_list
        ]
        self.assertTrue(any("Crew: 1 planner, 2 coders, 2 reviewers." in item for item in progress))
        self.assertTrue(any("worker-local OMP is running the crew" in item for item in progress))
        self.assertTrue(event_emitter.await_args_list[-1].args[0]["data"]["done"])

if __name__ == "__main__":
    unittest.main()
