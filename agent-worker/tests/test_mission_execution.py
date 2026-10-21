from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mission import CODER_PROMPT, PLANNER_PROMPT, REVIEWER_PROMPT, execute_mission


CALCULATOR_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Calculator</title></head>
<body>
  <main>
    <input id="display" aria-label="Calculator display" readonly>
    <button data-op="add">+</button><button data-op="subtract">−</button>
    <button data-op="multiply">×</button><button data-op="divide">÷</button>
    <button id="equals">=</button><button id="clear">Clear</button>
    <p id="error" role="alert"></p>
  </main>
  <script>
    function calculate(left, right, op) {
      if (op === "add") return left + right;
      if (op === "subtract") return left - right;
      if (op === "multiply") return left * right;
      if (op === "divide") {
        if (right === 0) throw new Error("Cannot divide by zero");
        return left / right;
      }
      throw new Error("Unsupported operation");
    }
  </script>
</body>
</html>
"""


class DynamicMissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_calculator_goal_flows_through_roles_and_returns_generated_file(self) -> None:
        goal = (
            "Could you generate a GUI of a calculator that computes differences, "
            "additions, subtractions, multiplication, and dividing?"
        )
        responses = [
            {
                "summary": "A standalone browser calculator.",
                "acceptance_criteria": [
                    "Provide addition, subtraction, multiplication, and division.",
                    "Show a clear error for division by zero.",
                ],
                "coder_tasks": [{"task": "Implement the calculator interface and operations.", "files": ["calculator.html"]}],
            },
            {"summary": "Created calculator.html.", "files": [{"path": "calculator.html", "content": CALCULATOR_HTML}]},
            {"decision": "approved", "findings": []},
            {"decision": "approved", "findings": []},
        ]
        calls: list[tuple[str, str, int]] = []

        async def model_call(system: str, user: str, max_tokens: int) -> str:
            calls.append((system, user, max_tokens))
            if not responses:
                raise AssertionError("mission called the model more often than the role plan requires")
            return json.dumps(responses.pop(0))

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = await execute_mission(
                {
                    "run_id": "be1dd0ef-8ec1-4d06-a141-77ee224ea67b",
                    "goal": goal,
                    "forgejo_repository": "lab/calculator",
                    "base_ref": "main",
                },
                {
                    "project_name": "calculator",
                    "mission": {"coders": 1, "reviewers": 2},
                },
                model_call=model_call,
                workspace=workspace,
            )

            artifact = result["artifacts"][0]
            saved = workspace / "missions" / "calculator" / "be1dd0ef-8ec1-4d06-a141-77ee224ea67b" / artifact["path"]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["crew_outcome"], "accepted")
            self.assertEqual(artifact["path"], "calculator.html")
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.read_text(encoding="utf-8"), CALCULATOR_HTML)
            self.assertIn("divide by zero", artifact["content"])
            self.assertEqual([call[0] for call in calls], [PLANNER_PROMPT, CODER_PROMPT, REVIEWER_PROMPT, REVIEWER_PROMPT])
            self.assertTrue(all("hello" not in call[1].casefold() for call in calls))

    async def test_reviewer_findings_are_given_to_coder_for_one_bounded_revision(self) -> None:
        responses = [
            {"summary": "Calculator", "acceptance_criteria": ["Handle division by zero."], "coder_tasks": [{"task": "Create calculator.", "files": ["calculator.html"]}]},
            {"files": [{"path": "calculator.html", "content": "first revision"}]},
            {"decision": "changes_requested", "findings": ["Handle division by zero."]},
            {"files": [{"path": "calculator.html", "content": "second revision with division by zero handling"}]},
            {"decision": "approved", "findings": []},
        ]
        coder_inputs: list[dict] = []

        async def model_call(system: str, user: str, max_tokens: int) -> str:
            if system == CODER_PROMPT:
                coder_inputs.append(json.loads(user))
            return json.dumps(responses.pop(0))

        with tempfile.TemporaryDirectory() as directory:
            result = await execute_mission(
                {"run_id": "mission-retry", "goal": "Build a calculator", "forgejo_repository": "lab/calculator"},
                {"project_name": "calculator", "mission": {"coders": 1, "reviewers": 1}},
                model_call=model_call,
                workspace=Path(directory),
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["artifacts"][0]["content"], "second revision with division by zero handling")
        self.assertEqual(coder_inputs[1]["reviewer_findings"], ["Handle division by zero."])


if __name__ == "__main__":
    unittest.main()
