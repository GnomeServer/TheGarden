#!/usr/bin/env python3
"""Long-running Agent Manager worker for model-driven missions and scripts."""

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

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

KEYBROKER_URL = os.getenv(
    "KEYBROKER_URL",
    "https://infra-lab-services.tail494f6d.ts.net/keybroker",
).rstrip("/")
KEYBROKER_TIMEOUT = float(os.getenv("KEYBROKER_TIMEOUT", "15"))
CADDY_HOST = "infra-lab-services.tail494f6d.ts.net"
CADDY_TAILSCALE_IP = "100.94.49.45"
MODEL_CA_FILE = os.getenv("MODEL_CA_FILE", "")
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
    "true",
    "yes",
}
WORKER_VERSION = "0.5.0"
HEARTBEAT_SECONDS = 15
_presence = {"status": "idle", "run_id": None}
ACTIVE_MISSIONS: dict[str, tuple[str, asyncio.Event]] = {}
CANCEL_POLL_INTERVAL_SECONDS = 1.0

def docker_model_host_args(base_url: str) -> list[str]:
    """Resolve the Caddy MagicDNS name inside bridge-network containers."""
    if urlsplit(base_url).hostname == CADDY_HOST:
        return ["--add-host", f"{CADDY_HOST}:{CADDY_TAILSCALE_IP}"]
    return []


REDACTED_CREDENTIAL = "[REDACTED CREDENTIAL]"
CREDENTIAL_PATTERN = re.compile(
    r"sk-or-v1-[A-Fa-f0-9]{20,}|"
    r"sk-ant-[A-Za-z0-9_-]{20,}|"
    r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|"
    r"AIza[0-9A-Za-z_-]{35,}"
)


class CredentialExposure(RuntimeError):
    """Raised after a credential-like value is detected in run text."""


def redact_credentials(text: str, session_key: str | None = None) -> tuple[str, bool, bool]:
    """Redact known credential forms and report whether the session key matched."""
    session_key_match = bool(session_key and session_key in text)
    found = session_key_match or CREDENTIAL_PATTERN.search(text) is not None
    if session_key:
        text = text.replace(session_key, REDACTED_CREDENTIAL)
    return CREDENTIAL_PATTERN.sub(REDACTED_CREDENTIAL, text), found, session_key_match


def keybroker_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    url = f"{KEYBROKER_URL}{path}"
    if urlsplit(url).scheme != "https":
        raise RuntimeError("KEYBROKER_URL must use HTTPS")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    context = ssl.create_default_context(cafile=MODEL_CA_FILE.strip() or None)
    try:
        with urllib.request.urlopen(
            request, timeout=KEYBROKER_TIMEOUT, context=context
        ) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Keybroker returned HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("could not reach Keybroker") from None
    if not response_body:
        return {}
    try:
        response_data = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Keybroker returned an invalid response") from None
    if not isinstance(response_data, dict):
        raise RuntimeError("Keybroker returned an invalid response")
    return response_data


async def validate_keybroker_worker_identity() -> None:
    identity = await asyncio.to_thread(
        keybroker_request, "GET", "/v1/worker/identity"
    )
    if identity.get("worker_id") != WORKER_ID:
        raise RuntimeError("Keybroker worker identity did not match WORKER_ID")


async def request_keybroker_session(run_id: str, operation: str) -> dict[str, str]:
    await validate_keybroker_worker_identity()
    response = await asyncio.to_thread(
        keybroker_request,
        "POST",
        "/v1/sessions",
        {"run_id": run_id, "operation": operation},
    )
    required = (
        "session_id",
        "key",
        "key_id",
        "key_alias",
        "expires_at",
        "model_base_url",
        "model",
    )
    if any(not isinstance(response.get(name), str) or not response[name] for name in required):
        session_id = response.get("session_id")
        if isinstance(session_id, str) and session_id:
            try:
                await revoke_keybroker_session(session_id)
            except Exception:
                pass
        raise RuntimeError("Keybroker returned an incomplete session")
    session = {name: response[name] for name in required}
    session["model_base_url"] = session["model_base_url"].rstrip("/")
    return session


async def revoke_keybroker_session(session_id: str) -> bool:
    response = await asyncio.to_thread(
        keybroker_request, "DELETE", f"/v1/sessions/{session_id}"
    )
    return response.get("revoked") is not False


async def report_key_exposure(
    session_id: str, *, session_key_match: bool
) -> tuple[bool, bool | None]:
    reason = (
        "session_key_echo_detected"
        if session_key_match
        else "credential_pattern_detected"
    )
    response = await asyncio.to_thread(
        keybroker_request,
        "POST",
        f"/v1/sessions/{session_id}/exposure",
        {
            "event_id": str(uuid.uuid4()),
            "reason": reason,
            "session_key_match": session_key_match,
        },
    )
    revoked = response.get("revoked")
    return (
        response.get("stop_run") is True,
        revoked if isinstance(revoked, bool) else None,
    )


