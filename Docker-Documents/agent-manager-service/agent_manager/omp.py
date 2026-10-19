"""Chat turns enter here. OMP asks a question or hands a mission to the orchestrator."""

from __future__ import annotations

import json
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import require_run_submission
from .dashboard import record_activity
from .database import get_session
from .models import RunRecord, UserRecord
from .crew_rules import attach_project_reply, is_chat_turn, missing_requirements
from .orchestrator import ask_model, crew_label, crew_size, run_ephemeral_worker, start_crew

router = APIRouter()

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GREETING = re.compile(r"^(hi|hello|hey|yo|thanks|thank you|ok|okay)[.!?\s]*$", re.IGNORECASE)
PENDING: dict[str, tuple[float, str]] = {}
PENDING_SECONDS = 600.0

class OmpTurn(BaseModel):
    goal: str = Field(default="", max_length=20000)
    forgejo_repository: str = Field(default="", max_length=512)
    project_name: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=512)


def question(text: str) -> dict[str, str]:
    return {"type": "question", "text": text}


def mission_reply(mission_id: str, status: str, summary: str) -> dict[str, str]:
    return {
        "type": "mission",
        "mission_id": mission_id,
        "status": status,
        "summary": summary,
    }


def omp_decision(body: OmpTurn) -> dict[str, str] | None:
    """Return a question, or None when the turn is ready for the orchestrator."""
    goal = body.goal.strip()
    if not goal or GREETING.match(goal) or len(goal) < 12:
        return question("What should the crew do?")
    if not body.forgejo_repository.strip():
        return question("Which Forgejo repository should this mission use? For example owner/project.")
    project = (body.project_name or "").strip()
    if not project:
        return question("What project name should this mission use?")
    return None


async def accept_or_bounce(
    body: OmpTurn,
    actor: UserRecord,
    request: Request,
    session: AsyncSession,
) -> dict[str, str]:
    """Record one mission on the existing run ledger. Do not queue a worker."""
    repository = body.forgejo_repository.strip()
    project = (body.project_name or "").strip()
    bounced = not REPO_PATTERN.match(repository)
    gap = None if bounced else missing_requirements(body.goal.strip())
    size = crew_size(body.goal.strip())
    bounce = "Need a Forgejo repository in owner/project form." if bounced else gap
    bounce_to = "omp" if bounce else "none"
    summary = (
        "Bounced to OMP. The repository was not usable."
        if bounced
        else gap
        if gap
        else f"Accepted. Crew {crew_label(size)}. Starting."
    )
    mission_id = str(uuid.uuid4())
    source = "open-webui" if getattr(request.state, "auth_method", "") == "open-webui" else "api"
    metadata: dict[str, Any] = {
        "source": source,
        "operation": "mission",
        "worker_role": "orchestrator",
        "project_name": project,
        "submitted_by": actor.id,
        "ledger": {
            "kind": "mission",
            "bounce_to": bounce_to,
            "bounce": bounce,
            "result_summary": summary[:200],
        },
        "crew": {"size": size, "host": "starting"},
    }
    record = RunRecord(
        id=mission_id,
        goal=body.goal.strip(),
        forgejo_repository=repository or "unspecified",
        base_ref="main",
        model=body.model,
        status="bounced" if bounce else "accepted",
        run_metadata=metadata,
    )
    session.add(record)
    await session.flush()
    await record_activity(
        session,
        source="run",
        source_event_id=f"mission:{mission_id}",
        event_type="mission.bounced" if bounce else "mission.accepted",
        actor_user_id=actor.id,
        run_id=mission_id,
        repository=record.forgejo_repository,
        summary=summary[:200],
        payload={"bounce_to": bounce_to},
    )
    await session.commit()
    if bounce:
        return question(bounce or "The orchestrator sent this mission back.")
    summary = (
        f"OMP sized the job {crew_label(size)}. "
        "Orchestrator handed it to the worker computers. "
        "Their OMP starts the ephemeral image and returns the revised result."
    )
    metadata = dict(record.run_metadata or {})
    ledger = dict(metadata.get("ledger") or {})
    ledger["result_summary"] = summary[:200]
    ledger["kind"] = "mission"
    ledger["bounce_to"] = "none"
    metadata["ledger"] = ledger
    metadata["worker_role"] = "coder"
    metadata["crew"] = {"size": size, "host": "worker-computer"}
    metadata["chatter"] = [
        f"OMP: this job is {crew_label(size)}.",
        "OMP: planner, coder, and reviewer nodes are assigned.",
        "Orchestrator: sending the task to the worker computers.",
        "Worker OMP: not heard from yet.",
    ]
    record.run_metadata = metadata
    record.status = "queued"
    await session.commit()
    event = {
        "event": "run.created",
        "event_id": str(uuid.uuid4()),
        "run_id": mission_id,
        "goal": body.goal.strip(),
        "forgejo_repository": record.forgejo_repository,
        "base_ref": "main",
        "model": body.model,
        "metadata": metadata,
    }
    try:
        await request.app.state.nats.jetstream().publish(
            "agent.runs.created",
            json.dumps(event).encode("utf-8"),
            headers={"Nats-Msg-Id": f"agent.runs.created:{mission_id}"},
        )
    except Exception:
        return question("OMP sized the crew, but the orchestrator could not reach the worker computers.")
    return question(f"{summary} Mission {mission_id}.")


