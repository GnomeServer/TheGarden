#!/usr/bin/env python3
"""Run one isolated OMP-planned mission and emit a worker completion object."""

from __future__ import annotations

import json
import os
import shutil
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

MISSION_PATH = Path(os.environ.get("MISSION_PATH", "/mission.json"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "/work"))
OMP_PLANNER_PROMPT = Path("/opt/omp-worker/planner.md")
OMP_AGENT_DIR = Path("/opt/omp-worker/agents")
OMP_MODELS = Path("/opt/omp-worker/models.yml")
OMP_MODEL = os.environ.get("OMP_MODEL", "worker-litellm/luna")
OMP_TIMEOUT_SECONDS = max(60, int(os.environ.get("OMP_TIMEOUT_SECONDS", "600")))


def emit(result: dict[str, Any], exit_code: int = 0) -> None:
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    raise SystemExit(exit_code)


def failure(mission_id: str, summary: str, error: str = "") -> None:
    result: dict[str, Any] = {
        "mission_id": mission_id,
        "status": "failed",
        "summary": summary,
        "bounce_to": "none",
        "steps": [],
        "reviewer_approved": False,
        "reviewer_count": None,
        "artifacts": [],
    }
    if error:
        result["error"] = error[:1200]
    emit(result, 1)


def load_mission() -> dict[str, Any]:
    try:
        value = json.loads(MISSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        failure("unknown", "Worker image could not read its mission file.", f"{type(exc).__name__}: {exc}")
    if not isinstance(value, dict):
        failure("unknown", "Mission file must contain one JSON object.")
    mission_id = value.get("mission_id")
    if not isinstance(mission_id, str) or not mission_id:
        failure("unknown", "Mission is missing mission_id.")
    goal = value.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        failure(mission_id, "Mission is missing a non-empty goal.")
    crew = value.get("crew")
    if not isinstance(crew, dict):
        failure(mission_id, "Mission is missing the sized crew.")
    counts = crew.get("size")
    if not isinstance(counts, dict):
        failure(mission_id, "Mission is missing crew role counts.")
    for role in ("planners", "coders", "reviewers"):
        count = counts.get(role)
        if type(count) is not int or not 1 <= count <= 5:
            failure(mission_id, f"Mission has an invalid {role} count.")
    return value


def prepare_omp_home() -> Path:
    home = Path(os.environ.get("OMP_JOB_HOME", "/tmp/omp-home"))
    agent_dir = home / ".omp" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    model_base_url = os.environ.get("MODEL_BASE_URL", "").strip()
    if not model_base_url:
        raise RuntimeError("MODEL_BASE_URL is missing")
    model_config = OMP_MODELS.read_text(encoding="utf-8").replace(
        "__MODEL_BASE_URL__", json.dumps(model_base_url)
    )
    if "__MODEL_BASE_URL__" in model_config:
        raise RuntimeError("worker model config was not fully rendered")
    (agent_dir / "models.yml").write_text(model_config, encoding="utf-8")
    return home


def prepare_project_agents() -> None:
    destination = WORK_DIR / ".omp" / "agents"
    destination.mkdir(parents=True, exist_ok=True)
    for source in OMP_AGENT_DIR.glob("*.md"):
        shutil.copy2(source, destination / source.name)


def extract_result(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    stripped = text.strip()
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    for index in reversed([offset for offset, char in enumerate(stripped) if char == "{"]):
        try:
            value, _end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("status") in {
            "completed", "accepted", "failed", "blocked", "rejected"
        }:
            return value
    return None


def validate_artifact_paths(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RuntimeError("OMP success must list at least one artifact path")
    if len(value) > 100:
        raise RuntimeError("OMP artifact manifest exceeds 100 files")

    root = WORK_DIR.resolve(strict=True)
    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in value:
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or "\x00" in raw_path
            or "\\" in raw_path
            or any(char in raw_path for char in "\r\n")
        ):
            raise RuntimeError("OMP artifact paths must be non-empty relative POSIX paths")
        relative = PurePosixPath(raw_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or relative.as_posix() != raw_path
            or any(part in {"", ".", ".."} for part in relative.parts)
            or (
                len(raw_path) >= 2
                and raw_path[0].isalpha()
                and raw_path[1] == ":"
            )
        ):
            raise RuntimeError(f"OMP returned an invalid artifact path: {raw_path!r}")
        if raw_path in seen:
            raise RuntimeError(f"OMP returned a duplicate artifact path: {raw_path!r}")
        seen.add(raw_path)

        path = root
        for index, part in enumerate(relative.parts):
            path = path / part
            try:
                info = path.lstat()
            except OSError as exc:
                raise RuntimeError(
                    f"OMP artifact path does not exist: {raw_path!r}"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"OMP artifact path contains a symlink: {raw_path!r}")
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f"OMP artifact parent is not a directory: {raw_path!r}")
        if not stat.S_ISREG(info.st_mode) or not path.resolve(strict=True).is_relative_to(root):
            raise RuntimeError(f"OMP artifact is not a regular file under /work: {raw_path!r}")
        paths.append(raw_path)
    return paths


def normalize_result(result: dict[str, Any], mission_id: str) -> dict[str, Any]:
    if result.get("mission_id") not in (None, mission_id):
        raise RuntimeError("OMP result mission_id did not match the queued mission")
    result["mission_id"] = mission_id

    reviewer_count = result.get("reviewer_count")
    if type(reviewer_count) is not int or reviewer_count < 0:
        raise RuntimeError("OMP result omitted a valid reviewer_count")

    reported_status = result.get("status")
    approved = result.get("reviewer_approved") is True
    result.pop("script", None)
    if reported_status in {"accepted", "completed"} and approved:
        result["artifacts"] = validate_artifact_paths(result.get("artifacts"))
        result["status"] = "completed"
    else:
        result["status"] = "failed"
        result["artifacts"] = []
        if reported_status in {"accepted", "completed"} and not approved:
            result["summary"] = "Worker OMP did not report explicit reviewer approval."
    result["bounce_to"] = "none"
    result["steps"] = [
        item for item in result.get("steps", []) if isinstance(item, str)
    ] if isinstance(result.get("steps"), list) else []
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise RuntimeError("OMP result omitted a useful summary")
    return result

def _write_progress(mission_id: str, phase: str, **details: Any) -> None:
    event = {
        "event": "worker.progress",
        "mission_id": mission_id,
        "phase": phase,
        **details,
    }
    sys.stderr.write(
        "OMP_PROGRESS "
        + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )
    sys.stderr.flush()


def _message_text(message: Any) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def _assistant_text_from_event(event: dict[str, Any]) -> str:
    messages: list[Any] = []
    if event.get("type") in {"message_end", "turn_end"}:
        messages.append(event.get("message"))
    elif event.get("type") == "agent_end" and isinstance(event.get("messages"), list):
        messages.extend(event["messages"])
    return next(
        (text for message in reversed(messages) if (text := _message_text(message))),
        "",
    )
def _assistant_error_from_event(event: dict[str, Any]) -> str:
    assistant_event = event.get("assistantMessageEvent")
    if (
        event.get("type") == "message_update"
        and isinstance(assistant_event, dict)
        and assistant_event.get("type") == "error"
        and isinstance(assistant_event.get("reason"), str)
    ):
        return assistant_event["reason"]
    messages: list[Any] = []
    if event.get("type") in {"message_end", "turn_end"}:
        messages.append(event.get("message"))
    elif event.get("type") == "agent_end" and isinstance(event.get("messages"), list):
        messages.extend(event["messages"])
    for message in reversed(messages):
        if isinstance(message, dict):
            error = message.get("errorMessage")
            if isinstance(error, str) and error:
                return error
    return ""


def _task_agents(arguments: Any) -> list[str]:
    if not isinstance(arguments, dict):
        return []
    agents: list[str] = []
    single = arguments.get("agent")
    if single in {"coder", "reviewer"}:
        agents.append(single)
    tasks = arguments.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict) and task.get("agent") in {"coder", "reviewer"}:
                agents.append(task["agent"])
    return agents


