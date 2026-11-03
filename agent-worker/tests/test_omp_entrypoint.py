from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "omp_entrypoint_under_test", Path(__file__).parents[1] / "omp-image" / "entrypoint.py"
)
assert SPEC and SPEC.loader
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


def mission(count: int = 1) -> dict:
    return {
        "mission_id": "mission-1",
        "goal": "Create a small calculator app",
        "crew": {"size": {"planners": 1, "coders": count, "reviewers": 1}},
        "crew_plan": {"planners": 1, "coders": count, "reviewers": 1},
    }


class MissionInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch.dict(entrypoint.os.environ, {"OMP_EXPECTED_MISSION_ID": "mission-1"})
        )

    def test_startup_failures_keep_the_worker_supplied_identity(self) -> None:
        cases = (
            (None, "could not read", "FileNotFoundError"),
            (b"{", "could not read", "JSONDecodeError"),
            (b"\xff", "could not read", "UnicodeDecodeError"),
            (b"[]", "one JSON object", ""),
            (b"{}", "missing mission_id", ""),
            (json.dumps({**mission(), "mission_id": "another-mission"}).encode(),
             "mismatched mission_id", ""),
        )
        for contents, summary, error in cases:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                mission_path = Path(directory) / "mission.json"
                if contents is not None:
                    mission_path.write_bytes(contents)
                output = io.StringIO()
                with (
                    patch.object(entrypoint, "MISSION_PATH", mission_path),
                    patch.object(entrypoint, "run_omp") as run_omp,
                    contextlib.redirect_stdout(output),
                    self.assertRaises(SystemExit) as raised,
                ):
                    entrypoint.main()
                result = json.loads(output.getvalue())
                self.assertEqual(raised.exception.code, 1)
                self.assertEqual(result["mission_id"], "mission-1")
                self.assertEqual(result["status"], "failed")
                self.assertIn(summary, result["summary"])
                self.assertIn(error, result.get("error", ""))
                self.assertIsNone(result["reviewer_count"])
                self.assertEqual(result["artifacts"], [])
                run_omp.assert_not_called()

    def test_missing_expected_identity_does_not_trust_the_mission_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mission_path = Path(directory) / "mission.json"
            mission_path.write_text(json.dumps(mission()), encoding="utf-8")
            output = io.StringIO()
            with (
                patch.dict(entrypoint.os.environ, {"OMP_EXPECTED_MISSION_ID": ""}),
                patch.object(entrypoint, "MISSION_PATH", mission_path),
                patch.object(entrypoint, "run_omp") as run_omp,
                contextlib.redirect_stdout(output),
                self.assertRaises(SystemExit),
            ):
                entrypoint.main()
            result = json.loads(output.getvalue())
            self.assertEqual(result["mission_id"], "unknown")
            self.assertEqual(result["status"], "failed")
            self.assertIn("expected mission identity", result["summary"])
            run_omp.assert_not_called()

    def test_accepts_a_valid_sized_crew(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mission_path = Path(directory) / "mission.json"
            mission_path.write_text(json.dumps(mission(5)), encoding="utf-8")
            with patch.object(entrypoint, "MISSION_PATH", mission_path):
                self.assertEqual(entrypoint.load_mission(), mission(5))


    def test_rejects_a_mission_without_an_explicit_crew_plan(self) -> None:
        value = mission()
        del value["crew_plan"]
        with tempfile.TemporaryDirectory() as directory:
            mission_path = Path(directory) / "mission.json"
            mission_path.write_text(json.dumps(value), encoding="utf-8")
            with (
                patch.object(entrypoint, "MISSION_PATH", mission_path),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                entrypoint.load_mission()

    def test_rejects_a_crew_count_outside_the_supported_range(self) -> None:
        for count in (0, 6):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                mission_path = Path(directory) / "mission.json"
                mission_path.write_text(json.dumps(mission(count)), encoding="utf-8")
                with (
                    patch.object(entrypoint, "MISSION_PATH", mission_path),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    entrypoint.load_mission()


class ModelConfigTests(unittest.TestCase):
    def test_renders_model_gateway_in_the_omp_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "models.yml"
            template.write_text(
                "providers:\n  worker-litellm:\n    baseUrl: __MODEL_BASE_URL__\n",
                encoding="utf-8",
            )
            with (
                patch.object(entrypoint, "OMP_MODELS", template),
                patch.dict(
                    "os.environ",
                    {
                        "OMP_JOB_HOME": str(root / "home"),
                        "MODEL_BASE_URL": "https://gateway.example/litellm/v1",
                    },
                ),
            ):
                home = entrypoint.prepare_omp_home()
            config = (home / ".omp" / "agent" / "models.yml").read_text(encoding="utf-8")
            self.assertIn(
                'baseUrl: "https://gateway.example/litellm/v1"', config
            )
            self.assertNotIn("__MODEL_BASE_URL__", config)


class MissionResultTests(unittest.TestCase):
    def test_extracts_result_json_from_omp_text(self) -> None:
        expected = {
            "mission_id": "mission-1",
            "status": "completed",
            "reviewer_approved": True,
            "reviewer_count": 0,
            "summary": "Reviewer approved the calculator.",
            "artifacts": ["index.html"],
        }
        text = "OMP completed the task.\n" + json.dumps(expected) + "\n"
        self.assertEqual(entrypoint.extract_result(text), expected)

    def test_needs_input_result_requests_clarification_after_retry_limit(self) -> None:
        result = {
            "mission_id": "mission-1",
            "status": "needs_input",
            "reviewer_approved": False,
            "reviewer_count": 10,
            "summary": "The reviewer still rejects one requirement.",
            "question": "Should the result prioritize speed or accuracy?",
            "artifacts": [],
        }

        self.assertEqual(
            entrypoint.extract_result("Additional notes.\n" + json.dumps(result)),
            result,
        )
        normalized = entrypoint.normalize_result(result.copy(), "mission-1")
        self.assertEqual(normalized["status"], "needs_input")
        self.assertEqual(normalized["reviewer_count"], 10)
        self.assertEqual(normalized["question"], result["question"])
        self.assertEqual(normalized["artifacts"], [])

    def test_needs_input_requires_limit_and_question(self) -> None:
        for overrides in (
            {"reviewer_count": 9},
            {"question": ""},
        ):
            result = {
                "status": "needs_input",
                "reviewer_approved": False,
                "reviewer_count": 10,
                "summary": "Clarification needed.",
                "question": "Choose an output format.",
                "artifacts": [],
                **overrides,
            }
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                RuntimeError, "ten reviewer rejections"
            ):
                entrypoint.normalize_result(result, "mission-1")


    def test_approved_result_returns_validated_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            (work_dir / "index.html").write_text("calculator", encoding="utf-8")
            with patch.object(entrypoint, "WORK_DIR", work_dir):
                result = entrypoint.normalize_result(
                    {
                        "status": "accepted",
                        "reviewer_approved": True,
                        "reviewer_count": 2,
                        "summary": "Reviewer approved the calculator.",
                        "artifacts": ["index.html"],
                        "script": "obsolete source blob",
                        "steps": ["coder implemented", 3, "reviewer approved"],
                    },
                    "mission-1",
                )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["mission_id"], "mission-1")
        self.assertEqual(result["bounce_to"], "none")
        self.assertEqual(result["artifacts"], ["index.html"])
        self.assertNotIn("script", result)
        self.assertEqual(result["reviewer_count"], 2)
        self.assertEqual(result["steps"], ["coder implemented", "reviewer approved"])

    def test_success_requires_explicit_reviewer_approval(self) -> None:
        result = entrypoint.normalize_result(
            {
                "status": "completed",
                "reviewer_approved": False,
                "summary": "Coder finished the product.",
                "script": "unreviewed product",
                "artifacts": ["unreviewed.html"],
                "reviewer_count": 1,
            },
            "mission-1",
        )
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("script", result)
        self.assertEqual(result["artifacts"], [])
        self.assertIn("reviewer approval", result["summary"])
        self.assertEqual(result["reviewer_count"], 1)

    def test_artifact_paths_must_exist_and_be_relative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            (work_dir / "product.txt").write_text("product", encoding="utf-8")
            with patch.object(entrypoint, "WORK_DIR", work_dir):
                for path in ("../outside.txt", "/absolute.txt", "missing.txt"):
                    with self.subTest(path=path), self.assertRaises(RuntimeError):
                        entrypoint.validate_artifact_paths([path])
                self.assertEqual(
                    entrypoint.validate_artifact_paths(["product.txt"]),
                    ["product.txt"],
                )

    def test_reviewer_count_must_be_a_nonnegative_integer(self) -> None:
        for value in (-1, True, "1", None):
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "valid reviewer_count"
            ):
                entrypoint.normalize_result(
                    {
                        "status": "failed",
                        "reviewer_approved": False,
                        "reviewer_count": value,
                        "summary": "Reviewer requested changes.",
                    },
                    "mission-1",
                )


    def test_success_requires_a_product_manifest(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "at least one artifact path"):
            entrypoint.normalize_result(
                {
                    "status": "completed",
                    "reviewer_approved": True,
                    "summary": "Reviewer approved.",
                    "reviewer_count": 0,
                    "artifacts": [],
                },
                "mission-1",
            )
    def test_rejects_a_result_for_another_mission(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "mission_id did not match"):
            entrypoint.normalize_result(
                {
                    "mission_id": "mission-2",
                    "status": "completed",
                    "reviewer_approved": True,
                    "reviewer_count": 0,
                    "summary": "Reviewer approved.",
                    "artifacts": ["product.html"],
                },
                "mission-1",
            )

    def test_wrapper_failure_marks_reviewer_count_unknown(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            entrypoint.failure("mission-1", "Worker OMP failed.", "no valid final response")
        self.assertEqual(raised.exception.code, 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["reviewer_approved"])
        self.assertIsNone(result["reviewer_count"])


class OmpProgressTests(unittest.TestCase):
    def test_task_progress_reports_roles_and_reviewer_verdicts_without_raw_text(self) -> None:
        state: dict[str, Any] = {}
        prompt = "private mission text"
        coder_start = entrypoint._progress_from_omp_event(
            {
                "type": "tool_execution_start",
                "toolName": "task",
                "toolCallId": "coder-call",
                "args": {"agent": "coder", "task": prompt},
            },
            state,
        )
        reviewer_start = entrypoint._progress_from_omp_event(
            {
                "type": "tool_execution_start",
                "toolName": "task",
                "toolCallId": "reviewer-call",
                "args": {
                    "tasks": [{"agent": "reviewer", "task": prompt}]
                },
            },
            state,
        )
        reviewer_result = entrypoint._progress_from_omp_event(
            {
                "type": "tool_execution_end",
                "toolName": "task",
                "toolCallId": "reviewer-call",
                "result": {
                    "details": {
                        "results": [
                            {"output": "changes_requested: missing divide"}
                        ]
                    }
                },
            },
            state,
        )
        updates = coder_start + reviewer_start + reviewer_result
        self.assertEqual(coder_start[0]["phase"], "coders_started")
        self.assertEqual(reviewer_start[0]["phase"], "reviewers_started")
        self.assertEqual(reviewer_result[0]["phase"], "reviewers_rejected")
        self.assertEqual(reviewer_result[0]["rejections_seen"], 1)
        rendered = json.dumps(updates)
        self.assertNotIn(prompt, rendered)
        self.assertNotIn("missing divide", rendered)

    def test_extracts_final_assistant_text_from_json_event(self) -> None:
        final = '{"status":"completed","reviewer_approved":true}'
        event = {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": final}],
            },
        }
        self.assertEqual(entrypoint._assistant_text_from_event(event), final)

    def test_omp_json_error_event_preserves_connection_detail(self) -> None:
        event = {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "error",
                "reason": "error",
                "error": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "could not resolve LiteLLM host",
                },
            },
        }
        self.assertEqual(
            entrypoint._provider_error_from_event(event)["message"],
            "could not resolve LiteLLM host",
        )


