from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CREW = Path(__file__).resolve().parents[1]
OMP = CREW / "omp.py"


def run_mission(goal: str, coders: int, reviewers: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="crew-test-") as directory:
        root = Path(directory)
        work = root / "work"
        mission = root / "mission.json"
        mission.write_text(
            json.dumps({
                "mission_id": "test-mission",
                "goal": goal,
                "repo": "test/repo",
                "planners": 1,
                "coders": coders,
                "reviewers": reviewers,
            }) + "\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update({"MISSION_PATH": str(mission), "WORK_DIR": str(work)})
        completed = subprocess.run(
            [sys.executable, str(OMP)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=15,
        )
        lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        if not lines:
            raise AssertionError(f"crew OMP returned no JSON: {completed.stderr}")
        return json.loads(lines[-1])


class CrewPipelineTests(unittest.TestCase):
    def test_configured_crew_runs_and_supervisor_returns_evidence(self) -> None:
        result = run_mission("Create a hello text artifact", coders=3, reviewers=2)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(result["evidence"]["coder_artifacts"]), 3)
        self.assertEqual(len(result["evidence"]["reviewers"]), 2)
        self.assertTrue(all(item["decision"] == "approved" for item in result["evidence"]["reviewers"]))
        self.assertEqual(len(result["evidence"]["supervisor_checks"]), 3)

    def test_unsupported_goal_retries_then_returns_rejection(self) -> None:
        result = run_mission("Build a calculator with buttons", coders=1, reviewers=1)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["attempts"], 3)
        self.assertIn("hello-text demo missions", result["evidence"].get("findings", ""))

    def test_worker_image_rejects_out_of_range_crew_size(self) -> None:
        result = run_mission("Create a hello text artifact", coders=4, reviewers=1)

        self.assertEqual(result["status"], "blocked")
        self.assertIn("limits", result["summary"])


if __name__ == "__main__":
    unittest.main()