def _reviewer_outputs(result: Any) -> list[str]:
    if isinstance(result, str):
        return [result]
    if not isinstance(result, dict):
        return []
    details = result.get("details")
    results = details.get("results") if isinstance(details, dict) else None
    if isinstance(results, list):
        outputs = [
            item["output"]
            for item in results
            if isinstance(item, dict) and isinstance(item.get("output"), str)
        ]
        if outputs:
            return outputs
    content = result.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
    return []


def _reviewer_verdicts(result: Any) -> tuple[int, int]:
    approved = 0
    rejected = 0
    for output in _reviewer_outputs(result):
        first_line = next(
            (line.strip() for line in output.splitlines() if line.strip()),
            "",
        )
        if first_line == "approved":
            approved += 1
        elif first_line.startswith("changes_requested:"):
            rejected += 1
    return approved, rejected


def _task_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("isError") is True:
        return True
    details = result.get("details")
    results = details.get("results") if isinstance(details, dict) else None
    return isinstance(results, list) and any(
        isinstance(item, dict)
        and (
            item.get("isError") is True
            or (type(item.get("exitCode")) is int and item["exitCode"] != 0)
        )
        for item in results
    )


def _progress_from_omp_event(
    event: dict[str, Any], state: dict[str, Any]
) -> list[dict[str, Any]]:
    event_type = event.get("type")
    if event_type == "agent_start" and not state.get("planner_active"):
        state["planner_active"] = True
        return [{"phase": "planner_active"}]
    if event_type == "tool_execution_start" and event.get("toolName") == "task":
        call_id = event.get("toolCallId")
        if not isinstance(call_id, str):
            state["anonymous_call"] = state.get("anonymous_call", 0) + 1
            call_id = f"anonymous-{state['anonymous_call']}"
        arguments = event.get("args", event.get("arguments"))
        agents = _task_agents(arguments)
        state.setdefault("task_calls", {})[call_id] = agents
        updates: list[dict[str, Any]] = []
        coders = agents.count("coder")
        reviewers = agents.count("reviewer")
        if coders:
            updates.append({"phase": "coders_started", "count": coders})
        if reviewers:
            state["review_calls"] = state.get("review_calls", 0) + 1
            updates.append(
                {
                    "phase": "reviewers_started",
                    "count": reviewers,
                    "review_call": state["review_calls"],
                }
            )
        if not updates:
            updates.append({"phase": "subagent_task_started"})
        return updates

    if event_type != "tool_execution_end":
        return []
    call_id = event.get("toolCallId")
    task_calls = state.setdefault("task_calls", {})
    agents = task_calls.pop(call_id, []) if isinstance(call_id, str) else []
    if not agents:
        return []
    result = event.get("result", event.get("toolResult"))
    updates = []
    coders = agents.count("coder")
    reviewers = agents.count("reviewer")
    if coders:
        updates.append(
            {
                "phase": "coders_failed" if _task_failed(result) else "coders_completed",
                "count": coders,
            }
        )
    if reviewers:
        approved, rejected = _reviewer_verdicts(result)
        total_rejections = state.get("reviewer_rejections", 0) + rejected
        state["reviewer_rejections"] = total_rejections
        if rejected:
            updates.append(
                {
                    "phase": "reviewers_rejected",
                    "approved": approved,
                    "rejected": rejected,
                    "rejections_seen": total_rejections,
                }
            )
        elif approved:
            updates.append(
                {
                    "phase": "reviewers_approved",
                    "approved": approved,
                    "rejections_seen": total_rejections,
                }
            )
        else:
            updates.append(
                {
                    "phase": "reviewer_result_received",
                    "count": reviewers,
                    "verdict": "unclassified",
                    "rejections_seen": total_rejections,
                }
            )
    return updates


