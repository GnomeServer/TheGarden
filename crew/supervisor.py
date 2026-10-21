"""Supervisor. Independently enforces the demo acceptance contract."""

import json
import sys
from pathlib import Path


def main() -> None:
    work = Path(sys.argv[1])
    mission = json.loads((work / "mission.json").read_text(encoding="utf-8"))
    coder_count = mission["coders"]
    reviewer_count = mission["reviewers"]
    candidates = [f"candidate-{index}.txt" for index in range(1, coder_count + 1)]
    reviews = []
    findings = []

    if "hello" not in mission["goal"].casefold() or any(
        term in mission["goal"].casefold() for term in ("calculator", "tkinter")
    ):
        findings.append("This worker image only supports hello-text demo missions.")

    missing = [name for name in candidates if not (work / name).is_file()]
    invalid = [
        name for name in candidates
        if (work / name).is_file()
        and not (work / name).read_text(encoding="utf-8").startswith("hello")
    ]
    if missing:
        findings.append("Missing coder artifacts: " + ", ".join(missing))
    if invalid:
        findings.append("Invalid coder artifacts: " + ", ".join(invalid))

    for reviewer_id in range(1, reviewer_count + 1):
        review_path = work / f"review-{reviewer_id}.json"
        if not review_path.is_file():
            findings.append(f"Reviewer {reviewer_id} produced no verdict.")
            continue
        review = json.loads(review_path.read_text(encoding="utf-8"))
        decision = review.get("decision")
        reviews.append({"reviewer_id": reviewer_id, "decision": decision})
        if decision != "approved":
            findings.append(f"Reviewer {reviewer_id} requested changes.")
        if review.get("candidate_files") != candidates:
            findings.append(f"Reviewer {reviewer_id} did not inspect the full coder set.")

    result = {
        "decision": "approved" if not findings else "rejected",
        "findings": "; ".join(findings),
        "evidence": {
            "coder_artifacts": candidates,
            "reviewers": reviews,
            "supervisor_checks": [
                "all expected coder artifacts exist",
                "all coder artifacts satisfy the demo contract",
                "every reviewer inspected every coder artifact",
            ],
            "findings": "; ".join(findings),
        },
    }
    (work / "supervisor.json").write_text(
        json.dumps(result) + "\n", encoding="utf-8"
    )
    # A rejected mission is a valid verdict; the planner owns retry policy.
    return


if __name__ == "__main__":
    main()
