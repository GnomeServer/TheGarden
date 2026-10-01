#!/usr/bin/env python3
"""Long-running Agent Manager worker that generates and runs Python scripts."""

import asyncio
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
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

    script_path = WORKSPACE / run_id / "generated_agent.py"
    result: dict[str, Any] = {
        "event": "run.completed",
        "run_id": run_id,
        "worker_id": WORKER_ID,
        "hostname": socket.gethostname(),
        "status": "failed",
        "script": str(script_path),
    }

    try:
        if metadata.get("operation") != "create-python-script":
            raise RuntimeError("unsupported worker operation")

        goal = event.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise RuntimeError("event is missing a non-empty goal")
        if not ALLOW_GENERATED_CODE:
            raise RuntimeError(
                "refusing to execute generated code; set ALLOW_GENERATED_CODE=1 "
                "only on a disposable/test worker"
            )

        run_dir = WORKSPACE / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        source, source_origin = await generate_source(goal, metadata)
        compile(source, str(script_path), "exec")
        script_path.write_text(source, encoding="utf-8")

        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(script_path)],
                cwd=run_dir,
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

    await js.publish("agent.runs.completed", json.dumps(result).encode("utf-8"))
    print(json.dumps(result, indent=2), flush=True)
    await message.ack()


async def main() -> None:
    nc = await nats.connect(
        NATS_URL,
        name=f"{WORKER_ID}-{socket.gethostname()}",
    )
    js = nc.jetstream()

    subscription = await js.pull_subscribe(
        "agent.runs.created",
        stream="AGENT_RUNS",
        durable=WORKER_CONSUMER,
        config=ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=900,
            deliver_policy=DeliverPolicy.NEW,
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
        await subscription.unsubscribe()
        await nc.close()


if __name__ == "__main__":
    asyncio.run(main())
