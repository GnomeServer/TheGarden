"""Crew OMP. Starts one planner and prints the one result JSON."""

import json
import os
import subprocess
import sys
from pathlib import Path

MISSION_PATH = Path(os.environ.get("MISSION_PATH", "/mission.json"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "/work"))
PLANNER = Path(__file__).with_name("planner.py")


def fail(summary: str) -> None:
    mission_id = "unknown"
    if MISSION_PATH.is_file():
        try:
            loaded = json.loads(MISSION_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("mission_id"), str):
                mission_id = loaded["mission_id"]
        except json.JSONDecodeError:
            pass
    print(json.dumps({
        "mission_id": mission_id,
        "status": "blocked",
        "summary": summary,
        "bounce_to": "none",
    }))
    raise SystemExit(1)


def main() -> None:
    if not MISSION_PATH.is_file():
        fail("mission file is missing")
    try:
        mission = json.loads(MISSION_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        fail("mission file is not JSON")
    required = ("mission_id", "goal", "repo", "planners", "coders", "reviewers")
    if not isinstance(mission, dict) or any(key not in mission for key in required):
        fail("mission is missing a required crew-plan field")
    if (
        type(mission["planners"]) is not int
        or mission["planners"] != 1
        or type(mission["coders"]) is not int
        or not 1 <= mission["coders"] <= 3
        or type(mission["reviewers"]) is not int
        or not 1 <= mission["reviewers"] <= 2
    ):
        fail("the crew plan exceeds this worker image's limits")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    (WORK_DIR / "mission.json").write_text(
        json.dumps(mission) + "\n", encoding="utf-8"
    )
    completed = subprocess.run(
        [sys.executable, str(PLANNER), str(WORK_DIR)],
        check=False,
    )
    result_path = WORK_DIR / "result.json"
    if completed.returncode != 0 or not result_path.is_file():
        fail("planner did not produce a result")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        fail("planner result is not a JSON object")
    print(json.dumps(result))

if __name__ == "__main__":
    main()
