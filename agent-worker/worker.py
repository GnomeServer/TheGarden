#!/usr/bin/env python3
"""Long-running Agent Manager worker for model-driven missions and scripts."""

import asyncio
import io
import json
import mimetypes
import os
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import sys
import unicodedata
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit
import uuid
from datetime import datetime, timezone
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
CADDY_HOST = "infra-lab-services.tail494f6d.ts.net"
CADDY_TAILSCALE_IP = "100.94.49.45"
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
MODEL_CA_FILE = os.getenv("MODEL_CA_FILE", "")
MODEL_NAME = os.getenv("MODEL_NAME", "luna")
OMP_WORKER_IMAGE = os.getenv("OMP_WORKER_IMAGE", "local/omp-worker:18.12.5")
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
WORKER_VERSION = "0.5.0"
HEARTBEAT_SECONDS = 15
_presence = {"status": "idle", "run_id": None}

def docker_model_host_args(base_url: str) -> list[str]:
    """Resolve the Caddy MagicDNS name inside bridge-network containers."""
    if urlsplit(base_url).hostname == CADDY_HOST:
        return ["--add-host", f"{CADDY_HOST}:{CADDY_TAILSCALE_IP}"]
    return []



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
        "capabilities": ["mission-crew"] + (["create-python-script"] if ALLOW_GENERATED_CODE else []),
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


def model_chat_sync(system_prompt: str, user: str, max_tokens: int = 4000) -> str:
    url = completions_url()
    payload = {
        "model": MODEL_NAME,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {MODEL_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    context = ssl._create_unverified_context() if MODEL_TLS_INSECURE and url.startswith("https://") else None
    try:
        with urllib.request.urlopen(
            request,
            timeout=MODEL_TIMEOUT,
            **({"context": context} if context is not None else {}),
        ) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"model API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach model API: {exc.reason}") from exc

    try:
        completion = json.loads(response_body)
        content = completion["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("model API returned an unexpected response") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("model response content was empty or not text")
    if len(content) > MAX_SCRIPT_CHARS * 2:
        raise RuntimeError("model response exceeded the worker output limit")
    return content.strip()


async def model_chat(system_prompt: str, user: str, max_tokens: int = 4000) -> str:
    return await asyncio.to_thread(model_chat_sync, system_prompt, user, max_tokens)


def generate_source_sync(goal: str) -> str:
    return extract_source(model_chat_sync(SYSTEM_PROMPT, goal, 5000))


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
MAX_ARTIFACT_FILES = 100
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024


def collect_artifact_files(
    work_dir: Path, artifact_paths: Any
) -> list[tuple[str, Path, int]]:
    """Validate the OMP manifest and collect its regular files safely."""
    if not isinstance(artifact_paths, list) or not artifact_paths:
        raise ValueError("OMP success must list at least one artifact path")
    if len(artifact_paths) > MAX_ARTIFACT_FILES:
        raise ValueError(f"artifact manifest exceeds {MAX_ARTIFACT_FILES} files")

    root = work_dir.resolve(strict=True)
    collected: list[tuple[str, Path, int]] = []
    seen: set[str] = set()
    total_size = 0
    for value in artifact_paths:
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or "\\" in value
        ):
            raise ValueError("artifact paths must be non-empty relative POSIX paths")
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or not relative.parts
            or relative.as_posix() != value
            or any(part in {"", ".", ".."} for part in relative.parts)
            or re.match(r"^[A-Za-z]:", relative.parts[0])
            or any(char in value for char in "\r\n")
        ):
            raise ValueError(f"invalid artifact path: {value!r}")
        if value in seen:
            raise ValueError(f"duplicate artifact path: {value!r}")
        seen.add(value)

        path = root
        for index, part in enumerate(relative.parts):
            path = path / part
            try:
                info = path.lstat()
            except OSError as exc:
                raise ValueError(f"artifact path does not exist: {value!r}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"artifact path contains a symlink: {value!r}")
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"artifact parent is not a directory: {value!r}")
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"artifact is not a regular file: {value!r}")
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError(f"artifact path escapes /work: {value!r}")
        total_size += info.st_size
        if total_size > MAX_ARTIFACT_BYTES:
            raise ValueError(
                f"artifact files exceed the {MAX_ARTIFACT_BYTES}-byte total limit"
            )
        collected.append((value, path, info.st_size))
    return collected