async def inspect_value(value: Any, inspect_text: Any) -> None:
    if isinstance(value, str):
        await inspect_text(value)
    elif isinstance(value, list):
        for item in value:
            await inspect_value(item, inspect_text)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                await inspect_text(key)
            await inspect_value(item, inspect_text)


def make_session_model_call(
    session: dict[str, str],
) -> tuple[Any, Any, dict[str, bool | None]]:
    lock = asyncio.Lock()
    exposure_detected = False
    alert_state = {"reported": False, "revoked": None}
    session_key = session.pop("key")

    async def flag_exposure(session_key_match: bool) -> None:
        nonlocal exposure_detected
        exposure_detected = True
        try:
            stop_run, revoked = await report_key_exposure(
                session["session_id"], session_key_match=session_key_match
            )
            alert_state["reported"] = stop_run
            alert_state["revoked"] = revoked
        except Exception:
            # The session is also revoked by the caller's finally block.
            pass
        raise CredentialExposure("credential exposure detected")

    async def inspect_text(text: str) -> str:
        async with lock:
            if exposure_detected:
                raise CredentialExposure("credential exposure detected")
            cleaned, found, session_key_match = redact_credentials(text, session_key)
            if found:
                await flag_exposure(session_key_match)
            return cleaned

    async def model_call(
        system_prompt: str, user: str, max_tokens: int = 4000
    ) -> str:
        async with lock:
            if exposure_detected:
                raise CredentialExposure("credential exposure detected")
            clean_system, system_found, system_match = redact_credentials(
                system_prompt, session_key
            )
            clean_user, user_found, user_match = redact_credentials(user, session_key)
            if system_found or user_found:
                await flag_exposure(system_match or user_match)
            response = await model_chat(
                clean_system,
                clean_user,
                max_tokens,
                api_key=session_key,
                model_base_url=session["model_base_url"],
                model=session["model"],
            )
            clean_response, found, session_key_match = redact_credentials(
                response, session_key
            )
            if found:
                await flag_exposure(session_key_match)
            return clean_response

    return model_call, inspect_text, alert_state


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


def completions_url(model_base_url: str) -> str:
    if not model_base_url:
        raise RuntimeError("Keybroker session omitted the model endpoint")
    return f"{model_base_url.rstrip('/')}/chat/completions"


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


