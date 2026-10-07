#!/usr/bin/env python3
"""Idempotently create the tracked Dark Factory roadmap in Agent Manager."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def request_json(
    base_url: str,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Agent Manager returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Agent Manager: {exc.reason}") from exc
    return json.loads(payload) if payload else None


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("repository"), str) or not payload["repository"]:
        raise ValueError("manifest repository is required")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("manifest tasks must be a non-empty list")
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("every manifest task must be an object")
        title = task.get("title")
        if not isinstance(title, str) or not title:
            raise ValueError("every manifest task requires a title")
        if title in seen:
            raise ValueError(f"duplicate manifest title: {title}")
        seen.add(title)
        if task.get("priority") not in {"low", "normal", "high", "urgent"}:
            raise ValueError(f"invalid priority for {title}")
    return payload


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default=os.getenv("AGENT_MANAGER_URL", "http://localhost:8090"),
        help="Agent Manager base URL",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository_root / "docs" / "dark-factory-backlog.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.getenv("AGENT_MANAGER_API_TOKEN", "")
    if not token:
        print("AGENT_MANAGER_API_TOKEN is required", file=sys.stderr)
        return 2
    parsed_url = urllib.parse.urlparse(args.url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        print("--url must be an HTTP(S) URL", file=sys.stderr)
        return 2

    try:
        manifest = load_manifest(args.manifest)
        existing = request_json(args.url, token, "GET", "/v1/tasks?limit=500")
        existing_keys = {
            (task.get("repository"), task.get("title"))
            for task in existing
            if isinstance(task, dict)
        }
        missing = [
            task
            for task in manifest["tasks"]
            if (manifest["repository"], task["title"]) not in existing_keys
        ]
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "manifest_tasks": len(manifest["tasks"]),
                        "existing": len(manifest["tasks"]) - len(missing),
                        "would_create": len(missing),
                    }
                )
            )
            return 0

        created = 0
        for task in missing:
            request_json(
                args.url,
                token,
                "POST",
                "/v1/tasks",
                {
                    "title": task["title"],
                    "description": task["description"],
                    "priority": task["priority"],
                    "status": "todo",
                    "repository": manifest["repository"],
                },
            )
            created += 1
        print(
            json.dumps(
                {
                    "manifest_tasks": len(manifest["tasks"]),
                    "created": created,
                    "already_present": len(manifest["tasks"]) - created,
                }
            )
        )
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
