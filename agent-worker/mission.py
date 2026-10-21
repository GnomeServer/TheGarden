from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

ModelCall = Callable[[str, str, int], Awaitable[str]]
MAX_ATTEMPTS = 3
MAX_FILES = 8
MAX_FILE_CHARACTERS = 16000
MAX_TOTAL_CHARACTERS = 24000

PLANNER_PROMPT = """You are the planner for a coding mission. Make reasonable implementation choices from the request; do not ask the user questions. If the request does not specify a platform for a GUI, choose a self-contained browser GUI using HTML, CSS, and JavaScript. Return only JSON with this shape:
{"summary":"short deliverable description","acceptance_criteria":["..."],"coder_tasks":[{"task":"...","files":["relative/path"]}]}
There must be exactly the requested number of coder_tasks. Assign each output file to exactly one coder. Include concrete, observable acceptance criteria. Do not claim to have inspected or modified the repository; only the mission details are available."""

CODER_PROMPT = """You are a coder in a bounded worker crew. Implement your assigned task from the mission and plan. Return only JSON with this shape: {"summary":"...","files":[{"path":"relative/path","content":"complete text file contents"}]}. Produce complete files, not explanations or placeholders. Use only the files assigned to this coder. Do not use absolute paths, parent traversal, shell commands, network calls, secrets, or external dependencies unless the mission explicitly requires them. When the request is a GUI and no platform is specified, create a self-contained HTML/CSS/JavaScript browser interface. Incorporate reviewer findings on later attempts."""

REVIEWER_PROMPT = """You are an independent reviewer. Evaluate the supplied files against the original mission and every acceptance criterion. Do not assume unshown files or claim tests ran. Return only JSON: {"decision":"approved"|"changes_requested","findings":["..."]}. Approve only when the actual file contents satisfy the requirements. Flag missing operations, dead controls, incorrect edge cases, unsafe path names, or incomplete artifacts."""


def _json_object(text: str, role: str) -> dict[str, Any]:
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"{role} returned no JSON object")
    try:
        value = json.loads(content[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{role} returned invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{role} JSON result must be an object")
    return value


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise RuntimeError("coder returned an invalid artifact path")
    normalized = value.strip()
    if any(ord(character) < 32 for character in normalized):
        raise RuntimeError("artifact paths cannot contain control characters")
    if "\\" in normalized:
        raise RuntimeError("artifact paths must use forward slashes")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("artifact path escapes the mission workspace")
    if normalized in {"mission.json", "result.json"}:
        raise RuntimeError("artifact path is reserved by the worker")
    return path.as_posix()


def _normalize_files(payload: dict[str, Any], allowed_paths: set[str]) -> dict[str, str]:
    items = payload.get("files")
    if not isinstance(items, list) or not items or len(items) > MAX_FILES:
        raise RuntimeError("coder must return between 1 and 8 text files")
    files: dict[str, str] = {}
    total = 0
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("coder returned a malformed file entry")
        path = _safe_relative_path(item.get("path"))
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"coder returned empty content for {path}")
        if path not in allowed_paths:
            raise RuntimeError(f"coder wrote unassigned file {path}")
        if path in files:
            raise RuntimeError(f"multiple coders wrote {path}")
        if len(content) > MAX_FILE_CHARACTERS:
            raise RuntimeError(f"{path} exceeds the per-file size limit")
        total += len(content)
        if total > MAX_TOTAL_CHARACTERS:
            raise RuntimeError("mission artifacts exceed the total size limit")
        files[path] = content
    return files


def _validate_plan(payload: dict[str, Any], coder_count: int) -> tuple[str, list[str], list[dict[str, Any]]]:
    summary = payload.get("summary")
    criteria = payload.get("acceptance_criteria")
    tasks = payload.get("coder_tasks")
    if not isinstance(summary, str) or not summary.strip():
        raise RuntimeError("planner returned no deliverable summary")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 12 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 500 for item in criteria
    ):
        raise RuntimeError("planner returned invalid acceptance criteria")
    if not isinstance(tasks, list) or len(tasks) != coder_count:
        raise RuntimeError("planner task count does not match the configured coder count")
    planned_paths: set[str] = set()
    checked_tasks: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise RuntimeError("planner returned a malformed coder task")
        description = task.get("task")
        paths = task.get("files")
        if not isinstance(description, str) or not description.strip() or not isinstance(paths, list) or not paths:
            raise RuntimeError("planner coder tasks must include a task and file list")
        checked = [_safe_relative_path(path) for path in paths]
        if planned_paths.intersection(checked):
            raise RuntimeError("planner assigned the same artifact path to multiple coders")
        planned_paths.update(checked)
        checked_tasks.append({"task": description.strip()[:2000], "files": checked})
    return summary.strip()[:200], [item.strip() for item in criteria], checked_tasks


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:64]
    return slug or "mission"


