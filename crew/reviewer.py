"""Reviewer demo. Independently checks every coder candidate without editing it."""

import json
import sys
from pathlib import Path


def main() -> None:
    work = Path(sys.argv[1])
    reviewer_id = int(sys.argv[2])
    mission = json.loads((work / "mission.json").read_text(encoding="utf-8"))
    goal = mission.get("goal") if isinstance(mission.get("goal"), str) else ""
    expected = [
        f"candidate-{index}.txt"
        for index in range(1, int(mission["coders"]) + 1)
    ]
    missing = [name for name in expected if not (work / name).is_file()]
    invalid = [
        name
        for name in expected
        if (work / name).is_file()
        and not (work / name).read_text(encoding="utf-8").startswith("hello")
    ]
    supported = "hello" in goal.casefold() and not any(
        term in goal.casefold() for term in ("calculator", "tkinter")
    )
    findings = []
    if not supported:
        findings.append("This demo skill only produces hello-text artifacts.")
    if missing:
        findings.append("Missing coder artifacts: " + ", ".join(missing))
    if invalid:
        findings.append("Invalid coder artifacts: " + ", ".join(invalid))
    decision = {
        "reviewer_id": reviewer_id,
        "decision": "approved" if not findings else "changes_requested",
        "candidate_files": expected,
        "findings": "; ".join(findings),
    }
    (work / f"review-{reviewer_id}.json").write_text(
        json.dumps(decision) + "\n", encoding="utf-8"
    )
    raise SystemExit(0 if not findings else 1)


if __name__ == "__main__":
    main()