def package_artifacts(
    work_dir: Path, artifact_paths: Any
) -> tuple[str, str, bytes]:
    files = sorted(
        collect_artifact_files(work_dir, artifact_paths),
        key=lambda artifact: artifact[0],
    )
    contents: list[tuple[str, bytes]] = []
    total_size = 0
    for relative, path, _expected_size in files:
        with path.open("rb") as artifact_file:
            content = artifact_file.read(MAX_ARTIFACT_BYTES - total_size + 1)
        total_size += len(content)
        if total_size > MAX_ARTIFACT_BYTES:
            raise ValueError(
                f"artifact files exceed the {MAX_ARTIFACT_BYTES}-byte total limit"
            )
        contents.append((relative, content))

    if len(contents) == 1:
        relative, content = contents[0]
        filename = PurePosixPath(relative).name
        return (
            filename,
            mimetypes.guess_type(filename)[0] or "application/octet-stream",
            content,
        )

    archive = io.BytesIO()
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as bundle:
        for relative, content in contents:
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.internal_attr = 0
            info.extra = b""
            info.comment = b""
            bundle.writestr(info, content)
    payload = archive.getvalue()
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError(
            f"ZIP artifact exceeds the {MAX_ARTIFACT_BYTES}-byte upload limit"
        )
    return "artifacts.zip", "application/zip", payload


class _RejectArtifactRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def upload_artifact(
    upload_url: Any,
    upload_token: Any,
    run_id: Any,
    filename: str,
    content_type: str,
    content: bytes,
    ca_file: Path | None,
) -> dict[str, Any]:
    if (
        not isinstance(upload_url, str)
        or not isinstance(upload_token, str)
        or not isinstance(run_id, str)
        or not run_id
        or run_id in {".", ".."}
        or "/" in run_id
        or "\\" in run_id
    ):
        raise ValueError("artifact upload URL, token, and run ID are required")
    parsed_url = urlsplit(upload_url)
    try:
        port = parsed_url.port
    except ValueError:
        raise ValueError(
            "artifact upload URL must target the configured manager route"
        ) from None
    expected_path = f"/v1/omp/missions/{quote(run_id, safe='')}/artifact"
    allowed_netlocs = {CADDY_HOST.lower(), f"{CADDY_HOST.lower()}:443"}
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != CADDY_HOST.lower()
        or parsed_url.netloc.lower() not in allowed_netlocs
        or port not in (None, 443)
        or parsed_url.path != expected_path
        or "?" in upload_url
        or "#" in upload_url
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("artifact upload URL must target the configured manager route")
    if not upload_token:
        raise ValueError("artifact upload URL and token are required")
    if ca_file is None or not ca_file.is_file():
        raise ValueError("MODEL_CA_FILE is required to verify artifact upload TLS")

    request = urllib.request.Request(
        upload_url,
        data=content,
        headers={
            "Authorization": f"Bearer {upload_token}",
            "X-Artifact-Filename": filename,
            "Content-Type": content_type,
            "Accept": "application/json",
        },
        method="PUT",
    )
    context = ssl.create_default_context(cafile=str(ca_file))
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        _RejectArtifactRedirects(),
    )
    try:
        with opener.open(request, timeout=60) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"artifact upload failed with HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("artifact upload failed; verify the manager endpoint and TLS") from None
    try:
        response_json = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("artifact upload returned invalid JSON") from exc
    descriptor = response_json.get("artifact") if isinstance(response_json, dict) else None
    if not isinstance(descriptor, dict):
        raise RuntimeError("artifact upload response omitted its descriptor")
    return descriptor


