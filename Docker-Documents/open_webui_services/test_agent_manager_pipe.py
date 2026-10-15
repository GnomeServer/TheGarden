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
        self.request_body: dict | None = None

    async def __aenter__(self) -> FakeAgentManagerClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.request_body = json
        return FakeResponse(
            {
                "id": "manager-uuid",
                "status": "queued",
                "metadata": {"project_name": json["project_name"]},
            }
        )

    async def get(self, url: str, *, headers: dict) -> FakeResponse:
        project = self.request_body["project_name"]
        return FakeResponse(
            {
                "id": "manager-uuid",
                "status": "completed",
                "metadata": {
                    "project_name": project,
                    "result": {
                        "stdout": "report generated\n",
                        "script": (
                            "/workspace/quarterly-reports/"
                            "dependency_free_report_json.py"
                        ),
                    },
                },
            }
        )


class PipeSubmissionTests(unittest.IsolatedAsyncioTestCase):
    def configured_pipe(self) -> Pipe:
        pipe = Pipe()
        pipe.valves.AGENT_MANAGER_API_TOKEN = "test-token"
        pipe.valves.FORGEJO_REPOSITORY = "owner/repository"
        return pipe

    async def test_asks_project_and_submits_a_derived_filename(self) -> None:
        pipe = self.configured_pipe()
        event_call = AsyncMock(return_value="Quarterly Reports")
        client = FakeAgentManagerClient()
        prompt = "Create a dependency-free Python script that writes a report.json file."

        with patch.object(
            agent_manager_pipe.httpx, "AsyncClient", return_value=client
        ):
            result = await pipe.pipe(
                {"messages": [{"role": "user", "content": prompt}]},
                __event_call__=event_call,
            )

        event_call.assert_awaited_once()
        self.assertEqual(event_call.await_args.args[0]["type"], "input")
        self.assertEqual(client.request_body["goal"], prompt)
        self.assertEqual(client.request_body["project_name"], "Quarterly Reports")
        self.assertEqual(
            client.request_body["script_name"], "dependency_free_report_json.py"
        )
        self.assertIn("Project: Quarterly Reports", result)
        self.assertNotIn("manager-uuid", result)

    async def test_explicit_names_skip_project_prompt(self) -> None:
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
        self.assertEqual(client.request_body["goal"], "Build a report.")
        self.assertEqual(client.request_body["project_name"], "Quarterly Reports")
        self.assertEqual(client.request_body["script_name"], "audit_report.py")


if __name__ == "__main__":
    unittest.main()
