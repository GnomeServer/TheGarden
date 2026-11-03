#!/usr/bin/env python3
"""Run one isolated OMP-planned mission and emit a worker completion object."""

from __future__ import annotations

import json
import os
import re
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
OMP_TIMEOUT_SECONDS = max(60, int(os.environ.get("OMP_TIMEOUT_SECONDS", "1200")))


def emit(result: dict[str, Any], exit_code: int = 0) -> None:
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    raise SystemExit(exit_code)


def failure(
    mission_id: str,
    summary: str,
    error: str = "",
    provider_error: dict[str, Any] | None = None,
) -> None:
    result: dict[str, Any] = {
        "mission_id": mission_id,
        "status": "failed",
        "summary": _sanitize_diagnostic(summary, 1200),
        "bounce_to": "none",
        "steps": [],
        "reviewer_approved": False,
        "reviewer_count": None,
        "artifacts": [],
    }
    if error:
        result["error"] = _sanitize_diagnostic(error, 1200)
    if provider_error:
        _attach_provider_error(result, provider_error)
    emit(result, 1)


def load_mission() -> dict[str, Any]:
    expected_mission_id = os.environ.get("OMP_EXPECTED_MISSION_ID", "")
    if not expected_mission_id:
        failure("unknown", "Worker image is missing its expected mission identity.")
    try:
        value = json.loads(MISSION_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        failure(
            expected_mission_id,
            "Worker image could not read its mission file.",
            f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(value, dict):
        failure(expected_mission_id, "Mission file must contain one JSON object.")
    mission_id = value.get("mission_id")
    if not isinstance(mission_id, str) or not mission_id:
        failure(expected_mission_id, "Mission is missing mission_id.")
    if mission_id != expected_mission_id:
        failure(expected_mission_id, "Mission file has a mismatched mission_id.")
    goal = value.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        failure(mission_id, "Mission is missing a non-empty goal.")
    crew_plan = value.get("crew_plan")
    if not isinstance(crew_plan, dict):
        failure(mission_id, "Mission is missing its crew plan.")
    for role in ("planners", "coders", "reviewers"):
        count = crew_plan.get(role)
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
    (agent_dir / "config.yml").write_text(
        json.dumps(
            {
                "web_search": {"enabled": True},
                "fetch": {"enabled": True},
                "modelRoles": {"web": "web/duckduckgo"},
                "retry": {"fallbackChains": {"web": []}},
                "providers": {"webSearchTimeoutSeconds": 30},
            }
        ),
        encoding="utf-8",
    )
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
        if (
            isinstance(value, dict)
            and value.get("status") in {
                "completed",
                "accepted",
                "failed",
                "blocked",
                "rejected",
                "needs_input",
            }
        ):
            return value


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
    if reported_status == "needs_input":
        question = result.get("question")
        if approved or reviewer_count < 10 or not isinstance(question, str) or not question.strip():
            raise RuntimeError(
                "OMP needs_input result must include ten reviewer rejections and a clarification question"
            )
        result["status"] = "needs_input"
        result["reviewer_approved"] = False
        result["question"] = question.strip()[:2000]
        result["artifacts"] = []
    elif reported_status in {"accepted", "completed"} and approved:
        result["artifacts"] = validate_artifact_paths(result.get("artifacts"))
        result["status"] = "completed"
    else:
        result["status"] = "failed"
        result["artifacts"] = []
        if reported_status in {"accepted", "completed"} and not approved:
            result["summary"] = "Worker OMP did not report explicit reviewer approval."
    result["bounce_to"] = "none"
    result["steps"] = (
        [item for item in result.get("steps", []) if isinstance(item, str)]
        if isinstance(result.get("steps"), list)
        else []
    )
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


def _sanitize_diagnostic(text: str, limit: int = 600) -> str:
    """Redact credentials and labelled payloads before truncating diagnostics."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(char for char in text if char.isprintable() or char.isspace())
    for key, value in os.environ.items():
        if value and re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", key, re.I):
            text = text.replace(value, "[REDACTED CREDENTIAL]")
    text = re.sub(
        r"sk-(?:or-v1-|ant-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35,}",
        "[REDACTED CREDENTIAL]",
        text,
    )
    text = re.sub(r"https?://[^\s<>\"']+", "[REDACTED URL]", text, flags=re.I)
    text = re.sub(
        r"(?i)\b(?:authorization|proxy-authorization)\b[\"']?\s*[:=]\s*"
        r"(?:[\"'][^\"'\r\n]*[\"']|[^\r\n,;}]+)",
        "[REDACTED CREDENTIAL]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|token|password|secret)\b[\"']?\s*[:=]\s*"
        r"(?:[\"'][^\"'\r\n]*[\"']|[^\s,;}]+)|\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "[REDACTED CREDENTIAL]",
        text,
    )
    text = re.sub(
        r"(?is)\b(?:prompt|messages|tool[_ ]?arguments|arguments|request[_ ]?body|response[_ ]?body)"
        r"[\"']?\s*[:=].*",
        "[OMITTED PAYLOAD]",
        text,
    )
    return " ".join(text.split())[:limit]


def _error_diagnostic(
    message: str, source: str, metadata: dict[str, Any], category: str
) -> dict[str, Any]:
    # Only explicitly labelled identifiers are evidence. OMP's errorId is not a
    # provider code, and responseId is not an HTTP request ID.
    text = _sanitize_diagnostic(message, 4096)
    diagnostic: dict[str, Any] = {
        "category": category,
        "source": source,
        "message": text[:600] or "OMP reported an error without a diagnostic message.",
    }
    for target, keys in (
        ("provider", ("provider",)),
        ("model", ("model",)),
        ("code", ("code", "errorCode")),
        ("request_id", ("requestId", "request_id")),
        ("response_id", ("responseId",)),
        ("agent", ("agent",)),
        ("agent_id", ("id",)),
        ("stop_reason", ("stopReason",)),
    ):
        value = next((metadata[key] for key in keys if key in metadata), None)
        if target in {"code", "request_id"} and value is None:
            label = r"(?:error[_ -]?code|code)" if target == "code" else r"(?:x-request-id|request[_ -]?id)"
            match = re.search(rf"\b{label}[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.:/-]+)", text, re.I)
            if match:
                value = match.group(1)
        if target == "code" and type(value) is int:
            value = str(value)
        if (
            isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value)
            and _sanitize_diagnostic(value, 128) == value
        ):
            diagnostic[target] = value
    status = metadata.get("errorStatus")
    if type(status) is int and 100 <= status <= 599:
        diagnostic["http_status"] = status
    error_id = metadata.get("errorId")
    if type(error_id) is int and 0 <= error_id < 2**64:
        diagnostic["omp_error_id"] = error_id
    if (
        re.match(r"^(?:\[[^\]\n]{1,160}\]\s*)?Provider finish_reason:\s*content_filter\b", text)
        or diagnostic.get("code") in {"content_filter", "content_policy_violation", "ResponsibleAIPolicyViolation"}
    ):
        diagnostic["category"] = "content_filter"
    return diagnostic


def _task_error_diagnostic(result: Any, source: str) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    details = result.get("details")
    results = details.get("results") if isinstance(details, dict) else None
    if not isinstance(results, list):
        return None
    for child in reversed(results):
        if not isinstance(child, dict) or child.get("aborted") is True:
            continue
        failed = child.get("isError") is True or (
            type(child.get("exitCode")) is int and child["exitCode"] != 0
        )
        if not failed:
            continue
        for field in ("error", "stderr"):
            message = child.get(field)
            if isinstance(message, str) and message.strip():
                # Child output can contain partial tool arguments or normal prose.
                # Its dedicated error/stderr is evidence, output is never evidence.
                return _error_diagnostic(message, f"{source}.details.results.{field}", child, "task_error")
    return None


def _provider_error_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    assistant_event = event.get("assistantMessageEvent")
    if event_type == "message_update" and isinstance(assistant_event, dict) and assistant_event.get("type") == "error":
        message = assistant_event.get("error")
        if isinstance(message, dict):
            error = message.get("errorMessage")
            return _error_diagnostic(
                error if isinstance(error, str) else "OMP assistant stream ended with an error.",
                "message_update.assistantMessageEvent.error",
                message,
                "provider_error",
            )
    if event_type == "tool_execution_end" and event.get("toolName") == "task":
        return _task_error_diagnostic(event.get("result"), "tool_execution_end.result")
    messages: list[Any] = []
    if event_type in {"message_end", "turn_end"}:
        messages.append(event.get("message"))
    elif event_type == "agent_end" and isinstance(event.get("messages"), list):
        messages = event["messages"]
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "toolResult" and message.get("toolName") == "task":
            diagnostic = _task_error_diagnostic(message, f"{event_type}.toolResult")
            if diagnostic:
                return diagnostic
        if message.get("role") == "assistant":
            error = message.get("errorMessage")
            if isinstance(error, str) and error.strip():
                source = "agent_end.messages" if event_type == "agent_end" else f"{event_type}.message"
                return _error_diagnostic(error, source, message, "provider_error")
    return None


def _provider_failure_summary(diagnostic: dict[str, Any]) -> str:
    if diagnostic["category"] == "content_filter":
        return "The provider stopped generation with a content filter. Use a benign example or narrow the request; do not retry unchanged."
    if diagnostic["category"] == "task_error":
        return "An OMP child task failed. Inspect the reported task error before rerunning the mission."
    return "OMP reported a provider error. Inspect gateway/provider logs using the reported identifiers or failure time."


def _attach_provider_error(result: dict[str, Any], diagnostic: dict[str, Any]) -> None:
    result["provider_error"] = diagnostic
    identifiers = "; ".join(
        f"{key}={diagnostic[key]}"
        for key in ("provider", "model", "code", "request_id", "response_id", "http_status", "agent")
        if key in diagnostic
    )
    error = f"{_provider_failure_summary(diagnostic)} {diagnostic['message']}"
    if identifiers:
        error += f" ({identifiers})"
    prior_error = result.get("error")
    if isinstance(prior_error, str) and prior_error.strip():
        error += f" Detail: {prior_error}"
    result["error"] = _sanitize_diagnostic(error, 1200)


class OmpExecutionError(RuntimeError):
    def __init__(self, message: str, provider_error: dict[str, Any] | None) -> None:
        super().__init__(_sanitize_diagnostic(message, 1200))
        self.provider_error = provider_error


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
    text = stream.read().decode("utf-8", errors="replace")
    if size > limit:
        # A tail starting halfway through a credential cannot be safely redacted.
        text = text.partition("\n")[2]
    return _sanitize_diagnostic(text, limit)



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
        "read,bash,edit,write,grep,glob,task,web_search",
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
    observed_error: dict[str, Any] | None = None
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
            nonlocal last_assistant_text, observed_error
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return
            if not isinstance(event, dict):
                return
            text = _assistant_text_from_event(event)
            if text:
                last_assistant_text = text
            error = _provider_error_from_event(event)
            if error:
                observed_error = error
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
            raise OmpExecutionError(f"OMP exceeded {OMP_TIMEOUT_SECONDS} seconds", observed_error) from exc
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
        detail = "See observed OMP error." if observed_error else (stderr_detail or "No stderr diagnostic.")
        raise OmpExecutionError(
            f"OMP exited with code {return_code}: {detail}",
            observed_error,
        )
    result = extract_result(last_assistant_text)
    if result is None:
        # Assistant text may be the rejected generation. Do not echo it as an error.
        raise OmpExecutionError(
            "OMP did not return the required JSON result." + (f" {stderr_detail}" if not observed_error else ""),
            observed_error,
        )
    # This field is reserved for observed runtime evidence, not planner claims.
    result.pop("provider_error", None)
    try:
        result = normalize_result(result, mission["mission_id"])
    except RuntimeError as exc:
        raise OmpExecutionError(str(exc), observed_error) from exc
    if result["status"] != "completed":
        result["summary"] = _sanitize_diagnostic(result["summary"], 1200)
        if isinstance(result.get("error"), str):
            result["error"] = _sanitize_diagnostic(result["error"], 1200)
        if observed_error:
            _attach_provider_error(result, observed_error)
    return result


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
        diagnostic = exc.provider_error if isinstance(exc, OmpExecutionError) else None
        failure(
            mission["mission_id"],
            _provider_failure_summary(diagnostic) if diagnostic else f"Worker OMP failed ({type(exc).__name__}).",
            f"{type(exc).__name__}: {exc}",
            diagnostic,
        )


if __name__ == "__main__":
    main()
