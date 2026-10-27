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

    def test_new_task_after_repository_question_is_not_consumed_as_a_reply(self) -> None:
        body = {
            "messages": [
                {"role": "user", "content": "Create a summary of the lab."},
                {
                    "role": "assistant",
                    "content": "Which Forgejo repository should this mission use? For example owner/project.",
                },
                {
                    "role": "user",
                    "content": "Project: fresh-report\nBuild a report for the lab.",
                },
            ]
        }

        goal, project_name, repository = Pipe._resolve_turn(body, "default/repository")

        self.assertEqual(goal, "Build a report for the lab.")
        self.assertEqual(project_name, "fresh-report")
        self.assertEqual(repository, "default/repository")

    def test_valid_repository_reply_continues_the_waiting_task(self) -> None:
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "Project: report\nBuild a summary of the lab.",
                },
                {
                    "role": "assistant",
                    "content": "Which Forgejo repository should this mission use? For example owner/project.",
                },
                {"role": "user", "content": "owner/repository"},
            ]
        }

        goal, project_name, repository = Pipe._resolve_turn(body, "")

        self.assertEqual(goal, "Build a summary of the lab.")
        self.assertEqual(project_name, "report")
        self.assertEqual(repository, "owner/repository")

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

    async def test_project_name_is_optional_and_manager_derives_title(self) -> None:
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
        self.assertIsNone(client.request_body["project_name"])
        self.assertIn("Mission `mission-1`", result)
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

    async def test_project_directive_returns_download_link_without_product_source(self) -> None:
        pipe = self.configured_pipe()
        client = FakeAgentManagerClient()
        event_emitter = AsyncMock()
        messages = [
            {
                "role": "user",
                "content": "Project: The Garden\nWrite a short status note for the lab.",
            }
        ]

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            result = await pipe.pipe(
                {"messages": messages},
                __event_emitter__=event_emitter,
            )

        self.assertEqual(client.request_body["project_name"], "The Garden")
        self.assertEqual(client.request_body["goal"], "Write a short status note for the lab.")
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


    async def test_modification_followup_uses_previous_completed_artifact(self) -> None:
        pipe = self.configured_pipe()
        client = FakeAgentManagerClient()
        parent_id = "8a9e15ac-4f53-45b1-a7c8-34f7c8b0e3a1"
        artifact_id = "4ba9c3c2-61b8-4b66-b5d5-804547e64ab1"
        artifact_url = (
            "https://manager.example/v1/omp/missions/"
            f"{parent_id}/artifacts/{artifact_id}/download?expires=123&signature=abc"
        )
        messages = [
            {"role": "user", "content": "Build a calculator."},
            {
                "role": "assistant",
                "content": (
                    f"Mission `{parent_id}` completed.\n"
                    f"Download the completed work:\n- [artifacts.zip]({artifact_url})"
                ),
            },
            {"role": "user", "content": "Can you add a dark mode to the app?"},
        ]

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            result = await pipe.pipe(
                {"messages": messages},
                __user__={"id": "openwebui-user-1"},
            )

        self.assertEqual(client.request_body["parent_mission_id"], parent_id)
        self.assertEqual(client.request_body["requester_id"], "openwebui-user-1")
        self.assertEqual(
            client.request_body["goal"], "Can you add a dark mode to the app?"
        )
        self.assertIn(f"[calculator.html]({client.signed_url})", result)

    async def test_modification_without_a_downloaded_artifact_does_not_start_a_run(self) -> None:
        pipe = self.configured_pipe()
        client = FakeAgentManagerClient()
        parent_id = "8a9e15ac-4f53-45b1-a7c8-34f7c8b0e3a1"
        messages = [
            {"role": "user", "content": "Build a calculator."},
            {"role": "assistant", "content": f"Mission `{parent_id}` completed."},
            {"role": "user", "content": "Update it to support keyboard shortcuts."},
        ]

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            result = await pipe.pipe({"messages": messages})

        self.assertIn("completed artifact earlier in this chat", result)
        self.assertEqual(client.post_calls, 0)


    async def test_modification_requires_openwebui_user_identity(self) -> None:
        pipe = self.configured_pipe()
        client = FakeAgentManagerClient()
        parent_id = "8a9e15ac-4f53-45b1-a7c8-34f7c8b0e3a1"
        artifact_id = "4ba9c3c2-61b8-4b66-b5d5-804547e64ab1"
        artifact_url = (
            "https://manager.example/v1/omp/missions/"
            f"{parent_id}/artifacts/{artifact_id}/download?expires=123&signature=abc"
        )
        messages = [
            {"role": "assistant", "content": f"[artifacts.zip]({artifact_url})"},
            {"role": "user", "content": "Add a dark mode to the app."},
        ]

        with patch.object(agent_manager_pipe.httpx, "AsyncClient", return_value=client):
            result = await pipe.pipe({"messages": messages})

        self.assertIn("identify the Open WebUI user", result)
        self.assertEqual(client.post_calls, 0)


if __name__ == "__main__":
    unittest.main()
