#!/usr/bin/env python3
"""Forward normalized login and service events from journald to Agent Manager."""

from __future__ import annotations

import hashlib
import json
import os
import select
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANAGER_URL = os.environ.get("ACTIVITY_MANAGER_URL", "").rstrip("/")
NODE_ID = os.environ.get("ACTIVITY_NODE_ID", "")
KEY_FILE = Path(os.environ.get("ACTIVITY_NODE_KEY_FILE", "/etc/dark-factory/activity-key"))
CURSOR_FILE = Path(
    os.environ.get("ACTIVITY_CURSOR_FILE", "/var/lib/dark-factory-activity/journal.cursor")
)
BATCH_SIZE = max(1, min(100, int(os.environ.get("ACTIVITY_BATCH_SIZE", "25"))))
FLUSH_SECONDS = max(1.0, float(os.environ.get("ACTIVITY_FLUSH_SECONDS", "5")))
HTTP_TIMEOUT = max(2.0, float(os.environ.get("ACTIVITY_HTTP_TIMEOUT", "15")))
CA_FILE = os.environ.get("SSL_CERT_FILE", "")

ACCEPTED_RE = re.compile(r"Accepted \S+ for (?P<user>\S+) from (?P<remote>\S+)")
FAILED_RE = re.compile(r"Failed \S+ for (?:invalid user )?(?P<user>\S+) from (?P<remote>\S+)")
CLOSED_RE = re.compile(r"session closed for user (?P<user>\S+)", re.IGNORECASE)
OPENED_RE = re.compile(r"session opened for user (?P<user>\S+)", re.IGNORECASE)
SUDO_RE = re.compile(r"^(?P<user>[^ :]+)\s*:")


