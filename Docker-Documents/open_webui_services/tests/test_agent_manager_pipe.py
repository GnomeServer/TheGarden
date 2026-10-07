import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import ValidationError


SOURCE = Path(__file__).resolve().parents[1] / "agent_manager_pipe.py"
SPEC = importlib.util.spec_from_file_location("agent_manager_pipe_under_test", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ASYNC_CLIENT = httpx.AsyncClient
TOKEN = "synthetic-test-run-only-token"
BODY = {"messages": [{"role": "user", "content": "Print the integers 1 through 3."}]}


def record(state, **result):
    # These are independent Manager states, never echoes of forwarded request input.
    return {"id": "run-42", "status": state, "error": None, "metadata": {"result": result}}


class PipeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pipe = MODULE.Pipe()
        self.pipe.valves = self.pipe.Valves(
            AGENT_MANAGER_API_KEY=TOKEN,
            FORGEJO_REPOSITORY="lab/disposable-smoke",
            POLL_INTERVAL_SECONDS=0.5,
            MAX_WAIT_SECONDS=5,
        )
        self.events = []
        self.requests = []
        self.now = 0

    async def exercise(self, responses, body=None, user=None, task=None, emitter=None):
        scenarios = iter(responses)

        def handler(request):
            self.requests.append(request)
            response = next(scenarios)
            if isinstance(response, BaseException):
                raise response
            status, data = response
            if isinstance(data, bytes):
                return httpx.Response(status, content=data)
            return httpx.Response(status, json=data)

        def client(**kwargs):
            self.assertFalse(kwargs["follow_redirects"])
            self.assertFalse(kwargs["trust_env"])
            return ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        async def emit(event):
            self.events.append(event)

        async def sleep(seconds):
            self.now += seconds

        clock = SimpleNamespace(monotonic=lambda: self.now)
        scheduler = SimpleNamespace(
            sleep=sleep, wait_for=asyncio.wait_for,
            TimeoutError=asyncio.TimeoutError, CancelledError=asyncio.CancelledError,
        )
        with patch.object(MODULE.httpx, "AsyncClient", side_effect=client), \
                patch.object(MODULE, "time", clock), patch.object(MODULE, "asyncio", scheduler):
            result = await self.pipe.pipe(
                BODY if body is None else body,
                __user__=user,
                __event_emitter__=emit if emitter is None else emitter,
                __task__=task,
            )
        self.assertNotIn(TOKEN, result)
        self.assertNotIn(TOKEN, json.dumps(self.events))
        if emitter is None:
            self.assertTrue(self.events[-1]["data"]["done"], "The final event must finish the spinner")
        self.assertLessEqual(sum(request.method == "POST" for request in self.requests), 1)
        return result

    async def test_queued_running_completed_reports_actual_worker_output(self):
        result = await self.exercise([
            (202, record("queued")),
            (200, record("running")),
            (200, record("completed", worker_id="worker-03", returncode=0, stdout="1\n2\n3\n", stderr="")),
        ])
        self.assertIn("Agent Manager run: completed", result)
        self.assertIn("Run ID: run-42", result)
        self.assertIn("Worker: worker-03", result)
        self.assertIn("Return code: 0", result)
        self.assertIn("1\n2\n3\n", result)
        self.assertIn("not independent validation", result)
        self.assertIn("No Forgejo commit or review is implied", result)
        self.assertEqual([request.method for request in self.requests], ["POST", "GET", "GET"])
        self.assertEqual(self.requests[1].url.path, "/v1/runs/run-42")
        descriptions = [event["data"]["description"] for event in self.events]
        self.assertIn("Submitting", descriptions[0])
        self.assertTrue(any("running" in description for description in descriptions))
        self.assertIn("Completed", descriptions[-1])

    async def test_only_latest_user_text_and_service_attribution_are_forwarded(self):
        body = {"messages": [
            {"role": "system", "content": "never forward system secrets"},
            {"role": "user", "content": "old user instruction"},
            {"role": "assistant", "content": "assistant history"},
            {"role": "user", "content": [
                {"type": "text", "text": "Make a local report."},
                {"type": "image_url", "image_url": {"url": "private-image"}},
                {"type": "text", "text": "Use deterministic data."},
            ]},
        ], "model": "a-user-model", "metadata": {"worker_id": "worker-01", "operation": "other"}}
        await self.exercise([(202, record("completed", worker_id="worker-04", returncode=0))], body,
                            user={"id": "webui-id", "name": "Display Name", "email": "not-forwarded", "valves": {"key": "no"}})
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload, {
            "goal": "Make a local report.\nUse deterministic data.",
            "forgejo_repository": "lab/disposable-smoke",
            "base_ref": "main",
            "model": "luna",
            "metadata": {
                "worker_role": "coder", "operation": "create-python-script", "source": "open-webui",
                "requester": {"id": "webui-id", "name": "Display Name"},
            },
        })
        self.assertEqual(self.requests[0].headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(str(self.requests[0].url), "http://agent-manager:8000/v1/runs")
        self.assertNotIn(TOKEN, self.requests[0].content.decode())

    async def test_queued_running_failed_preserves_stderr_and_error(self):
        failed = record("failed", worker_id="worker-02", returncode=1,
                        stdout="partial report", stderr="ValueError: invalid row", error="worker execution failed")
        failed["error"] = "Manager recorded worker failure"
        result = await self.exercise([(202, record("queued")), (200, record("running")), (200, failed)])
        for expected in ("run: failed", "worker-02", "Return code: 1", "partial report",
                         "ValueError: invalid row", "worker execution failed", "Manager recorded worker failure"):
            self.assertIn(expected, result)
        self.assertIn("Failed", self.events[-1]["data"]["description"])

    async def test_worker_timeout_is_failure_not_pipe_poll_timeout(self):
        result = await self.exercise([
            (202, record("running")),
            (200, record("failed", worker_id="worker-01", returncode=None, error="script exceeded 300 seconds")),
        ])
        self.assertIn("run: failed", result)
        self.assertIn("script exceeded 300 seconds", result)
        self.assertNotIn("Polling time limit", result)

    async def test_no_worker_is_explicit_rejection_and_never_polled(self):
        result = await self.exercise([(503, {"detail": {"code": "NO_WORKER_AVAILABLE", "message": "raw-server-text"}})])
        self.assertIn("Not submitted: NO_WORKER_AVAILABLE (503)", result)
        self.assertIn("ALLOW_GENERATED_CODE=1", result)
        self.assertNotIn("raw-server-text", result)
        self.assertEqual(len(self.requests), 1)

    async def test_submission_timeout_never_reposts_or_claims_rejection(self):
        result = await self.exercise([httpx.ReadTimeout(f"raw auth secret {TOKEN}")])
        self.assertIn("Submission outcome is unknown", result)
        self.assertIn("Do not resend", result)
        self.assertIn("submission time", result)
        self.assertNotIn("raw auth", result)
        self.assertNotIn("Not submitted", result)
        self.assertEqual(len(self.requests), 1)

    async def test_ambiguous_http_or_malformed_submission_requires_operator(self):
        for response in ((503, {"detail": "run could not be queued"}), (502, b"gateway error"),
                         (307, b"redirect"), (202, b"not-json"), (202, {"status": "queued"}), (202, [])):
            with self.subTest(response=response):
                self.requests = []
                result = await self.exercise([response])
                self.assertIn("Submission outcome is unknown", result)
                self.assertIn("No automatic retry", result)
                self.assertEqual(len(self.requests), 1)

    async def test_auth_rejection_does_not_expose_response_body(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.requests = []
                result = await self.exercise([(status, {"detail": f"raw auth error {TOKEN}"})])
                self.assertIn("Not submitted", result)
                self.assertIn("administrator", result)
                self.assertNotIn("raw auth error", result)

    async def test_validation_rejection_is_not_a_completion(self):
        result = await self.exercise([(422, {"detail": [{"input": "private input"}]})])
        self.assertIn("Not submitted", result)
        self.assertIn("HTTP 422", result)
        self.assertNotIn("private input", result)

    async def test_poll_timeout_keeps_run_id_and_does_not_cancel(self):
        result = await self.exercise([(202, record("queued"))] + [(200, record("running"))] * 10)
        self.assertIn("Polling time limit reached", result)
        self.assertIn("run-42", result)
        self.assertIn("not cancelled", result)
        self.assertIn("GET /v1/runs/run-42", result)
        self.assertNotIn("run: completed", result)
        self.assertEqual(self.now, 5)
        self.assertTrue(all(request.method == "GET" for request in self.requests[1:]))

    async def test_poll_connection_failure_keeps_recovery_information(self):
        result = await self.exercise([(202, record("queued")), httpx.ConnectError("private network diagnostics")])
        self.assertIn("run-42", result)
        self.assertIn("not cancelled", result)
        self.assertIn("Do not resubmit", result)
        self.assertNotIn("private network diagnostics", result)

    async def test_poll_rejection_or_bad_record_never_reposts(self):
        for response in ((401, {"detail": f"raw auth {TOKEN}"}), (403, b"raw auth"),
                         (404, b"no run"), (200, b"not-json"),
                         (200, {"id": "another-run", "status": "completed"}),
                         (200, {"id": "run-42", "status": "unexpected"})):
            with self.subTest(response=response):
                self.requests = []
                result = await self.exercise([(202, record("queued")), response])
                self.assertIn("Run ID: run-42", result)
                self.assertIn("not cancelled", result)
                self.assertNotIn("raw auth", result)
                self.assertEqual(len(self.requests), 2)

    async def test_completed_without_output_does_not_invent_worker_or_success(self):
        result = await self.exercise([(202, {"id": "run-42", "status": "completed", "metadata": None})])
        self.assertIn("Worker: not reported", result)
        self.assertIn("Return code: not reported", result)
        self.assertIn("not independent validation", result)
        self.assertNotIn("successful", result)

    async def test_inconsistent_completion_is_not_called_successful(self):
        result = await self.exercise([(202, record("completed", worker_id="worker-04", returncode=2))])
        self.assertIn("do not treat this as successful execution", result)
        self.assertIn("Failed", self.events[-1]["data"]["description"])

    async def test_output_redacts_service_token_and_contains_markdown_fences(self):
        result = await self.exercise([(202, record("completed", worker_id="worker-03", returncode=0,
                                                  stdout=f"```\n<script>example</script>\n{TOKEN}"))])
        self.assertIn("[redacted]", result)
        self.assertIn("````text\n```", result)

    async def test_missing_admin_configuration_never_submits(self):
        for field in ("AGENT_MANAGER_API_KEY", "FORGEJO_REPOSITORY", "MODEL_NAME"):
            with self.subTest(field=field):
                old = getattr(self.pipe.valves, field)
                setattr(self.pipe.valves, field, "")
                result = await self.exercise([])
                self.assertIn("Not submitted", result)
                self.assertEqual(self.requests, [])
                setattr(self.pipe.valves, field, old)

    async def test_non_api_origin_never_sends_credentials(self):
        for url in ("file:///tmp/socket", "http://user:pass@agent-manager:8000", "https://example/dashboard", "http://host?token=value"):
            with self.subTest(url=url):
                self.pipe.valves.AGENT_MANAGER_URL = url
                result = await self.exercise([])
                self.assertIn("direct Agent Manager API origin", result)
                self.assertEqual(self.requests, [])

    async def test_missing_latest_user_text_never_repeats_previous_goal(self):
        for latest in ("", "  ", [{"type": "image_url", "image_url": {"url": "private"}}], None):
            with self.subTest(latest=latest):
                result = await self.exercise([], {"messages": [
                    {"role": "user", "content": "old task must not run twice"},
                    {"role": "user", "content": latest},
                ]})
                self.assertIn("latest user message", result)
                self.assertEqual(self.requests, [])

    async def test_background_chat_tasks_cannot_submit_runs(self):
        for task in ("title_generation", "tags_generation", "follow_up_generation"):
            with self.subTest(task=task):
                result = await self.exercise([], task=task)
                self.assertIn("only for direct chat messages", result)
                self.assertEqual(self.requests, [])

    async def test_disconnected_event_emitter_does_not_change_run_result(self):
        async def unavailable(_event):
            raise RuntimeError("UI disconnected")

        result = await self.exercise([(202, record("completed", worker_id="worker-01", returncode=0))], emitter=unavailable)
        self.assertIn("run: completed", result)
        self.assertEqual(len(self.requests), 1)

    async def test_interrupted_poll_finishes_spinner_without_cancel_request(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.exercise([(202, record("queued")), asyncio.CancelledError()])
        self.assertTrue(self.events[-1]["data"]["done"])
        self.assertIn("run-42", self.events[-1]["data"]["description"])
        self.assertIn("not cancelled", self.events[-1]["data"]["description"])
        self.assertEqual([request.method for request in self.requests], ["POST", "GET"])

    def test_polling_valves_are_bounded(self):
        for values in ({"POLL_INTERVAL_SECONDS": 0}, {"POLL_INTERVAL_SECONDS": 31},
                       {"MAX_WAIT_SECONDS": 0}, {"MAX_WAIT_SECONDS": 1801},
                       {"MAX_WAIT_SECONDS": float("inf")}, {"POLL_INTERVAL_SECONDS": float("nan")}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.pipe.Valves(**values)


if __name__ == "__main__":
    unittest.main()