async def execute_mission(
    event: dict[str, Any],
    metadata: dict[str, Any],
    *,
    model_call: ModelCall,
    workspace: Path,
) -> dict[str, Any]:
    mission_id = event.get("run_id")
    goal = event.get("goal")
    repository = event.get("forgejo_repository")
    plan_meta = metadata.get("mission")
    if not isinstance(mission_id, str) or not mission_id or not isinstance(goal, str) or not goal.strip():
        raise RuntimeError("mission event is missing its run id or goal")
    if not isinstance(repository, str) or not repository.strip():
        raise RuntimeError("mission event is missing its repository identifier")
    if not isinstance(plan_meta, dict):
        raise RuntimeError("mission event is missing its crew plan")
    coders, reviewers = plan_meta.get("coders"), plan_meta.get("reviewers")
    if type(coders) is not int or not 1 <= coders <= 3 or type(reviewers) is not int or not 1 <= reviewers <= 2:
        raise RuntimeError("mission crew plan is outside the worker's supported limits")

    project = metadata.get("project_name")
    mission_dir = workspace / "missions" / _slug(str(project or repository)) / _slug(mission_id)
    mission_dir.mkdir(parents=True, exist_ok=False)
    steps = ["Worker OMP accepted the mission and started dynamic planning."]

    plan_payload = _json_object(
        await model_call(
            PLANNER_PROMPT,
            json.dumps({"goal": goal.strip(), "repository": repository, "base_ref": event.get("base_ref", "main"), "coder_count": coders}),
            1800,
        ),
        "planner",
    )
    summary, criteria, tasks = _validate_plan(plan_payload, coders)
    plan = {"summary": summary, "acceptance_criteria": criteria, "coder_tasks": tasks}
    steps.append(f"Planner produced {len(criteria)} acceptance criteria and {len(tasks)} coder task(s).")

    feedback: list[str] = []
    final_files: dict[str, str] = {}
    final_reviews: list[dict[str, Any]] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        coder_calls = [
            model_call(
                CODER_PROMPT,
                json.dumps({
                    "goal": goal.strip(),
                    "plan": plan,
                    "coder_id": index,
                    "task": task,
                    "reviewer_findings": feedback,
                }),
                4000,
            )
            for index, task in enumerate(tasks, start=1)
        ]
        coder_payloads = await asyncio.gather(*coder_calls)
        files: dict[str, str] = {}
        allowed: set[str] = set()
        for task, output in zip(tasks, coder_payloads):
            task_paths = set(task["files"])
            allowed.update(task_paths)
            payload = _json_object(output, "coder")
            for path, content in _normalize_files(payload, task_paths).items():
                if path in files:
                    raise RuntimeError(f"multiple coders wrote {path}")
                files[path] = content
        missing = allowed.difference(files)
        if missing:
            raise RuntimeError("coders omitted planned files: " + ", ".join(sorted(missing)))
        if len(files) > MAX_FILES or sum(len(content) for content in files.values()) > MAX_TOTAL_CHARACTERS:
            raise RuntimeError("combined coder artifacts exceed the crew output limits")
        steps.append(f"Coder crew produced {len(files)} file(s) on attempt {attempt}.")

        reviewer_input = json.dumps({"goal": goal.strip(), "plan": plan, "files": files})
        reviewer_outputs = await asyncio.gather(*[
            model_call(REVIEWER_PROMPT, reviewer_input, 1200)
            for _ in range(reviewers)
        ])
        reviews = []
        feedback = []
        for index, output in enumerate(reviewer_outputs, start=1):
            review = _json_object(output, "reviewer")
            decision = review.get("decision")
            findings = review.get("findings")
            if decision not in {"approved", "changes_requested"} or not isinstance(findings, list) or any(
                not isinstance(item, str) for item in findings
            ):
                raise RuntimeError(f"reviewer {index} returned an invalid verdict")
            review_result = {"reviewer_id": index, "decision": decision, "findings": [item[:500] for item in findings[:8]]}
            reviews.append(review_result)
            if decision != "approved":
                feedback.extend(review_result["findings"] or [f"Reviewer {index} requested changes."])
        final_files, final_reviews = files, reviews
        steps.append(f"Reviewer crew returned {sum(item['decision'] == 'approved' for item in reviews)}/{reviewers} approvals.")
        if not feedback:
            for relative, content in files.items():
                destination = mission_dir.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("x", encoding="utf-8") as artifact:
                    artifact.write(content)
            artifacts = [{"path": path, "content": content} for path, content in sorted(files.items())]
            evidence = {
                "acceptance_criteria": criteria,
                "artifacts": [item["path"] for item in artifacts],
                "reviewers": reviews,
                "workspace": str(mission_dir),
            }
            steps.append("Supervisor accepted the artifacts and saved them in the worker workspace.")
            return {
                "status": "completed",
                "summary": f"Worker generated {len(artifacts)} artifact(s) after {attempt} attempt(s); the Forgejo repository was not modified.",
                "steps": steps,
                "crew_outcome": "accepted",
                "crew_plan": {"planners": 1, "coders": coders, "reviewers": reviewers},
                "attempts": attempt,
                "evidence": evidence,
                "artifacts": artifacts,
                "workspace": str(mission_dir),
            }

    return {
        "status": "failed",
        "summary": f"Worker reviewers requested changes after {MAX_ATTEMPTS} attempts.",
        "steps": steps,
        "crew_outcome": "rejected",
        "crew_plan": {"planners": 1, "coders": coders, "reviewers": reviewers},
        "attempts": MAX_ATTEMPTS,
        "evidence": {
            "acceptance_criteria": criteria,
            "reviewers": final_reviews,
            "findings": feedback,
        },
        "artifacts": [{"path": path, "content": content} for path, content in sorted(final_files.items())],
        "workspace": str(mission_dir),
    }
