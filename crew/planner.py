"""Planner. Runs the configured coder/reviewer crew and sends the verdict to OMP."""

import json
import subprocess
import sys
from pathlib import Path

MAX_ATTEMPTS = 3
CODER = Path(__file__).with_name("coder.py")
REVIEWER = Path(__file__).with_name("reviewer.py")
SUPERVISOR = Path(__file__).with_name("supervisor.py")


def write_result(
    work: Path,
    mission_id: str,
    status: str,
    summary: str,
    bounce_to: str,
    attempts: int,
    evidence: dict,
) -> None:
    (work / "result.json").write_text(
        json.dumps({
            "mission_id": mission_id,
            "status": status,
            "summary": summary,
            "bounce_to": bounce_to,
            "attempts": attempts,
            "evidence": evidence,
            "steps": [
                "Planner selected the configured coder and reviewer counts.",
                f"Planner completed {attempts} attempt(s).",
                "Supervisor checked the reviewer verdicts and coder artifacts.",
            ],
        }) + "\n",
        encoding="utf-8",
    )


def run_role(script: Path, work: Path, role_id: int) -> int:
    completed = subprocess.run(
        [sys.executable, str(script), str(work), str(role_id)],
        check=False,
    )
    return completed.returncode


def main() -> None:
    work = Path(sys.argv[1])
    mission = json.loads((work / "mission.json").read_text(encoding="utf-8"))
    mission_id = mission["mission_id"]
    coder_count = mission["coders"]
    reviewer_count = mission["reviewers"]
    last_evidence: dict = {}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        (work / "attempt.txt").write_text(str(attempt), encoding="utf-8")
        for path in (*work.glob("candidate-*.txt"), *work.glob("review-*.json")):
            path.unlink(missing_ok=True)
        (work / "supervisor.json").unlink(missing_ok=True)

        coder_failures = [
            index
            for index in range(1, coder_count + 1)
            if run_role(CODER, work, index) != 0
        ]
        reviewer_failures = [
            index
            for index in range(1, reviewer_count + 1)
            if run_role(REVIEWER, work, index) != 0
        ]
        supervisor_code = run_role(SUPERVISOR, work, 0)
        supervisor_path = work / "supervisor.json"
        if supervisor_code != 0 or not supervisor_path.is_file():
            write_result(
                work, mission_id, "blocked",
                "Supervisor did not produce a verdict.", "omp", attempt, {},
            )
            return

        supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
        last_evidence = supervisor.get("evidence", {})
        last_evidence["coder_failures"] = coder_failures
        last_evidence["reviewer_failures"] = reviewer_failures
        if supervisor.get("decision") == "approved":
            write_result(
                work,
                mission_id,
                "accepted",
                f"Supervisor accepted {coder_count} coder artifact(s) after {attempt} attempt(s).",
                "none",
                attempt,
                last_evidence,
            )
            return
        (work / "changes.txt").write_text(
            str(supervisor.get("findings") or "Supervisor rejected the crew result."),
            encoding="utf-8",
        )

    write_result(
        work,
        mission_id,
        "rejected",
        f"Supervisor rejected the result after {MAX_ATTEMPTS} attempts.",
        "omp",
        MAX_ATTEMPTS,
        last_evidence,
    )


if __name__ == "__main__":
    main()