async def run_mission_image(event: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """Run the mission, upload its product, then remove its temporary workspace."""
    goal = event.get("goal") if isinstance(event.get("goal"), str) else ""
    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"status": "failed", "summary": "Mission is missing run_id."}
    if not goal.strip():
        return {"status": "failed", "summary": "Mission is missing its goal."}
    if not shutil.which("docker"):
        return {
            "status": "failed",
            "summary": "The worker cannot launch the OMP job image because Docker is unavailable.",
        }
    if not MODEL_BASE_URL or not MODEL_API_KEY:
        return {
            "status": "failed",
            "summary": "The worker OMP image is missing its LiteLLM URL or virtual key.",
        }

    ca_file = Path(MODEL_CA_FILE).expanduser() if MODEL_CA_FILE.strip() else None
    if MODEL_BASE_URL.startswith("https://") and (ca_file is None or not ca_file.is_file()):
        return {
            "status": "failed",
            "summary": "The worker cannot verify the HTTPS model endpoint because its Caddy root certificate is missing.",
            "error": "Set MODEL_CA_FILE to the installed Caddy root certificate.",
        }

    crew = metadata.get("crew") if isinstance(metadata.get("crew"), dict) else {}
    mission = {
        "mission_id": run_id,
        "goal": goal.strip(),
        "repo": event.get("forgejo_repository", ""),
        "base_ref": event.get("base_ref", "main"),
        "project_name": metadata.get("project_name"),
        "crew": crew,
    }
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omp-mission-", dir=WORKSPACE) as temporary:
        run_dir = Path(temporary)
        mission_path = run_dir / "mission.json"
        work_dir = run_dir / "work"
        env_file = run_dir / "job.env"
        work_dir.mkdir()
        mission_path.write_text(json.dumps(mission, ensure_ascii=False) + "\n", encoding="utf-8")
        env_file.write_text(
            "\n".join(
                [
                    f"MODEL_BASE_URL={MODEL_BASE_URL}",
                    f"MODEL_API_KEY={MODEL_API_KEY}",
                    f"MODEL_NAME={MODEL_NAME}",
                    "OMP_TIMEOUT_SECONDS=600",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chmod(env_file, 0o600)

        command = [
            "docker", "run", "--rm",
            "--network", "bridge",
            "--read-only",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--memory", "2g",
            "--cpus", "2",
            "--pids-limit", "128",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
            "--env-file", str(env_file),
            "--mount", f"type=bind,src={mission_path},dst=/mission.json,readonly",
            "--mount", f"type=bind,src={work_dir},dst=/work",
        ]
        command.extend(docker_model_host_args(MODEL_BASE_URL))
        if ca_file is not None and ca_file.is_file():
            command.extend([
                "-e", "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/caddy-root.crt",
                "--mount", f"type=bind,src={ca_file},dst=/etc/ssl/certs/caddy-root.crt,readonly",
            ])
        command.append(OMP_WORKER_IMAGE)
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                timeout=660,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "failed",
                "summary": "The worker OMP image exceeded its 660-second limit.",
            }
        line = next(
            (item for item in reversed(completed.stdout.splitlines()) if item.startswith("{")),
            "",
        )
        if not line:
            return {
                "status": "failed",
                "summary": "Worker OMP image exited without a result.",
                "error": (completed.stderr or completed.stdout or "No worker output")[-1200:],
            }
        try:
            result = json.loads(line)
        except json.JSONDecodeError as exc:
            return {
                "status": "failed",
                "summary": "Worker OMP image returned invalid JSON.",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(result, dict):
            return {"status": "failed", "summary": "Worker OMP image returned a non-object result."}
        if result.get("mission_id") not in (None, run_id):
            return {"status": "failed", "summary": "Worker OMP result has a mismatched mission_id."}
        result["mission_id"] = run_id
        result.setdefault("summary", "Worker OMP finished.")
        result.pop("script", None)
        result.pop("artifact", None)
        if result.get("status") == "completed":
            filename, content_type, content = await asyncio.to_thread(
                package_artifacts, work_dir, result.get("artifacts")
            )
            descriptor = await asyncio.to_thread(
                upload_artifact,
                metadata.get("artifact_upload_url"),
                metadata.get("artifact_upload_token"),
                run_id,
                filename,
                content_type,
                content,
                ca_file,
            )
            result["artifact"] = descriptor
        result.pop("artifacts", None)
        return result


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


    if metadata.get("operation") == "create-python-script":
        result["script"] = None

    try:
        _presence["status"] = "busy"
        _presence["run_id"] = run_id
        status_payload = running_status_event(run_id)
        encoded_status = json.dumps(status_payload).encode("utf-8")
        await js.publish("agent.runs.status", encoded_status)
        await js.publish("agent.runs.started", encoded_status)

        if metadata.get("operation") == "mission":
            mission_result = await run_mission_image(event, metadata)
            if mission_result.get("status") == "bounced":
                mission_result["error"] = (
                    mission_result.get("error")
                    or mission_result.get("summary")
                    or "Worker mission was bounced."
                )
                mission_result["status"] = "failed"
            result.update(mission_result)
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
        # Model, validation, and workspace failures must be terminal and visible.
        detail = str(exc)[:1000]
        result["error"] = detail
        if metadata.get("operation") == "mission":
            result["summary"] = f"Worker mission failed: {detail[:500]}"
    result.setdefault("completed_at", datetime.now(timezone.utc).isoformat())

    try:
        await js.publish("agent.runs.completed", json.dumps(result).encode("utf-8"))
        print(json.dumps(result, indent=2), flush=True)
        await message.ack()
    finally:
        _presence["status"] = "idle"
        _presence["run_id"] = None


async def respond_to_health(message: Any, publish: Any) -> None:
    try:
        body = json.loads(message.data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(body, dict):
        return
    target = body.get("worker_id")
    if target and target != WORKER_ID:
        return
    reply = getattr(message, "reply", None)
    if reply:
        payload = {"worker_id": WORKER_ID, "worker_role": WORKER_ROLE}
        await publish(reply, json.dumps(payload).encode("utf-8"))


async def main() -> None:
    nc = await nats.connect(
        NATS_URL,
        name=f"{WORKER_ID}-{socket.gethostname()}",
    )
    js = nc.jetstream()

    async def reply_health(message: Any) -> None:
        await respond_to_health(message, nc.publish)

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