@router.post("/v1/omp/turns")
async def omp_turn(
    body: OmpTurn,
    request: Request,
    actor: UserRecord = Depends(require_run_submission),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    pending = PENDING.get(actor.id)
    pending_goal = pending[1] if pending and time.monotonic() - pending[0] < PENDING_SECONDS else None
    goal, project_name = attach_project_reply(body.goal, pending_goal)
    if project_name:
        PENDING.pop(actor.id, None)
        body = body.model_copy(update={"goal": goal, "project_name": project_name})
    if is_chat_turn(body.goal):
        return await answer_chat(body.goal.strip(), actor, request, session, body.project_name)
    gap = missing_requirements(body.goal)
    if gap:
        return question(gap)
    if not (body.project_name or "").strip():
        body = body.model_copy(update={"project_name": chat_title(body.goal, None)})
    reply = omp_decision(body)
    if reply is not None:
        return reply
    try:
        return await accept_or_bounce(body, actor, request, session)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="The mission could not be recorded") from exc


def chat_title(goal: str, project_name: str | None) -> str:
    if project_name and project_name.strip():
        return project_name.strip()[:48]
    for line in goal.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().casefold() in {"chat", "project"} and value.strip():
            return value.strip()[:48]
    words = goal.strip().split()
    return (" ".join(words[:6]) or "Untitled chat")[:48]


async def answer_chat(
    goal: str,
    actor: UserRecord,
    request: Request,
    session: AsyncSession,
    project_name: str | None,
) -> dict[str, str]:
    from .settings import settings

    title = chat_title(goal, project_name)
    if not settings.model_api_key:
        return question("OMP cannot answer yet. The manager has no model key.")
    try:
        text = await ask_model(
            "You are Luna, answering through OMP. Answer in a few sentences. Do not ask for a project name.",
            goal,
        )
    except Exception as exc:
        await record_chat(session, actor, request, title, "Luna did not answer.")
        return question(f"OMP could not reach Luna ({type(exc).__name__}).")
    await record_chat(session, actor, request, title, "Luna answered through OMP.")
    return question(text.strip()[:4000])


async def record_chat(
    session: AsyncSession,
    actor: UserRecord,
    request: Request,
    title: str,
    summary: str,
) -> None:
    try:
        mission_id = str(uuid.uuid4())
        source = "open-webui" if getattr(request.state, "auth_method", "") == "open-webui" else "api"
        record = RunRecord(
            id=mission_id,
            goal=title,
            forgejo_repository="chat/luna",
            base_ref="main",
            status="completed",
            run_metadata={
                "source": source,
                "operation": "chat",
                "worker_role": "omp",
                "project_name": title,
                "submitted_by": actor.id,
                "ledger": {
                    "kind": "mission",
                    "bounce_to": "none",
                    "result_summary": summary[:200],
                },
            },
        )
        session.add(record)
        await session.flush()
        await record_activity(
            session,
            source="run",
            source_event_id=f"chat:{mission_id}",
            event_type="chat.answered",
            actor_user_id=actor.id,
            run_id=mission_id,
            repository="chat/luna",
            summary=f"Chat {title}: {summary}"[:200],
        )
        await session.commit()
    except Exception:
        await session.rollback()


@router.get("/v1/omp/missions/{mission_id}")
async def mission_status(
    mission_id: str,
    actor: UserRecord = Depends(require_run_submission),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    del actor
    record = await session.get(RunRecord, mission_id)
    metadata = record.run_metadata or {} if record is not None else {}
    if record is None or metadata.get("operation") != "mission":
        raise HTTPException(status_code=404, detail="Mission not found")
    ledger = metadata.get("ledger") if isinstance(metadata.get("ledger"), dict) else {}
    result = metadata.get("result") if isinstance(metadata.get("result"), dict) else {}
    script = result.get("script") if isinstance(result.get("script"), str) else None
    summary = ledger.get("result_summary") if isinstance(ledger.get("result_summary"), str) else ""
    bounce = ledger.get("bounce") if isinstance(ledger.get("bounce"), str) else None
    return {
        "type": "mission",
        "mission_id": record.id,
        "status": record.status,
        "summary": summary[:200],
        "question": bounce if record.status == "bounced" else None,
        "script": script[:8000] if record.status == "completed" and script else None,
        "crew": metadata.get("crew") if isinstance(metadata.get("crew"), dict) else None,
    }
