"""Mission dispatch and shared model access for Agent Manager."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .settings import settings


async def dispatch_to_worker(jetstream: Any, event: dict[str, Any]) -> None:
    """Dispatch an OMP-approved mission to the worker pool."""
    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("mission event is missing run_id")
    event.setdefault(
        "expires_at",
        (
            datetime.now(timezone.utc)
            + timedelta(seconds=settings.worker_run_stale_after_seconds)
        ).isoformat(),
    )
    await jetstream.publish(
        "agent.runs.created",
        json.dumps(event).encode("utf-8"),
        headers={"Nats-Msg-Id": f"agent.runs.created:{run_id}"},
    )



async def ask_model(system: str, user: str, *, max_tokens: int = 400) -> str:
    if not settings.model_base_url.strip():
        raise RuntimeError("model URL is empty")
    headers = {"Content-Type": "application/json"}
    if settings.model_api_key:
        headers["Authorization"] = f"Bearer {settings.model_api_key}"
    payload = {
        "model": settings.model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user[:12000]},
        ],
    }
    if "api.openai.com" in settings.model_base_url:
        payload["max_completion_tokens"] = max_tokens
        payload["reasoning_effort"] = settings.reasoning_effort
    else:
        payload["temperature"] = 0.2
        payload["max_tokens"] = max_tokens
    base = settings.model_base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
        response = await client.post(
            f"{base}/chat/completions",
            headers=headers,
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Luna HTTP {response.status_code}")
        body = response.json()
    message = body["choices"][0]["message"]
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        if max_tokens < 1200:
            return await ask_model(system, user, max_tokens=1200)
        raise RuntimeError("Luna returned an empty reply")
    return content
