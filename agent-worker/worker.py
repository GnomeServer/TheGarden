#!/usr/bin/env python3
"""Long-running Agent Manager worker that generates and runs Python scripts."""

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
import uuid
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import nats
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy


NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
WORKER_ID = os.getenv("WORKER_ID", "worker-1")
WORKER_ROLE = os.getenv("WORKER_ROLE", "coder")
WORKSPACE = Path(
    os.getenv("AGENT_WORKSPACE", str(Path.home() / "agent-workspace"))
)
WORKER_CONSUMER = os.getenv(
    "WORKER_CONSUMER", f"agent-workers-{WORKER_ROLE}"
)

# MODEL_BASE_URL should include the OpenAI-compatible /v1 path, for example:
# https://infra-lab-services.tail494f6d.ts.net/litellm/v1
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "").rstrip("/")
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "luna")
MODEL_TIMEOUT = float(os.getenv("MODEL_TIMEOUT", "120"))
CREW_IMAGE = os.getenv("CREW_IMAGE", "crew:slice")
SCRIPT_TIMEOUT = float(os.getenv("SCRIPT_TIMEOUT", "300"))
MAX_SCRIPT_CHARS = int(os.getenv("MAX_SCRIPT_CHARS", "30000"))
MAX_OUTPUT_CHARS = int(os.getenv("MAX_OUTPUT_CHARS", "12000"))
ALLOW_GENERATED_CODE = os.getenv("ALLOW_GENERATED_CODE", "0").lower() in {
    "1",
    "true",
    "yes",
}
MODEL_TLS_INSECURE = os.getenv("MODEL_TLS_INSECURE", "0").lower() in {
    "1",
    "true",
    "yes",
}
WORKER_VERSION = "0.4.0"
HEARTBEAT_SECONDS = 15
_presence = {"status": "idle", "run_id": None}


