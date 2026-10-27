from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import agent_manager_pipe
from agent_manager_pipe import Pipe


class ProjectDirectiveTests(unittest.TestCase):
    def test_script_filename_instruction_remains_in_the_mission_goal(self) -> None:
        self.assertEqual(
            Pipe._extract_project_directive(
                "Project: Quarterly Reports\nScript: audit_report.py\n\nBuild a report."
            ),
            ("Script: audit_report.py\n\nBuild a report.", "Quarterly Reports"),
        )

    def test_script_directive_without_project_is_not_discarded(self) -> None:
        prompt = "Script: audit_report.py\n\nBuild a report."
        self.assertEqual(Pipe._extract_project_directive(prompt), (prompt, None))


class ArtifactFormattingTests(unittest.TestCase):
    def test_signed_download_url_and_filename_are_preserved(self) -> None:
        signed_url = (
            "https://manager.example/v1/omp/missions/run-1/artifacts/id/download"
            "?expires=123&signature=abc"
        )
        result = Pipe._format_artifacts(
            {
                "filename": "report [final].zip",
                "download_url": signed_url,
                "size_bytes": 2048,
            }
        )

        self.assertIn(f"[report \\[final\\].zip]({signed_url})", result)
        self.assertIn("2,048 bytes", result)

    def test_missing_artifact_has_an_explicit_user_message(self) -> None:
        result = Pipe._format_artifacts(None)

        self.assertIn("completed", result)
        self.assertIn("no downloadable artifact", result)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeAgentManagerClient:
    signed_url = (
        "https://manager.example/v1/omp/missions/mission-1/artifacts/"
        "b2123ee9-84f6-4c12-9f07-11e023b1702c/download?expires=123&signature=abc"
    )

    def __init__(self) -> None:
        self.url: str | None = None
        self.request_body: dict | None = None
        self.post_calls = 0
        self.get_urls: list[str] = []
        self.resume_requests: list[tuple[str, dict, dict]] = []
        self.cancel_requests: list[tuple[str, dict]] = []
        self.cancel_on_get = False
        self.status_response = {
            "type": "mission",
            "mission_id": "be1dd0ef-8ec1-4d06-a141-77ee224ea67b",
            "status": "running",
            "summary": "Worker OMP started dynamic planning.",
            "worker_id": "worker-03",
        }
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
                "summary": "Worker generated calculator.html after 1 attempt.",
                "attempts": 1,
                "evidence": {
                    "artifacts": ["calculator.html"],
                    "reviewers": [
                        {"reviewer_id": 1, "decision": "approved"},
                        {"reviewer_id": 2, "decision": "approved"},
                    ],
                },
                "artifact": {
                    "filename": "calculator.html",
                    "download_url": self.signed_url,
                    "size_bytes": 2048,
                },
                "artifacts": [
                    {"path": "calculator.html", "content": "PRIVATE-PRODUCT-SOURCE"},
                ],
            },
        ]

    async def __aenter__(self) -> FakeAgentManagerClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict,
        json: dict | None = None,
    ) -> FakeResponse:
        self.post_calls += 1
        self.url = url
        if url.endswith("/cancel"):
            self.cancel_requests.append((url, headers))
            return FakeResponse({})
        request_body = json or {}
        if url.endswith("/resume"):
            self.resume_requests.append((url, headers, request_body))
            return FakeResponse(
                {"status": "queued", "summary": "Clarification received; resuming."}
            )
        self.request_body = request_body
        if not request_body.get("project_name"):
            return FakeResponse(
                {"type": "question", "text": "What project name should this mission use?"}
            )
        return FakeResponse(
            {
                "type": "mission",
                "mission_id": "mission-1",
                "status": "accepted",
                "summary": "Accepted by the orchestrator. A crew has not started.",
            }
        )

    async def get(self, url: str, *, headers: dict) -> FakeResponse:
        self.get_urls.append(url)
        if self.post_calls == 0:
            return FakeResponse(self.status_response)
        if self.cancel_on_get:
            self.cancel_on_get = False
            raise asyncio.CancelledError
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

    async def test_script_directive_stays_in_goal_and_is_not_a_script_field(self) -> None:
        pipe = self.configured_pipe()
        client = FakeAgentManagerClient()
        prompt = "Project: Quarterly Reports\nScript: audit_report.py\n\nBuild a report."

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            await pipe.pipe({"messages": [{"role": "user", "content": prompt}]})

        self.assertEqual(
            client.request_body["goal"], "Script: audit_report.py\n\nBuild a report."
        )
        self.assertEqual(client.request_body["project_name"], "Quarterly Reports")
        self.assertNotIn("script_name", client.request_body)

    async def test_project_reply_returns_download_link_without_product_source(self) -> None:
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
        self.assertIn("Artifacts produced: 1.", result)
        self.assertIn(f"[calculator.html]({client.signed_url})", result)
        self.assertIn("Attempts: 1.", result)
        self.assertNotIn("PRIVATE-PRODUCT-SOURCE", result)
        progress = [
            call.args[0]["data"]["description"]
            for call in event_emitter.await_args_list
        ]
        self.assertTrue(any("Crew: 1 planner, 2 coders, 2 reviewers." in item for item in progress))
        self.assertTrue(any("worker-local OMP is running the crew" in item for item in progress))
        self.assertTrue(event_emitter.await_args_list[-1].args[0]["data"]["done"])

    async def test_status_followup_queries_existing_mission(self) -> None:
        pipe = self.configured_pipe()
        client = FakeAgentManagerClient()
        mission_id = "be1dd0ef-8ec1-4d06-a141-77ee224ea67b"
        messages = [
            {"role": "user", "content": "Create a calculator GUI."},
            {
                "role": "assistant",
                "content": f"The crew is still working. Mission `{mission_id}`.",
            },
            {"role": "user", "content": "whats the current status"},
        ]

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            result = await pipe.pipe({"messages": messages})

        self.assertEqual(client.post_calls, 0)
        self.assertEqual(
            client.get_urls,
            [f"http://agent-manager:8000/v1/omp/missions/{mission_id}"],
        )
        self.assertIn(f"Mission `{mission_id}` is running on worker `worker-03`.", result)


    async def test_needs_input_resumes_same_mission_with_clarification(self) -> None:
        pipe = self.configured_pipe()
        client = FakeAgentManagerClient()
        client.states = [
            {
                "status": "needs_input",
                "summary": "Reviewers reached the attempt limit.",
                "question": "Which behavior should change?",
            },
            {
                "status": "completed",
                "summary": "The revised product passed review.",
                "artifact": {
                    "filename": "product.zip",
                    "download_url": client.signed_url,
                    "size_bytes": 2048,
                },
            },
        ]
        event_call = AsyncMock(return_value="Keep the existing layout and fix the calculation.")
        messages = [
            {"role": "user", "content": "Project: Garden\nBuild a calculator."},
        ]

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            result = await pipe.pipe(
                {"messages": messages},
                __event_call__=event_call,
            )

        event_call.assert_awaited_once()
        self.assertEqual(
            client.resume_requests,
            [
                (
                    "http://agent-manager:8000/v1/omp/missions/mission-1/resume",
                    {"Authorization": f"Bearer {client.request_body['cancel_token']}"},
                    {"clarification": "Keep the existing layout and fix the calculation."},
                )
            ],
        )
        self.assertIn(f"[product.zip]({client.signed_url})", result)

    async def test_stopping_mission_requests_backend_cancellation(self) -> None:
        pipe = self.configured_pipe()
        client = FakeAgentManagerClient()
        client.cancel_on_get = True
        messages = [{"role": "user", "content": "Project: Garden\nBuild a calculator."}]

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(asyncio.CancelledError):
                await pipe.pipe({"messages": messages})

        self.assertEqual(
            client.cancel_requests,
            [
                (
                    "http://agent-manager:8000/v1/omp/missions/mission-1/cancel",
                    {"Authorization": f"Bearer {client.request_body['cancel_token']}"},
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
