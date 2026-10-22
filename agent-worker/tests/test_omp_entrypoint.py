from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    }


class MissionInputTests(unittest.TestCase):
    def test_accepts_a_valid_sized_crew(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mission_path = Path(directory) / "mission.json"
            mission_path.write_text(json.dumps(mission(5)), encoding="utf-8")
            with patch.object(entrypoint, "MISSION_PATH", mission_path):
                self.assertEqual(entrypoint.load_mission(), mission(5))

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
                "reason": "could not resolve LiteLLM host",
            },
        }
        self.assertEqual(
            entrypoint._assistant_error_from_event(event),
            "could not resolve LiteLLM host",
        )

if __name__ == "__main__":
    unittest.main()