def heartbeat_payload() -> dict[str, Any]:
    status = _presence["status"] if _presence["status"] in {"idle", "busy", "degraded"} else "idle"
    return {
        "event": "worker.heartbeat",
        "event_id": f"heartbeat:{WORKER_ID}:{uuid.uuid4()}",
        "worker_id": WORKER_ID,
        "worker_role": WORKER_ROLE,
        "hostname": socket.gethostname(),
        "status": status,
        "current_run_id": _presence["run_id"],
        "version": WORKER_VERSION,
        "capabilities": ["mission-crew-demo"] + (["create-python-script"] if ALLOW_GENERATED_CODE else []),
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


def running_status_event(run_id: str) -> dict[str, Any]:
    return {
        "event": "run.started",
        "run_id": run_id,
        "worker_id": WORKER_ID,
        "worker_role": WORKER_ROLE,
        "hostname": socket.gethostname(),
        "status": "running",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "event_id": f"run-status:{run_id}:running",
    }


def run_event_expired(event: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Reject queued events whose Manager-assigned execution window elapsed."""
    deadline = event.get("expires_at")
    if deadline is None:
        return False
    if not isinstance(deadline, str):
        return True
    try:
        expires_at = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        return True
    current = now or datetime.now(timezone.utc)
    return current >= expires_at.astimezone(timezone.utc)



SYSTEM_PROMPT = """You generate one complete Python 3 source file from a user request.
Return only the source code, without Markdown fences or commentary.
Use only the Python standard library. The script must operate only in its
current working directory, must not use the network, must not invoke a shell
or subprocess, must not access secrets, and must finish in a bounded amount of
time. Do not call input(), read from stdin, ask follow-up questions, or wait
for human input. This is a headless Linux worker: do not import tkinter or
other GUI/desktop modules, do not require third-party packages, and do not
install packages. If a GUI is requested, create a text or file-based
alternative and explain the assumption. Make reasonable assumptions when
details are missing and state them in the final stdout output. Make the result
deterministic when the request involves generated data.
Include useful validation and concise stdout output. Create any requested
artifacts in the current working directory.
"""


def completions_url() -> str:
    if not MODEL_BASE_URL:
        raise RuntimeError("MODEL_BASE_URL is required for dynamic prompts")
    if not MODEL_API_KEY:
        raise RuntimeError("MODEL_API_KEY is required for dynamic prompts")
    return f"{MODEL_BASE_URL}/chat/completions"


def extract_source(content: str) -> str:
    """Accept a raw source response and tolerate an accidental code fence."""
    content = content.strip()
    fenced = re.findall(
        r"```(?:python|py)?\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL
    )
    if fenced:
        content = max(fenced, key=len).strip()

    if not content:
        raise RuntimeError("model returned an empty script")
    if len(content) > MAX_SCRIPT_CHARS:
        raise RuntimeError(
            f"model returned {len(content)} characters; "
            f"limit is {MAX_SCRIPT_CHARS}"
        )
    return content + ("\n" if not content.endswith("\n") else "")


def generate_source_sync(goal: str) -> str:
    payload = {
        "model": MODEL_NAME,
        "temperature": 0.2,
        "max_tokens": 5000,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ],
    }
    request = urllib.request.Request(
        completions_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {MODEL_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    context = None
    if MODEL_TLS_INSECURE and completions_url().startswith("https://"):
        context = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(
            request,
            timeout=MODEL_TIMEOUT,
            **({"context": context} if context is not None else {}),
        ) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(
            f"model API returned HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach model API: {exc.reason}") from exc

    try:
        completion = json.loads(response_body)
        content = completion["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("model API returned an unexpected response") from exc

    if not isinstance(content, str):
        raise RuntimeError("model response content was not text")
    return extract_source(content)


async def generate_source(goal: str, metadata: dict[str, Any]) -> tuple[str, str]:
    # A supplied script is useful for deterministic pipeline tests. Dynamic
    # prompts use the model when metadata does not contain script content.
    supplied_script = metadata.get("script")
    if supplied_script is not None:
        if not isinstance(supplied_script, str) or not supplied_script.strip():
            raise RuntimeError("metadata.script must be a non-empty string")
        return extract_source(supplied_script), "provided"

    source = await asyncio.to_thread(generate_source_sync, goal)
    return source, "model"


def project_directory_name(project_name: Any) -> str | None:
    if project_name is None:
        return None
    if (
        not isinstance(project_name, str)
        or not project_name.strip()
        or len(project_name) > 120
        or any(not character.isprintable() for character in project_name)
    ):
        raise ValueError("project_name must contain printable text")
    ascii_name = (
        unicodedata.normalize("NFKD", project_name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")
    return slug[:80].rstrip("-") or "project"


def python_script_filename(script_name: Any) -> str:
    if script_name is None:
        return "generated_agent.py"
    if not isinstance(script_name, str):
        raise ValueError("script_name must be a filename")
    value = script_name.strip()
    if value.casefold().endswith(".py"):
        stem = value[:-3]
    elif "." in value:
        raise ValueError("script_name must use the .py extension")
    else:
        stem = value
    if (
        not stem
        or not stem[0].isalnum()
        or any(
            not character.isalnum() and character not in " _.-"
            for character in stem
        )
    ):
        raise ValueError("script_name must be a simple Python filename, not a path")
    filename = f"{stem}.py"
    if len(filename) > 128:
        raise ValueError("script_name must be at most 128 characters")
    return filename


def write_script_without_overwrite(
    directory: Path, filename: str, source: str
) -> Path:
    stem = filename[:-3]
    candidate = filename
    suffix = 2
    while True:
        path = directory / candidate
        try:
            with path.open("x", encoding="utf-8") as script_file:
                script_file.write(source)
            return path
        except FileExistsError:
            suffix_text = f"_{suffix}"
            stem_limit = 128 - len(suffix_text) - len(".py")
            candidate = f"{stem[:stem_limit]}{suffix_text}.py"
            suffix += 1


async def run_mission_image(event: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """Run the worker-local OMP crew and return its bounded evidence."""
    mission_id = event.get("run_id") if isinstance(event.get("run_id"), str) else "unknown"
    goal = event.get("goal") if isinstance(event.get("goal"), str) else ""
    repo = event.get("forgejo_repository") if isinstance(event.get("forgejo_repository"), str) else ""
    plan = metadata.get("mission")
    if not isinstance(plan, dict):
        raise RuntimeError("mission event is missing its crew plan")
    counts = {
        name: plan.get(name)
        for name in ("planners", "coders", "reviewers")
    }
    if (
        type(counts["planners"]) is not int
        or counts["planners"] != 1
        or type(counts["coders"]) is not int
        or not 1 <= counts["coders"] <= 3
        or type(counts["reviewers"]) is not int
        or not 1 <= counts["reviewers"] <= 2
    ):
        raise RuntimeError("mission crew plan is outside the worker's supported limits")
    mission = {
        "mission_id": mission_id,
        "goal": goal,
        "repo": repo,
        **counts,
    }
    steps = [
        "Worker OMP: validated the crew plan.",
        f"Planner: 1 planner, {counts['coders']} coder(s), {counts['reviewers']} reviewer(s), up to 3 attempts.",
    ]
    if not shutil.which("docker"):
        return {
            "status": "failed",
            "summary": "This laptop has no Docker, so the worker-local crew did not start.",
            "steps": steps,
        }

    with tempfile.TemporaryDirectory(prefix="crew-") as directory:
        root = Path(directory)
        work = root / "work"
        work.mkdir()
        mission_path = root / "mission.json"
        mission_path.write_text(json.dumps(mission) + "\n", encoding="utf-8")
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [
                    "docker", "run", "--rm", "--network", "none",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "-v", f"{mission_path}:/mission.json:ro",
                    "-v", f"{work}:/work:rw",
                    CREW_IMAGE,
                ],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "failed",
                "summary": "The worker-local crew did not finish within 180 seconds.",
                "steps": steps,
            }
        line = next(
            (item for item in reversed(completed.stdout.splitlines()) if item.startswith("{")),
            "",
        )
        if not line:
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "no result JSON"
            return {
                "status": "failed",
                "summary": f"The worker-local crew exited without a result. {detail[:160]}",
                "steps": steps,
            }
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise RuntimeError("worker-local OMP returned an invalid result")
        outcome = payload.get("status")
        summary = payload.get("summary") if isinstance(payload.get("summary"), str) else "Crew finished."
        payload_steps = payload.get("steps")
        if isinstance(payload_steps, list):
            steps.extend(item for item in payload_steps if isinstance(item, str))
        steps.append(f"Worker OMP: finalized with {outcome}.")
        finalized = outcome in {"accepted", "rejected", "blocked"}
        return {
            "status": "completed" if outcome == "accepted" else "failed",
            "summary": summary,
            "steps": steps,
            "crew_outcome": outcome,
            "crew_plan": counts,
            "attempts": payload.get("attempts"),
            "evidence": payload.get("evidence"),
            "bounce_to": payload.get("bounce_to"),
            "finalized": finalized,
        }


async def process_message(js: Any, message: Any) -> None:
    event = json.loads(message.data.decode("utf-8"))
    metadata = event.get("metadata") or {}

    # Dynamic tasks omit worker_id and are assigned by the shared NATS
    # consumer. A worker_id is still honored for explicitly targeted jobs.
    target_worker = metadata.get("worker_id")
    if target_worker and target_worker != WORKER_ID:
        raise RuntimeError(
            f"task is for {target_worker!r}, not {WORKER_ID!r}"
        )

    target_role = metadata.get("worker_role", WORKER_ROLE)
    if target_role != WORKER_ROLE:
        raise RuntimeError(
            f"task requires role {target_role!r}, "
            f"but this worker provides {WORKER_ROLE!r}"
        )

    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("event is missing run_id")

    result: dict[str, Any] = {
        "event": "run.completed",
        "run_id": run_id,
        "worker_id": WORKER_ID,
        "hostname": socket.gethostname(),
        "status": "failed",
        "script": None,
    }
    if run_event_expired(event):
        result.update({
            "status": "failed",
            "summary": "The run expired before a worker could start it.",
            "error": "run delivery expired before worker pickup",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        await js.publish("agent.runs.completed", json.dumps(result).encode("utf-8"))
        print(json.dumps(result, indent=2), flush=True)
        await message.ack()
        return
    result["started_at"] = datetime.now(timezone.utc).isoformat()


    try:
        _presence["status"] = "busy"
        _presence["run_id"] = run_id
        status_payload = running_status_event(run_id)
        encoded_status = json.dumps(status_payload).encode("utf-8")
        await js.publish("agent.runs.status", encoded_status)
        await js.publish("agent.runs.started", encoded_status)

        if metadata.get("operation") == "mission":
            result.update(await run_mission_image(event, metadata))
        elif metadata.get("operation") != "create-python-script":
            raise RuntimeError("unsupported worker operation")
        else:
            goal = event.get("goal")
        if metadata.get("operation") != "mission":
            goal = event.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise RuntimeError("event is missing a non-empty goal")
            if not ALLOW_GENERATED_CODE:
                raise RuntimeError(
                    "refusing to execute generated code; set ALLOW_GENERATED_CODE=1 "
                    "only on a disposable/test worker"
                )
            project_component = project_directory_name(metadata.get("project_name"))
            script_filename = python_script_filename(metadata.get("script_name"))
            execution_dir = (
                WORKSPACE / project_component
                if project_component
                else WORKSPACE / run_id
            )
            execution_dir.mkdir(
                parents=True,
                exist_ok=project_component is not None,
            )
            source, source_origin = await generate_source(goal, metadata)
            compile(source, str(execution_dir / script_filename), "exec")
            script_path = write_script_without_overwrite(
                execution_dir, script_filename, source
            )
            result["script"] = str(script_path)
            try:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, str(script_path)],
                    cwd=execution_dir,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=SCRIPT_TIMEOUT,
                    check=False,
                )
                result.update(
                    {
                        "status": "completed" if completed.returncode == 0 else "failed",
                        "returncode": completed.returncode,
                        "stdout": completed.stdout[:MAX_OUTPUT_CHARS],
                        "stderr": completed.stderr[:MAX_OUTPUT_CHARS],
                    }
                )
            except subprocess.TimeoutExpired as exc:
                result.update(
                    {
                        "status": "failed",
                        "returncode": None,
                        "error": f"script exceeded {SCRIPT_TIMEOUT:g} seconds",
                        "stdout": str(exc.stdout or "")[:MAX_OUTPUT_CHARS],
                        "stderr": str(exc.stderr or "")[:MAX_OUTPUT_CHARS],
                    }
                )
            result["source"] = source_origin
    except Exception as exc:
        # Even model, validation, workspace, and compile failures become a
        # completion event so the manager can mark the database run failed.
        result["error"] = str(exc)[:2000]
    result.setdefault("completed_at", datetime.now(timezone.utc).isoformat())

    try:
        await js.publish("agent.runs.completed", json.dumps(result).encode("utf-8"))
        print(json.dumps(result, indent=2), flush=True)
        await message.ack()
    finally:
        _presence["status"] = "idle"
        _presence["run_id"] = None


async def main() -> None:
    nc = await nats.connect(
        NATS_URL,
        name=f"{WORKER_ID}-{socket.gethostname()}",
    )
    js = nc.jetstream()

    async def reply_health(message: Any) -> None:
        try:
            body = json.loads(message.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        target = body.get("worker_id") if isinstance(body, dict) else None
        if target and target != WORKER_ID:
            return
        if getattr(message, "reply", None):
            await nc.publish(
                message.reply,
                json.dumps({"worker_id": WORKER_ID, "worker_role": WORKER_ROLE}).encode("utf-8"),
            )

    async def heartbeat_loop() -> None:
        while True:
            try:
                await js.publish(
                    "agent.workers.heartbeat",
                    json.dumps(heartbeat_payload()).encode("utf-8"),
                )
            except Exception as exc:
                print(f"heartbeat failed: {type(exc).__name__}", flush=True)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    await nc.subscribe(f"agent.workers.health.{WORKER_ROLE}", cb=reply_health)
    heartbeat = asyncio.create_task(heartbeat_loop())
    subscription = await js.pull_subscribe(
        "agent.runs.created",
        stream="AGENT_RUNS",
        durable=WORKER_CONSUMER,
        config=ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=900,
            deliver_policy=DeliverPolicy.ALL,
        ),
    )

    try:
        while True:
            try:
                messages = await subscription.fetch(1, timeout=120)
            except NatsTimeoutError:
                continue
            await process_message(js, messages[0])
    finally:
        heartbeat.cancel()
        await subscription.unsubscribe()
        await nc.close()


if __name__ == "__main__":
    asyncio.run(main())