def model_chat_sync(
    system_prompt: str,
    user: str,
    max_tokens: int,
    *,
    api_key: str,
    model_base_url: str,
    model: str,
) -> str:
    url = completions_url(model_base_url)
    payload = {
        "model": model,
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
            "Authorization": f"Bearer {api_key}",
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
        raise RuntimeError(f"model API returned HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("could not reach model API") from None

    try:
        completion = json.loads(response_body)
        content = completion["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise RuntimeError("model API returned an unexpected response") from None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("model response content was empty or not text")
    if len(content) > MAX_SCRIPT_CHARS * 2:
        raise RuntimeError("model response exceeded the worker output limit")
    return content.strip()


async def model_chat(
    system_prompt: str,
    user: str,
    max_tokens: int = 4000,
    *,
    api_key: str,
    model_base_url: str,
    model: str,
) -> str:
    return await asyncio.to_thread(
        model_chat_sync,
        system_prompt,
        user,
        max_tokens,
        api_key=api_key,
        model_base_url=model_base_url,
        model=model,
    )


async def generate_source(
    goal: str,
    metadata: dict[str, Any],
    model_call: Any,
) -> tuple[str, str]:
    # A supplied script is useful for deterministic pipeline tests. Dynamic
    # prompts use the model when metadata does not contain script content.
    supplied_script = metadata.get("script")
    if supplied_script is not None:
        if not isinstance(supplied_script, str) or not supplied_script.strip():
            raise RuntimeError("metadata.script must be a non-empty string")
        return extract_source(supplied_script), "provided"

    source = extract_source(await model_call(SYSTEM_PROMPT, goal, 5000))
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


async def inspect_mission_files(
    work_dir: Path, relative_paths: Any, inspect_text: Any
) -> None:
    if not relative_paths:
        return
    files = await asyncio.to_thread(
        collect_artifact_files, work_dir, relative_paths
    )
    for _relative, artifact_path, expected_size in files:
        artifact_content = await asyncio.to_thread(artifact_path.read_bytes)
        if len(artifact_content) != expected_size:
            raise ValueError("artifact changed while scanning credentials")
        await inspect_text(artifact_content.decode("utf-8", errors="ignore"))


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


def _checkpoint_file_paths(work_dir: Path) -> list[str]:
    root = work_dir.resolve(strict=True)
    paths: list[str] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise ValueError("mission checkpoint contains a symlinked directory")
        for name in filenames:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("mission checkpoint contains a non-regular file")
            paths.append(path.relative_to(root).as_posix())
    return paths


def mission_checkpoint_path(run_id: str) -> Path:
    try:
        canonical_id = str(uuid.UUID(run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("mission checkpoint requires a UUID run_id") from exc
    return WORKSPACE / ".pending-missions" / canonical_id


def validate_mission_checkpoint(run_id: str) -> Path:
    checkpoint = mission_checkpoint_path(run_id)
    if checkpoint.is_symlink() or not checkpoint.is_dir():
        raise ValueError("mission clarification checkpoint is missing")
    paths = _checkpoint_file_paths(checkpoint)
    if paths:
        collect_artifact_files(checkpoint, paths)
    return checkpoint


def save_mission_checkpoint(work_dir: Path, run_id: str) -> Path:
    paths = _checkpoint_file_paths(work_dir)
    if paths:
        files = collect_artifact_files(work_dir, paths)
    else:
        files = []
    checkpoint = mission_checkpoint_path(run_id)
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if checkpoint.exists() or checkpoint.is_symlink():
        raise ValueError("mission clarification checkpoint already exists")
    checkpoint.mkdir(mode=0o700)
    try:
        for relative, source, _size in files:
            destination = checkpoint.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, destination, follow_symlinks=False)
    except Exception:
        shutil.rmtree(checkpoint, ignore_errors=True)
        raise
    return checkpoint


def remove_mission_checkpoint(run_id: str) -> None:
    checkpoint = mission_checkpoint_path(run_id)
    if checkpoint.is_symlink():
        checkpoint.unlink()
    elif checkpoint.is_dir():
        shutil.rmtree(checkpoint)


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


def download_parent_artifact(
    artifact_input: Any,
    ca_file: Path | None,
) -> bytes:
    """Fetch a signed source ZIP only from the configured Manager endpoint."""
    if not isinstance(artifact_input, dict):
        raise ValueError("modification mission omitted its source artifact")
    parent_id = artifact_input.get("parent_mission_id")
    artifact_id = artifact_input.get("artifact_id")
    download_url = artifact_input.get("download_url")
    filename = artifact_input.get("filename")
    content_type = artifact_input.get("content_type")
    expected_size = artifact_input.get("size_bytes")
    expected_digest = artifact_input.get("sha256")
    try:
        if str(uuid.UUID(parent_id)) != parent_id or str(uuid.UUID(artifact_id)) != artifact_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("source artifact identifiers are invalid") from None
    if (
        not isinstance(download_url, str)
        or not isinstance(filename, str)
        or not filename.casefold().endswith(".zip")
        or content_type != "application/zip"
        or type(expected_size) is not int
        or not 0 < expected_size <= MAX_ARTIFACT_BYTES
        or not isinstance(expected_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
    ):
        raise ValueError("source artifact descriptor is invalid")

    parsed_url = urlsplit(download_url)
    try:
        port = parsed_url.port
        query = parse_qs(parsed_url.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError("source artifact URL is invalid") from None
    expected_path = (
        f"/v1/omp/missions/{quote(parent_id, safe='')}/artifacts/"
        f"{quote(artifact_id, safe='')}/download"
    )
    allowed_netlocs = {CADDY_HOST.lower(), f"{CADDY_HOST.lower()}:443"}
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != CADDY_HOST.lower()
        or parsed_url.netloc.lower() not in allowed_netlocs
        or port not in (None, 443)
        or parsed_url.path != expected_path
        or set(query) != {"expires", "signature"}
        or any(len(values) != 1 for values in query.values())
        or not query["expires"][0].isdigit()
        or not re.fullmatch(r"[0-9a-f]{64}", query["signature"][0])
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
    ):
        raise ValueError("source artifact URL must target the signed Manager download route")
    if ca_file is None or not ca_file.is_file():
        raise ValueError("MODEL_CA_FILE is required to verify source artifact TLS")

    request = urllib.request.Request(
        download_url,
        headers={"Accept": "application/zip"},
        method="GET",
    )
    context = ssl.create_default_context(cafile=str(ca_file))
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        _RejectArtifactRedirects(),
    )
    try:
        with opener.open(request, timeout=60) as response:
            content = response.read(MAX_ARTIFACT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"source artifact download failed with HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("source artifact download failed; verify Manager reachability and TLS") from None
    if len(content) != expected_size or len(content) > MAX_ARTIFACT_BYTES:
        raise ValueError("source artifact size does not match its descriptor")
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise ValueError("source artifact checksum does not match its descriptor")
    return content


def extract_artifact_zip(content: bytes, work_dir: Path) -> list[str]:
    """Safely unpack a bounded Manager ZIP into a fresh mission workspace."""
    if not content or len(content) > MAX_ARTIFACT_BYTES:
        raise ValueError("source artifact ZIP is empty or exceeds the size limit")
    root = work_dir.resolve(strict=True)
    names: list[str] = []
    seen: set[str] = set()
    total_size = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARTIFACT_FILES:
                raise ValueError("source artifact ZIP has an invalid file count")
            for info in infos:
                name = info.filename
                relative = PurePosixPath(name)
                if (
                    not name
                    or "\x00" in name
                    or "\\" in name
                    or relative.is_absolute()
                    or not relative.parts
                    or relative.as_posix() != name
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or re.match(r"^[A-Za-z]:", relative.parts[0])
                    or any(char in name for char in "\r\n")
                    or info.is_dir()
                    or info.flag_bits & 0x1
                    or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)
                    or name in seen
                ):
                    raise ValueError(f"source artifact contains an unsafe path: {name!r}")
                seen.add(name)
                if info.file_size < 0 or info.file_size > MAX_ARTIFACT_BYTES:
                    raise ValueError("source artifact ZIP contains an oversized file")
                total_size += info.file_size
                if total_size > MAX_ARTIFACT_BYTES:
                    raise ValueError("source artifact ZIP exceeds the expanded size limit")

                destination = root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not destination.parent.resolve(strict=True).is_relative_to(root):
                    raise ValueError("source artifact path escapes its workspace")
                written = 0
                with archive.open(info) as source, destination.open("xb") as target:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size or written > MAX_ARTIFACT_BYTES:
                            raise ValueError("source artifact expanded beyond its declared size")
                        target.write(chunk)
                if written != info.file_size:
                    raise ValueError("source artifact file size does not match its ZIP entry")
                names.append(name)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("source artifact is not a valid ZIP archive") from exc
    return names


def get_mission_control(
    control_url: Any,
    upload_token: Any,
    run_id: str,
    ca_file: Path | None,
) -> dict[str, Any]:
    if not isinstance(control_url, str) or not isinstance(upload_token, str):
        raise ValueError("mission control URL and upload token are required")
    parsed_url = urlsplit(control_url)
    try:
        port = parsed_url.port
    except ValueError:
        raise ValueError("mission control URL must target the configured manager route") from None
    expected_path = f"/v1/omp/missions/{quote(run_id, safe='')}/control"
    allowed_netlocs = {CADDY_HOST.lower(), f"{CADDY_HOST.lower()}:443"}
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != CADDY_HOST.lower()
        or parsed_url.netloc.lower() not in allowed_netlocs
        or port not in (None, 443)
        or parsed_url.path != expected_path
        or parsed_url.query
        or parsed_url.fragment
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise ValueError("mission control URL must target the configured manager route")
    if ca_file is None or not ca_file.is_file():
        raise ValueError("MODEL_CA_FILE is required to verify mission control TLS")
    request = urllib.request.Request(
        control_url,
        headers={
            "Authorization": f"Bearer {upload_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    context = ssl.create_default_context(cafile=str(ca_file))
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        _RejectArtifactRedirects(),
    )
    try:
        with opener.open(request, timeout=10) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"mission control check failed with HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("mission control check failed; verify manager route and TLS") from None
    try:
        result = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("mission control returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("mission_id") != run_id:
        raise RuntimeError("mission control returned a mismatched run")
    return result


def stop_omp_container(container_name: str) -> None:
    for _attempt in range(20):
        completed = subprocess.run(
            ["docker", "stop", "--time", "5", container_name],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            return
        detail = f"{completed.stdout}\n{completed.stderr}".lower()
        if "no such container" not in detail and "not found" not in detail:
            raise RuntimeError("Docker could not stop the cancelled OMP container")
        time.sleep(0.1)
    raise RuntimeError("cancelled OMP container did not become available to stop")


async def cancel_active_mission(run_id: str) -> bool:
    active = ACTIVE_MISSIONS.get(run_id)
    if active is None:
        return False
    container_name, cancellation = active
    if cancellation.is_set():
        return True
    cancellation.set()
    try:
        await asyncio.to_thread(stop_omp_container, container_name)
    except Exception as exc:
        print(f"failed to stop cancelled OMP container: {type(exc).__name__}", flush=True)
    return True


async def run_mission_image(
    event: dict[str, Any],
    metadata: dict[str, Any],
    *,
    model_session: dict[str, str] | None = None,
    inspect_text: Any = None,
) -> dict[str, Any]:
    """Run a cancellable OMP container, upload its product, then remove its workspace."""
    session_key = (
        model_session.pop("key", None)
        if isinstance(model_session, dict)
        else None
    )
    model_base_url = (
        model_session.get("model_base_url")
        if isinstance(model_session, dict)
        else None
    )
    model = (
        model_session.get("model")
        if isinstance(model_session, dict)
        else None
    )
    if (
        not isinstance(session_key, str)
        or not session_key
        or not isinstance(model_base_url, str)
        or not model_base_url
        or not isinstance(model, str)
        or not model
    ):
        return {
            "status": "failed",
            "summary": "The worker did not receive a complete Keybroker session.",
        }
    model_base_url = model_base_url.rstrip("/")
    model = model.strip()
    if not model_base_url or not model:
        return {
            "status": "failed",
            "summary": "The worker did not receive a complete Keybroker session.",
        }
    goal = event.get("goal") if isinstance(event.get("goal"), str) else ""
    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"status": "failed", "summary": "Mission is missing run_id."}
    if not goal.strip():
        return {"status": "failed", "summary": "Mission is missing its goal."}
    resume_from_checkpoint = metadata.get("resume_from_checkpoint") is True
    checkpoint_retained = False

    parent_mission_id = metadata.get("parent_mission_id")
    artifact_input = metadata.get("artifact_input")
    if (parent_mission_id is None) != (artifact_input is None):
        return {
            "status": "failed",
            "summary": "The worker received incomplete artifact modification metadata.",
        }
    if artifact_input is not None and (
        not isinstance(parent_mission_id, str)
        or not isinstance(artifact_input, dict)
        or artifact_input.get("parent_mission_id") != parent_mission_id
        or resume_from_checkpoint
    ):
        return {
            "status": "failed",
            "summary": "The worker received invalid artifact modification metadata.",
        }

    ca_file = Path(MODEL_CA_FILE).expanduser() if MODEL_CA_FILE.strip() else None
    if ca_file is None or not ca_file.is_file():
        return {
            "status": "failed",
            "summary": "The worker cannot verify Agent Manager's cancellation endpoint because its Caddy root certificate is missing.",
        }
    upload_url = metadata.get("artifact_upload_url")
    control_url = metadata.get("artifact_control_url")
    if not isinstance(control_url, str) and isinstance(upload_url, str):
        control_url = f"{upload_url.rsplit('/', 1)[0]}/control"
    upload_token = metadata.get("artifact_upload_token")
    try:
        control = await asyncio.to_thread(
            get_mission_control, control_url, upload_token, run_id, ca_file
        )
    except Exception as exc:
        return {
            "status": "failed",
            "summary": "The worker could not verify the mission's cancellation state.",
            "error": f"{type(exc).__name__}: {exc}"[:1000],
        }
    control_status = control.get("status")
    if control.get("cancel_requested") or control_status in {
        "cancel_requested",
        "cancelled",
    }:
        if resume_from_checkpoint:
            remove_mission_checkpoint(run_id)
        return {
            "status": "cancelled",
            "summary": "Mission cancelled at the user's request.",
        }
    if control_status == "needs_input":
        return {
            "status": "needs_input",
            "summary": str(control.get("summary") or "Clarification is still required."),
            "question": str(control.get("question") or "Please clarify the unresolved review feedback."),
            "reviewer_approved": False,
            "reviewer_count": control.get("reviewer_count", 0),
            "artifacts": [],
        }
    if control_status not in {"queued", "running"}:
        return {
            "status": "failed",
            "summary": f"Mission cannot resume from Manager status {control_status!r}.",
        }
    if not shutil.which("docker"):
        return {
            "status": "failed",
            "summary": "The worker cannot launch the OMP job image because Docker is unavailable.",
        }
    if model_base_url.startswith("https://") and not ca_file.is_file():
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
        "clarification": metadata.get("clarification"),
        "reviewer_feedback": metadata.get("reviewer_feedback"),
        "reviewer_count_start": 0,
        "parent_mission_id": parent_mission_id,
        "modifies_existing_artifact": artifact_input is not None,
    }
    if inspect_text is not None:
        await inspect_value(mission, inspect_text)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omp-mission-", dir=WORKSPACE) as temporary:
        run_dir = Path(temporary)
        mission_path = run_dir / "mission.json"
        checkpoint_dir = mission_checkpoint_path(run_id) if resume_from_checkpoint else None
        if resume_from_checkpoint:
            try:
                work_dir = validate_mission_checkpoint(run_id)
            except (OSError, ValueError) as exc:
                return {
                    "status": "failed",
                    "summary": "The worker could not restore the mission checkpoint.",
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
        else:
            work_dir = run_dir / "work"
            work_dir.mkdir()
            if artifact_input is not None:
                try:
                    parent_archive = await asyncio.to_thread(
                        download_parent_artifact, artifact_input, ca_file
                    )
                    await asyncio.to_thread(
                        extract_artifact_zip, parent_archive, work_dir
                    )
                except Exception as exc:
                    return {
                        "status": "failed",
                        "summary": "The worker could not load the completed artifact for modification.",
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
        if inspect_text is not None:
            workspace_files = await asyncio.to_thread(
                _checkpoint_file_paths, work_dir
            )
            await inspect_mission_files(work_dir, workspace_files, inspect_text)
        mission_path.write_text(
            json.dumps(mission, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        env_file = run_dir / "job.env"
        if checkpoint_dir is not None and checkpoint_dir.is_symlink():
            return {
                "status": "failed",
                "summary": "The mission checkpoint path is unsafe.",
            }
        env_file.write_text(
            "\n".join(
                [
                    f"MODEL_BASE_URL={model_base_url}",
                    f"MODEL_API_KEY={session_key}",
                    f"OMP_MODEL=worker-litellm/{model}",
                    "OMP_TIMEOUT_SECONDS=600",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chmod(env_file, 0o600)
        del session_key

        container_name = f"omp-mission-{uuid.uuid5(uuid.NAMESPACE_URL, run_id).hex[:20]}"
        cancellation = asyncio.Event()
        ACTIVE_MISSIONS[run_id] = (container_name, cancellation)
        command = [
            "docker", "run", "--rm", "--name", container_name,
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
        command.extend(docker_model_host_args(model_base_url))
        command.extend([
            "-e", "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/caddy-root.crt",
            "--mount", f"type=bind,src={ca_file},dst=/etc/ssl/certs/caddy-root.crt,readonly",
        ])
        command.append(OMP_WORKER_IMAGE)

        async def watch_cancellation() -> None:
            while not cancellation.is_set():
                await asyncio.sleep(CANCEL_POLL_INTERVAL_SECONDS)
                if cancellation.is_set():
                    return
                try:
                    state = await asyncio.to_thread(
                        get_mission_control, control_url, upload_token, run_id, ca_file
                    )
                except Exception:
                    continue
                if state.get("cancel_requested") or state.get("status") in {
                    "cancel_requested",
                    "cancelled",
                }:
                    cancellation.set()
                    try:
                        await asyncio.to_thread(stop_omp_container, container_name)
                    except Exception as exc:
                        print(
                            f"failed to stop cancelled OMP container: {type(exc).__name__}",
                            flush=True,
                        )
                    return

        watcher = asyncio.create_task(watch_cancellation())
        try:
            if artifact_input is not None:
                source_control = await asyncio.to_thread(
                    get_mission_control, control_url, upload_token, run_id, ca_file
                )
                if (
                    source_control.get("cancel_requested")
                    or source_control.get("status") in {"cancel_requested", "cancelled"}
                ):
                    return {
                        "status": "cancelled",
                        "summary": "Mission cancelled at the user's request.",
                    }
            if cancellation.is_set():
                return {
                    "status": "cancelled",
                    "summary": "Mission cancelled at the user's request.",
                }
            try:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=660,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if not cancellation.is_set():
                    try:
                        await asyncio.to_thread(
                            stop_omp_container, container_name
                        )
                    except Exception:
                        pass
                if inspect_text is not None:
                    for output in (exc.stdout, exc.stderr):
                        if isinstance(output, bytes):
                            output = output.decode("utf-8", errors="replace")
                        if isinstance(output, str):
                            await inspect_text(output)
                if cancellation.is_set():
                    return {
                        "status": "cancelled",
                        "summary": "Mission cancelled at the user's request.",
                    }
                return {
                    "status": "failed",
                    "summary": "The worker OMP image exceeded its 660-second limit.",
                }
            if inspect_text is not None:
                await inspect_text(completed.stdout or "")
                await inspect_text(completed.stderr or "")
            if cancellation.is_set():
                return {
                    "status": "cancelled",
                    "summary": "Mission cancelled at the user's request.",
                }
            line = next(
                (
                    item
                    for item in reversed((completed.stdout or "").splitlines())
                    if item.startswith("{")
                ),
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
                return {
                    "status": "failed",
                    "summary": "Worker OMP image returned a non-object result.",
                }
            if result.get("mission_id") not in (None, run_id):
                return {
                    "status": "failed",
                    "summary": "Worker OMP result has a mismatched mission_id.",
                }
            result["mission_id"] = run_id
            result.setdefault("summary", "Worker OMP finished.")
            if inspect_text is not None:
                await inspect_value(result, inspect_text)
            result.pop("script", None)
            result.pop("artifact", None)
            if result.get("status") == "needs_input":
                if inspect_text is not None:
                    checkpoint_files = await asyncio.to_thread(
                        _checkpoint_file_paths, work_dir
                    )
                    await inspect_mission_files(
                        work_dir, checkpoint_files, inspect_text
                    )
                if not resume_from_checkpoint:
                    save_mission_checkpoint(work_dir, run_id)
                checkpoint_retained = True
                return result
            if result.get("status") == "completed":
                current_control = await asyncio.to_thread(
                    get_mission_control, control_url, upload_token, run_id, ca_file
                )
                if (
                    cancellation.is_set()
                    or current_control.get("cancel_requested")
                    or current_control.get("status") in {"cancel_requested", "cancelled"}
                ):
                    return {
                        "status": "cancelled",
                        "summary": "Mission cancelled at the user's request.",
                    }
                if inspect_text is not None:
                    await inspect_mission_files(
                        work_dir, result.get("artifacts"), inspect_text
                    )
                filename, content_type, content = await asyncio.to_thread(
                    package_artifacts, work_dir, result.get("artifacts")
                )
                descriptor = await asyncio.to_thread(
                    upload_artifact,
                    upload_url,
                    upload_token,
                    run_id,
                    filename,
                    content_type,
                    content,
                    ca_file,
                )
                result["artifact"] = descriptor
            result.pop("artifacts", None)
            return result
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            ACTIVE_MISSIONS.pop(run_id, None)
            if resume_from_checkpoint and not checkpoint_retained:
                remove_mission_checkpoint(run_id)


async def process_message(js: Any, message: Any) -> None:
    event = json.loads(message.data.decode("utf-8"))
    metadata = event.get("metadata") or {}

    # Dynamic tasks omit worker_id and are assigned by the shared NATS
    # consumer. A worker_id is still honored for explicitly targeted jobs.
    target_worker = metadata.get("worker_id")
    if target_worker and target_worker != WORKER_ID:
        raise RuntimeError(f"task is for {target_worker!r}, not {WORKER_ID!r}")

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
            "summary": "The run expired before a worker could start it.",
            "error": "run delivery expired before worker pickup",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        await js.publish("agent.runs.completed", json.dumps(result).encode("utf-8"))
        print(json.dumps(result, indent=2), flush=True)
        await message.ack()
        return
    result["started_at"] = datetime.now(timezone.utc).isoformat()

    session: dict[str, str] | None = None
    inspect_text = None
    alert_state: dict[str, bool | None] = {"reported": False, "revoked": None}
    security_exposed = False
    revoke_failed = False
    operation = metadata.get("operation")

    if metadata.get("operation") == "create-python-script":
        result["script"] = None

    try:
        _presence["status"] = "busy"
        _presence["run_id"] = run_id
        status_payload = running_status_event(run_id)
        encoded_status = json.dumps(status_payload).encode("utf-8")
        await js.publish("agent.runs.status", encoded_status)
        await js.publish("agent.runs.started", encoded_status)

        if operation not in {"mission", "create-python-script"}:
            raise RuntimeError("unsupported worker operation")
        if operation == "create-python-script" and not ALLOW_GENERATED_CODE:
            raise RuntimeError(
                "refusing to execute generated code; set ALLOW_GENERATED_CODE=1 "
                "only on a disposable/test worker"
            )

        session = dict(await request_keybroker_session(run_id, operation))
        mission_session = dict(session) if operation == "mission" else None
        model_call, inspect_text, alert_state = make_session_model_call(session)

        if operation == "mission":
            mission_result = await run_mission_image(
                event,
                metadata,
                model_session=mission_session,
                inspect_text=inspect_text,
            )
            mission_session.clear()
            if mission_result.get("status") == "bounced":
                mission_result["error"] = (
                    mission_result.get("error")
                    or mission_result.get("summary")
                    or "Worker mission was bounced."
                )
                mission_result["status"] = "failed"
            result.update(mission_result)
        else:
            goal = event.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise RuntimeError("event is missing a non-empty goal")
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
            source, source_origin = await generate_source(goal, metadata, model_call)
            source = await inspect_text(source)
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
                stdout = await inspect_text(completed.stdout)
                stderr = await inspect_text(completed.stderr)
                result.update(
                    {
                        "status": "completed" if completed.returncode == 0 else "failed",
                        "returncode": completed.returncode,
                        "stdout": stdout[:MAX_OUTPUT_CHARS],
                        "stderr": stderr[:MAX_OUTPUT_CHARS],
                    }
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                stdout = await inspect_text(stdout)
                stderr = await inspect_text(stderr)
                result.update(
                    {
                        "status": "failed",
                        "returncode": None,
                        "error": f"script exceeded {SCRIPT_TIMEOUT:g} seconds",
                        "stdout": stdout[:MAX_OUTPUT_CHARS],
                        "stderr": stderr[:MAX_OUTPUT_CHARS],
                    }
                )
            result["source"] = source_origin
    except CredentialExposure:
        security_exposed = True
        result.update({
            "status": "failed",
            "script": None,
            "summary": "Run stopped because a potential credential exposure was detected.",
        })
        for field in ("artifacts", "evidence", "stdout", "stderr", "source", "error"):
            result.pop(field, None)
    except Exception as exc:
        detail = str(exc)
        found = False
        if inspect_text:
            try:
                detail = await inspect_text(detail)
            except CredentialExposure:
                found = True
        else:
            detail, found, _ = redact_credentials(detail)
        if found:
            security_exposed = True
            result.update({
                "status": "failed",
                "script": None,
                "summary": "Run stopped because a potential credential exposure was detected.",
            })
            for field in ("artifacts", "evidence", "stdout", "stderr", "source", "error"):
                result.pop(field, None)
        else:
            result["error"] = detail[:1000]
            if operation == "mission":
                result["summary"] = f"Worker mission failed: {detail[:500]}"

    finally:
        if session:
            if not security_exposed and inspect_text:
                try:
                    await inspect_value(result, inspect_text)
                except CredentialExposure:
                    security_exposed = True
                    run_id_text, _, _ = redact_credentials(run_id)
                    result = {
                        "event": "run.completed",
                        "run_id": run_id_text,
                        "worker_id": WORKER_ID,
                        "hostname": socket.gethostname(),
                        "status": "failed",
                        "script": None,
                        "started_at": result.get("started_at"),
                        "summary": "Run stopped because a potential credential exposure was detected.",
                    }

            try:
                delete_confirmed = await revoke_keybroker_session(
                    session["session_id"]
                )
                alert_state["revoked"] = (
                    delete_confirmed or alert_state["revoked"] is True
                )
                revoke_failed = alert_state["revoked"] is not True
            except Exception:
                revoke_failed = alert_state["revoked"] is not True

    if security_exposed:
        notice = (
            "Potential credential exposure detected. Further model calls were stopped."
        )
        if revoke_failed:
            notice += " Session-key revocation could not be confirmed; an administrator must revoke it."
        elif session:
            notice += " The session key was revoked."
        else:
            notice += " No session key was issued."
        if not alert_state["reported"]:
            notice += " Administrator alert delivery could not be confirmed."
        result["security_notice"] = notice
    elif revoke_failed:
        result.update({
            "status": "failed",
            "summary": "The run ended, but session-key revocation could not be confirmed.",
            "security_notice": "An administrator must revoke the worker session key.",
        })

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


async def respond_to_cancellation(message: Any) -> None:
    try:
        payload = json.loads(message.data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return
    run_id = payload.get("run_id")
    if isinstance(run_id, str) and not await cancel_active_mission(run_id):
        try:
            await asyncio.to_thread(remove_mission_checkpoint, run_id)
        except ValueError:
            return


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

    resume_subscription = await js.pull_subscribe(
        f"agent.runs.resume.{WORKER_ID}",
        stream="AGENT_RUNS",
        durable=f"agent-worker-resume-{WORKER_ID}",
        config=ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=900,
            deliver_policy=DeliverPolicy.ALL,
        ),
    )

    cancellation_subscription = await js.pull_subscribe(
        "agent.runs.cancelled",
        stream="AGENT_RUNS",
        durable=f"agent-worker-cancel-{WORKER_ID}",
        config=ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=60,
            deliver_policy=DeliverPolicy.ALL,
        ),
    )

    async def cancellation_loop() -> None:
        while True:
            try:
                messages = await cancellation_subscription.fetch(1, timeout=1)
            except NatsTimeoutError:
                continue
            message = messages[0]
            try:
                await respond_to_cancellation(message)
                await message.ack()
            except Exception as exc:
                print(f"cancellation event handling failed: {type(exc).__name__}", flush=True)
                await message.nak()

    cancellation_task = asyncio.create_task(cancellation_loop())

    try:
        while True:
            try:
                resumed = await resume_subscription.fetch(1, timeout=0.25)
            except NatsTimeoutError:
                try:
                    created = await subscription.fetch(1, timeout=1)
                except NatsTimeoutError:
                    continue
                await process_message(js, created[0])
            else:
                await process_message(js, resumed[0])
    finally:
        heartbeat.cancel()
        cancellation_task.cancel()
        await asyncio.gather(cancellation_task, return_exceptions=True)
        await subscription.unsubscribe()
        await resume_subscription.unsubscribe()
        await cancellation_subscription.unsubscribe()
        await nc.close()


if __name__ == "__main__":
    asyncio.run(main())