class ProviderDiagnosticsTests(unittest.TestCase):
    def run_events(self, events: list[dict], return_code: int = 0, stderr: str = "") -> dict:
        """Exercise stream parsing through the completion JSON consumed by worker.py."""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as stream:
            root = Path(directory)
            (root / "index.html").write_text("reviewed product", encoding="utf-8")
            stream.write("".join(json.dumps(event) + "\n" for event in events).encode())
            stream.seek(0)
            process = Mock(stdout=stream)
            process.wait.return_value = return_code
            selector = Mock()
            selector.select.return_value = [(SimpleNamespace(fileobj=stream), 1)]

            def popen(*args, **kwargs):
                kwargs["stderr"].write(stderr.encode())
                return process

            output = io.StringIO()
            with (
                patch.object(entrypoint, "WORK_DIR", root),
                patch.object(entrypoint, "load_mission", return_value=mission()),
                patch.object(entrypoint, "prepare_project_agents"),
                patch.object(entrypoint, "prepare_omp_home", return_value=root),
                patch.object(entrypoint.subprocess, "Popen", side_effect=popen) as launch,
                patch.object(entrypoint.selectors, "DefaultSelector", return_value=selector),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                entrypoint.main()
            launch.assert_called_once()
            return json.loads(output.getvalue())

    @staticmethod
    def final_event(**overrides) -> dict:
        result = {
            "status": "failed",
            "reviewer_count": 0,
            "reviewer_approved": False,
            "summary": "The coder could not create the requested example.",
            "artifacts": [],
            **overrides,
        }
        return {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": json.dumps(result)}]},
        }

    @staticmethod
    def error_event(text: str, **metadata) -> dict:
        return {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "error",
                "reason": "error",
                "error": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": text,
                    "provider": "worker-litellm",
                    "model": "luna",
                    **metadata,
                },
            },
        }

    def test_actual_stream_error_reaches_consumer_without_raw_generation_or_credentials(self) -> None:
        secret = "session-value-not-an-api-key-shape"
        event = self.error_event(
            "Provider finish_reason: content_filter; code=content_filter; request_id=req-123\n"
            f"Authorization: Bearer {secret}\napi_key='unconfigured-secret'\n"
            "https://user:pass@example.invalid/path?token=private-query\n"
            "request_body={private rejected prompt}",
            errorStatus=400,
            errorId=123,
            responseId="resp-456",
            content=[{"type": "toolCall", "arguments": {"content": "private rejected generation"}}],
        )
        with patch.dict(entrypoint.os.environ, {"MODEL_API_KEY": secret}):
            result = self.run_events([event])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["artifacts"], [])
        self.assertIn("content filter", result["summary"])
        self.assertIn("req-123", result["error"])
        diagnostic = result["provider_error"]
        self.assertEqual(diagnostic["category"], "content_filter")
        self.assertEqual(diagnostic["source"], "message_update.assistantMessageEvent.error")
        self.assertEqual(diagnostic["code"], "content_filter")
        self.assertEqual(diagnostic["request_id"], "req-123")
        self.assertEqual(diagnostic["response_id"], "resp-456")
        self.assertEqual(diagnostic["http_status"], 400)
        self.assertEqual(diagnostic["omp_error_id"], 123)
        rendered = json.dumps(result)
        for private in (
            secret, "unconfigured-secret", "user:pass", "private-query",
            "private rejected prompt", "private rejected generation",
        ):
            self.assertNotIn(private, rendered)
        self.assertIn("[REDACTED CREDENTIAL]", rendered)

    def test_valid_planner_failure_keeps_child_error_and_useful_summary(self) -> None:
        child = {
            "agent": "coder", "id": "Example", "exitCode": 1,
            "error": "[worker-litellm/luna] Provider finish_reason: content_filter",
            "output": "partial tool arguments must not be returned",
        }
        details = {"results": [child]}
        carriers = [
            {"type": "tool_execution_end", "toolName": "task", "result": {"details": details}},
            {
                "type": "message_end",
                "message": {"role": "toolResult", "toolName": "task", "details": details},
            },
        ]
        for carrier in carriers:
            with self.subTest(carrier=carrier["type"]):
                result = self.run_events([carrier, self.final_event(error="Planner saw no saved file.")])
                self.assertEqual(result["summary"], "The coder could not create the requested example.")
                self.assertEqual(result["provider_error"]["category"], "content_filter")
                self.assertEqual(result["provider_error"]["agent"], "coder")
                self.assertIn("content_filter", result["error"])
                self.assertIn("Planner saw no saved file", result["error"])
                self.assertNotIn("code", result["provider_error"])
                self.assertNotIn("request_id", result["provider_error"])
                self.assertNotIn("partial tool arguments", json.dumps(result))

    def test_child_stderr_is_error_evidence_not_proof_of_provider_filtering(self) -> None:
        result = self.run_events([
            {
                "type": "tool_execution_end", "toolName": "task",
                "result": {"details": {"results": [{
                    "exitCode": 1, "stderr": "Test failed while documenting content filters.",
                }]}},
            },
            self.final_event(),
        ])
        self.assertEqual(result["provider_error"]["category"], "task_error")
        self.assertIn("Test failed", result["error"])

    def test_normal_prose_tool_output_and_planner_claims_are_not_provider_evidence(self) -> None:
        result = self.run_events([
            {
                "type": "tool_execution_end", "toolName": "task",
                "result": {"details": {"results": [{
                    "exitCode": 1, "output": "Provider finish_reason: content_filter",
                }]}},
            },
            self.final_event(
                summary="The documentation mentions content_filter and content filters.",
                provider_error={"code": "invented", "request_id": "invented"},
            ),
        ])
        self.assertNotIn("provider_error", result)
        self.assertNotIn("error", result)
        self.assertIn("documentation", result["summary"])
        for event in (
            {"type": "message_end", "message": {"role": "user", "errorMessage": "content_filter"}},
            {"type": "tool_execution_start", "args": {"error": "content_filter"}},
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "content_filter"}},
        ):
            self.assertIsNone(entrypoint._provider_error_from_event(event))

    def test_failure_exit_and_invalid_final_json_preserve_observed_error(self) -> None:
        event = self.error_event("Connection failed; request_id=req-connection", errorStatus=503)
        for return_code, final in ((2, self.final_event()), (0, self.final_event(reviewer_count=None))):
            with self.subTest(return_code=return_code):
                result = self.run_events([event, final], return_code, "non-provider wrapper stderr")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["provider_error"]["category"], "provider_error")
                self.assertEqual(result["provider_error"]["request_id"], "req-connection")
                self.assertIn("Connection failed", result["error"])
                self.assertEqual(result["artifacts"], [])

    def test_success_is_unaffected_by_earlier_errors_or_filter_mentions(self) -> None:
        result = self.run_events([
            self.error_event("Provider finish_reason: content_filter"),
            self.final_event(
                status="completed", reviewer_approved=True, artifacts=["index.html"],
                summary="Reviewer approved documentation about content filters.",
            ),
        ])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["artifacts"], ["index.html"])
        self.assertNotIn("provider_error", result)
        self.assertNotIn("error", result)

    def test_terminal_message_errors_keep_metadata_without_classifying_filter_mentions(self) -> None:
        message = {
            "role": "assistant", "stopReason": "error",
            "errorMessage": "Connection failed while loading documentation about content_filter.",
            "errorId": 42, "responseId": "response-only",
        }
        for event in (
            {"type": "message_end", "message": message},
            {"type": "turn_end", "message": message},
            {"type": "agent_end", "messages": [message]},
        ):
            with self.subTest(event=event["type"]):
                result = self.run_events([event, self.final_event()])
                diagnostic = result["provider_error"]
                self.assertEqual(diagnostic["category"], "provider_error")
                self.assertEqual(diagnostic["omp_error_id"], 42)
                self.assertEqual(diagnostic["response_id"], "response-only")
                self.assertNotIn("code", diagnostic)
                self.assertNotIn("request_id", diagnostic)

    def test_stderr_is_redacted_without_treating_it_as_provider_evidence(self) -> None:
        result = self.run_events(
            [], return_code=1,
            stderr="Wrapper connection failed\nAuthorization: Bearer private-value\n",
        )
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("provider_error", result)
        self.assertNotIn("private-value", result["error"])
        self.assertIn("Wrapper connection failed", result["error"])
        partial = self.run_events([], return_code=1, stderr="secret-prefix-" + "s" * 1500)
        self.assertNotIn("s" * 20, partial["error"])

    def test_diagnostics_are_bounded_and_redacted_before_truncation(self) -> None:
        secret = "s" * 1500
        with patch.dict(entrypoint.os.environ, {"MODEL_API_KEY": secret}):
            result = self.run_events([
                self.error_event("Connection failed: " + secret + "\x00\n" + "x" * 20000),
                self.final_event(),
            ])
        self.assertLessEqual(len(result["error"]), 1200)
        self.assertLessEqual(len(result["provider_error"]["message"]), 600)
        self.assertNotIn("s" * 20, json.dumps(result))
        self.assertNotIn("\x00", result["provider_error"]["message"])
        self.assertIn("[REDACTED CREDENTIAL]", result["error"])

if __name__ == "__main__":
    unittest.main()
