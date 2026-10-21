"""Central dispatch to worker laptops plus ephemeral worker helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm.attributes import flag_modified

from .dashboard import record_activity
from .database import SessionLocal
from .models import RunRecord
from .settings import settings

logger = logging.getLogger("agent-manager.orchestrator")

from .crew_rules import (  # noqa: E402
    MAX_SCRIPT_CHARS,
    compile_script,
    container_create_body,
    crew_label,
    crew_size,
    extract_script,
    missing_requirements,
    review_locally,
)

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


class DockerCrew:
    """Creates and destroys the crew network. The model key never enters a container."""

    def __init__(self, image: str) -> None:
        self.image = image
        self.network = ""
        self.names: list[str] = []
        self.client: httpx.AsyncClient | None = None

    async def start(self, mission_id: str, size: dict[str, int | str]) -> str:
        if not Path("/var/run/docker.sock").exists():
            raise RuntimeError("docker socket is not mounted")
        self.network = f"crew-{mission_id[:8]}"
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
            base_url="http://docker",
            timeout=20.0,
        )
        await self.client.post(
            "/networks/create",
            json={"Name": self.network, "CheckDuplicate": True, "Internal": True},
        )
        roles = (
            ["planner"] * int(size["planners"])
            + [f"coder-{index}" for index in range(1, int(size["coders"]) + 1)]
            + [f"reviewer-{index}" for index in range(1, int(size["reviewers"]) + 1)]
        )
        for role in roles:
            name = f"{self.network}-{role}"
            created = await self.client.post(
                "/containers/create",
                params={"name": name},
                json=container_create_body(name, self.network, self.image, role),
            )
            created.raise_for_status()
            await self.client.post(f"/containers/{name}/start")
            self.names.append(name)
        return "docker"

    async def stop(self) -> None:
        if self.client is None:
            return
        for name in self.names:
            await self.client.post(f"/containers/{name}/stop", params={"t": 1})
            await self.client.delete(f"/containers/{name}", params={"force": True})
        if self.network:
            await self.client.delete(f"/networks/{self.network}")
        await self.client.aclose()
        self.client = None


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


async def _set_state(
    session,
    record: RunRecord,
    *,
    status: str,
    summary: str,
    bounce: str | None = None,
    script: str | None = None,
    crew: dict[str, Any] | None = None,
) -> None:
    metadata = dict(record.run_metadata or {})
    ledger = dict(metadata.get("ledger") or {})
    ledger["kind"] = "mission"
    ledger["result_summary"] = summary[:200]
    ledger["bounce_to"] = "omp" if bounce else "none"
    ledger["bounce"] = bounce
    metadata["ledger"] = ledger
    if crew is not None:
        metadata["crew"] = crew
    if script is not None:
        result = dict(metadata.get("result") or {})
        result["script"] = script[:MAX_SCRIPT_CHARS]
        result["source"] = "crew"
        metadata["result"] = result
    record.status = status
    record.run_metadata = metadata
    flag_modified(record, "run_metadata")
    await record_activity(
        session,
        source="run",
        source_event_id=f"mission:{record.id}:{status}:{uuid.uuid4()}",
        event_type=f"mission.{status}",
        actor_user_id=metadata.get("submitted_by"),
        run_id=record.id,
        repository=record.forgejo_repository,
        summary=summary[:200],
        payload={"bounce_to": ledger["bounce_to"]},
    )
    await session.commit()


async def run_ephemeral_worker(mission_id: str, goal: str, size: dict[str, int | str]) -> dict[str, Any]:
    """Start one worker image, read its OMP result, then delete the image. Do not review it here."""
    if not Path("/var/run/docker.sock").exists():
        raise RuntimeError("docker socket is not mounted")
    source = Path(__file__).with_name("worker_omp.py").read_text(encoding="utf-8")
    base = settings.model_base_url.rstrip("/")
    if base and not base.endswith("/v1"):
        base = f"{base}/v1"
    name = f"omp-worker-{mission_id[:8]}"
    spec = {
        "Image": settings.crew_image,
        "User": "65534:65534",
        "Tty": True,
        "Env": [
            f"GOAL={goal}",
            f"CREW_NODES={crew_label(size)}",
            f"MODEL_BASE_URL={base}",
            f"MODEL_API_KEY={settings.model_api_key}",
            f"MODEL_NAME={settings.model_name}",
            f"REASONING_EFFORT={settings.reasoning_effort}",
        ],
        "Cmd": ["python", "-c", source],
        "HostConfig": {
            "NetworkMode": "caddy_proxy",
            "ReadonlyRootfs": True,
            "Memory": 512 * 1024 * 1024,
            "PidsLimit": 64,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
        },
    }
    if "docker.sock" in str(spec["HostConfig"]):
        raise RuntimeError("worker image must not receive the Docker socket")
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
        base_url="http://docker",
        timeout=120.0,
    ) as client:
        created = await client.post("/containers/create", params={"name": name}, json=spec)
        created.raise_for_status()
        await client.post(f"/containers/{name}/start")
        try:
            await client.post(f"/containers/{name}/wait")
            logs = await client.get(f"/containers/{name}/logs", params={"stdout": True, "stderr": True})
            logs.raise_for_status()
        finally:
            await client.delete(f"/containers/{name}", params={"force": True})
    text = logs.text.strip()
    line = next((item for item in reversed(text.splitlines()) if item.startswith("{")), "")
    if not line:
        raise RuntimeError("worker image exited without a result")
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise RuntimeError("worker image returned an invalid result")
    return payload


async def run_mission(mission_id: str) -> None:
    try:
        async with SessionLocal() as session:
            record = await session.get(RunRecord, mission_id)
            if record is None:
                return
            goal = record.goal
            size = crew_size(goal)
            await _set_state(
                session,
                record,
                status="planning",
                summary=f"Orchestrator refined the request. Starting one ephemeral worker: {crew_label(size)}.",
                crew={"size": size, "host": "ephemeral"},
            )
        try:
            result = await run_ephemeral_worker(mission_id, goal, size)
        except Exception:
            logger.warning("ephemeral worker image was not started for %s", mission_id)
            async with SessionLocal() as session:
                record = await session.get(RunRecord, mission_id)
                if record is not None:
                    await _set_state(
                        session,
                        record,
                        status="bounced",
                        summary="The ephemeral worker image could not be started.",
                        bounce="The ephemeral worker image could not be started.",
                        crew={"size": size, "host": "none"},
                    )
            return
        async with SessionLocal() as session:
            record = await session.get(RunRecord, mission_id)
            if record is None:
                return
            summary = result.get("summary") if isinstance(result.get("summary"), str) else "Worker image exited."
            script = result.get("script") if isinstance(result.get("script"), str) else None
            status = "completed" if result.get("status") == "completed" else "bounced"
            await _set_state(
                session,
                record,
                status=status,
                summary=summary[:200],
                bounce=summary[:200] if status == "bounced" else None,
                script=script,
                crew={"size": size, "host": "removed"},
            )
    except Exception:
        logger.exception("ephemeral worker failed for %s", mission_id)
        async with SessionLocal() as session:
            record = await session.get(RunRecord, mission_id)
            if record is not None and record.status not in {"completed", "bounced"}:
                await _set_state(session, record, status="failed", summary="The worker image stopped before returning a result.")


def start_crew(mission_id: str) -> None:
    asyncio.create_task(run_mission(mission_id))