def timestamp(record: dict[str, Any]) -> str:
    raw = record.get("__REALTIME_TIMESTAMP")
    try:
        return datetime.fromtimestamp(int(raw) / 1_000_000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


def event_id(cursor: str) -> str:
    return hashlib.sha256(f"{NODE_ID}\0{cursor}".encode("utf-8")).hexdigest()


def normalize(record: dict[str, Any]) -> dict[str, Any] | None:
    message = str(record.get("MESSAGE") or "")
    identifier = str(record.get("SYSLOG_IDENTIFIER") or "")
    unit = str(record.get("_SYSTEMD_UNIT") or "")
    cursor = str(record.get("__CURSOR") or "")
    if not cursor:
        return None
    base: dict[str, Any] = {
        "event_id": event_id(cursor),
        "occurred_at": timestamp(record),
        "metadata": {"systemd_unit": unit or None},
        "_cursor": cursor,
    }

    if identifier in {"sshd", "ssh"} or unit in {"ssh.service", "sshd.service"}:
        accepted = ACCEPTED_RE.search(message)
        if accepted:
            return {
                **base,
                "source": "sshd",
                "service": "ssh",
                "event_type": "login",
                "identity": accepted.group("user"),
                "identity_source": "linux",
                "remote_identity": accepted.group("remote"),
                "summary": f"{accepted.group('user')} logged into {NODE_ID} through SSH",
            }
        failed = FAILED_RE.search(message)
        if failed:
            return {
                **base,
                "source": "sshd",
                "service": "ssh",
                "event_type": "auth_failed",
                "identity": failed.group("user"),
                "identity_source": "linux",
                "remote_identity": failed.group("remote"),
                "summary": f"Failed SSH authentication for {failed.group('user')} on {NODE_ID}",
            }
        opened = OPENED_RE.search(message)
        if opened:
            return {
                **base,
                "source": "sshd",
                "service": "ssh",
                "event_type": "session_opened",
                "identity": opened.group("user"),
                "identity_source": "linux",
                "summary": f"SSH session opened for {opened.group('user')} on {NODE_ID}",
            }
        closed = CLOSED_RE.search(message)
        if closed:
            return {
                **base,
                "source": "sshd",
                "service": "ssh",
                "event_type": "logout",
                "identity": closed.group("user"),
                "identity_source": "linux",
                "summary": f"SSH session closed for {closed.group('user')} on {NODE_ID}",
            }
        return None

    if identifier == "sudo" or unit == "sudo.service":
        match = SUDO_RE.search(message)
        user = match.group("user") if match else None
        return {
            **base,
            "source": "sudo",
            "service": "sudo",
            "event_type": "privilege_use",
            "identity": user,
            "identity_source": "linux" if user else None,
            "summary": f"{user or 'A user'} used sudo on {NODE_ID}",
        }

    if identifier == "tailscaled" or unit == "tailscaled.service":
        lowered = message.lower()
        if not any(word in lowered for word in ("login", "logout", "auth", "peer")):
            return None
        return {
            **base,
            "source": "tailscale",
            "service": "tailscale",
            "event_type": "network_identity_event",
            "summary": f"Tailscale identity activity occurred on {NODE_ID}",
        }

    if unit in {"dark-factory-worker.service", "agent-worker.service"}:
        lowered = message.lower()
        if not any(word in lowered for word in ("started", "stopped", "failed")):
            return None
        event_type = "service_failed" if "failed" in lowered else "service_state"
        return {
            **base,
            "source": "systemd",
            "service": "agent-worker",
            "event_type": event_type,
            "summary": f"Agent worker service changed state on {NODE_ID}",
        }
    return None


def ssl_context() -> ssl.SSLContext | None:
    if not MANAGER_URL.startswith("https://"):
        return None
    return ssl.create_default_context(cafile=CA_FILE or None)


def send(events: list[dict[str, Any]], token: str) -> None:
    payload_events = [{key: value for key, value in event.items() if key != "_cursor"} for event in events]
    request = urllib.request.Request(
        f"{MANAGER_URL}/v1/ingest/events",
        data=json.dumps({"node_id": NODE_ID, "events": payload_events}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Node-Key": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(
        request,
        timeout=HTTP_TIMEOUT,
        **({"context": ssl_context()} if MANAGER_URL.startswith("https://") else {}),
    ) as response:
        if response.status != 202:
            raise RuntimeError(f"ingestion returned HTTP {response.status}")
        result = json.loads(response.read().decode("utf-8"))
        if result.get("accepted", 0) + result.get("duplicates", 0) != len(events):
            raise RuntimeError("ingestion did not account for every event")


def journal_command() -> list[str]:
    command = ["journalctl", "--output=json", "--follow", "--no-pager"]
    if CURSOR_FILE.exists() and CURSOR_FILE.read_text(encoding="utf-8").strip():
        command.append(f"--after-cursor={CURSOR_FILE.read_text(encoding='utf-8').strip()}")
    else:
        command.append("--since=now")
    return command


def validate_configuration() -> str:
    if not MANAGER_URL.startswith(("http://", "https://")):
        raise RuntimeError("ACTIVITY_MANAGER_URL must be an HTTP(S) URL")
    if not NODE_ID or not re.fullmatch(r"[A-Za-z0-9._-]+", NODE_ID):
        raise RuntimeError("ACTIVITY_NODE_ID is required and contains invalid characters")
    token = KEY_FILE.read_text(encoding="utf-8").strip()
    if not token.startswith("dfn_") or "." not in token:
        raise RuntimeError("activity node key is missing or invalid")
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    return token


def main() -> None:
    token = validate_configuration()
    process = subprocess.Popen(
        journal_command(),
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout is None:
        raise RuntimeError("journalctl did not provide stdout")
    batch: list[dict[str, Any]] = []
    last_flush = time.monotonic()
    failures = 0
    try:
        while True:
            timeout = max(0.0, FLUSH_SECONDS - (time.monotonic() - last_flush))
            ready, _, _ = select.select([process.stdout], [], [], timeout)
            if ready:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        raise RuntimeError(f"journalctl exited with status {process.returncode}")
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = normalize(record) if isinstance(record, dict) else None
                if event is not None:
                    batch.append(event)
            if batch and (
                len(batch) >= BATCH_SIZE
                or time.monotonic() - last_flush >= FLUSH_SECONDS
            ):
                while True:
                    try:
                        send(batch, token)
                        CURSOR_FILE.write_text(
                            batch[-1]["_cursor"] + "\n", encoding="utf-8"
                        )
                        batch.clear()
                        failures = 0
                        last_flush = time.monotonic()
                        break
                    except (
                        OSError,
                        RuntimeError,
                        urllib.error.URLError,
                        json.JSONDecodeError,
                    ) as exc:
                        failures += 1
                        delay = min(60, 2 ** min(failures, 6))
                        print(
                            f"activity ingestion failed; retrying in {delay}s: {exc}",
                            flush=True,
                        )
                        time.sleep(delay)
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