def _read_stream_tail(stream: Any, limit: int = 1200) -> str:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - limit))
    return stream.read().decode("utf-8", errors="replace").strip()



def run_omp(mission: dict[str, Any]) -> dict[str, Any]:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    prepare_project_agents()
    home = prepare_omp_home()
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["PI_CODING_AGENT_DIR"] = str(home / ".omp" / "agent")
    environment["LITELLM_API_KEY"] = environment.get("MODEL_API_KEY", "")
    environment["NODE_EXTRA_CA_CERTS"] = environment.get(
        "NODE_EXTRA_CA_CERTS", "/etc/ssl/certs/caddy-root.crt"
    )

    crew_size = mission.get("crew", {}).get("size", {})
    _write_progress(
        mission["mission_id"],
        "planner_starting",
        planners=crew_size.get("planners", 1),
        coders=crew_size.get("coders", 1),
        reviewers=crew_size.get("reviewers", 1),
    )
    initial_prompt = (
        "Run this worker mission. The current directory is the only writable workspace.\n"
        "Mission JSON:\n"
        + json.dumps(mission, ensure_ascii=False)
        + "\nUse the requested planner/coder/reviewer counts and return the required JSON result."
    )
    command = [
        "omp",
        "--print",
        "--mode",
        "json",
        "--no-session",
        "--no-lsp",
        "--no-extensions",
        "--tools",
        "read,bash,edit,write,grep,glob,task",
        "--model",
        OMP_MODEL,
        "--approval-mode",
        "yolo",
        "--append-system-prompt",
        str(OMP_PLANNER_PROMPT),
        "--",
        initial_prompt,
    ]

    deadline = time.monotonic() + OMP_TIMEOUT_SECONDS
    progress_state: dict[str, Any] = {"reviewer_rejections": 0}
    last_assistant_text = ""
    last_assistant_error = ""
    recent_output: list[str] = []
    with tempfile.TemporaryFile() as stderr_capture:
        process = subprocess.Popen(
            command,
            cwd=WORK_DIR,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_capture,
            bufsize=0,
        )
        if process.stdout is None:
            raise RuntimeError("OMP stdout pipe was not created")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        pending = bytearray()
        stdout_closed = False

        def consume_line(line_bytes: bytes) -> None:
            nonlocal last_assistant_text, last_assistant_error
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                recent_output.append(line[-1200:])
                del recent_output[:-8]
                return
            if not isinstance(event, dict):
                return
            text = _assistant_text_from_event(event)
            if text:
                last_assistant_text = text
            error = _assistant_error_from_event(event)
            if error:
                last_assistant_error = error
            for update in _progress_from_omp_event(event, progress_state):
                phase = update["phase"]
                details = {key: value for key, value in update.items() if key != "phase"}
                _write_progress(mission["mission_id"], phase, **details)

        try:
            while not stdout_closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, OMP_TIMEOUT_SECONDS)
                ready = selector.select(min(remaining, 0.25))
                for key, _mask in ready:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        stdout_closed = True
                        break
                    pending.extend(chunk)
                    while True:
                        newline = pending.find(b"\n")
                        if newline < 0:
                            break
                        line_bytes = bytes(pending[:newline])
                        del pending[: newline + 1]
                        consume_line(line_bytes)
            if pending:
                consume_line(bytes(pending))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, OMP_TIMEOUT_SECONDS)
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise RuntimeError(f"OMP exceeded {OMP_TIMEOUT_SECONDS} seconds") from exc
        except Exception:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        finally:
            selector.close()
            process.stdout.close()
        stderr_detail = _read_stream_tail(stderr_capture)

    if return_code != 0:
        detail = stderr_detail or last_assistant_error or last_assistant_text or "\n".join(recent_output)
        raise RuntimeError(
            f"OMP exited with code {return_code}: "
            f"{detail[-1200:] or 'OMP exited without output'}"
        )
    result = extract_result(last_assistant_text)
    if result is None:
        detail = last_assistant_error or last_assistant_text or stderr_detail or "\n".join(recent_output)
        raise RuntimeError(
            "OMP did not return the required JSON result: "
            f"{detail[-1200:] or 'OMP returned no final result'}"
        )
    return normalize_result(result, mission["mission_id"])


def main() -> None:
    mission = load_mission()
    try:
        result = run_omp(mission)
        _write_progress(
            mission["mission_id"],
            "mission_finished",
            status=result["status"],
            reviewer_count=result.get("reviewer_count"),
        )
        emit(result)
    except Exception as exc:
        _write_progress(
            mission["mission_id"],
            "mission_failed",
            error_type=type(exc).__name__,
        )
        failure(
            mission["mission_id"],
            f"Worker OMP failed ({type(exc).__name__}).",
            f"{type(exc).__name__}: {exc}",
        )


if __name__ == "__main__":
    main()
