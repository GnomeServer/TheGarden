"""Chat turns enter here. OMP asks a question or hands a mission to the orchestrator."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .artifacts import (
    mission_artifact,
    provision_upload_capability,
    upload_event_metadata,
    upload_token_hash,
)
from .auth import require_run_submission
from .dashboard import record_activity
from .database import get_session
from .models import RunRecord, UserRecord
from .settings import settings
from .crew_rules import attach_project_reply, crew_label, crew_size, is_chat_turn, missing_requirements
from .orchestrator import ask_model, dispatch_to_worker

router = APIRouter()
logger = logging.getLogger("agent-manager.omp")

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
GREETING = re.compile(r"^(hi|hello|hey|yo|thanks|thank you|ok|okay)[.!?\s]*$", re.IGNORECASE)
PENDING: dict[str, tuple[float, str]] = {}
PENDING_SECONDS = 600.0

class OmpTurn(BaseModel):
    goal: str = Field(default="", max_length=20000)
    forgejo_repository: str = Field(default="", max_length=512)
    project_name: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=512)
    mission_id: uuid.UUID | None = None
    cancel_token: str | None = Field(default=None, min_length=32, max_length=128)

    @model_validator(mode="after")
    def validate_cancel_capability(self) -> "OmpTurn":
        if (self.mission_id is None) != (self.cancel_token is None):
            raise ValueError("mission_id and cancel_token must be provided together")
        return self


class MissionClarification(BaseModel):
    clarification: str = Field(min_length=1, max_length=10000)


def question(text: str) -> dict[str, str]:
    return {"type": "question", "text": text}


def mission_cancel_token_hash(token: str) -> str:
    message = b"agent-manager:mission-cancel:v1:" + token.encode("utf-8")
    return hmac.new(settings.signing_key, message, hashlib.sha256).hexdigest()


def mission_reply(
    mission_id: str, status: str, summary: str, cancel_token: str
) -> dict[str, str]:
    return {
        "type": "mission",
        "mission_id": mission_id,
        "status": status,
        "summary": summary,
        "cancel_token": cancel_token,
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
    """Record a mission and hand its planned work to the central orchestrator."""
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
    mission_id = str(body.mission_id or uuid.uuid4())
    cancel_token = body.cancel_token or secrets.token_urlsafe(32)
    source = "open-webui" if getattr(request.state, "auth_method", "") == "open-webui" else "api"
    metadata: dict[str, Any] = {
        "source": source,
        "operation": "mission",
        "worker_role": "orchestrator",
        "project_name": project,
        "submitted_by": actor.id,
        "mission_cancel_token_hash": mission_cancel_token_hash(cancel_token),
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
    if bounce:
        await session.commit()
        return question(bounce or "The orchestrator sent this mission back.")
    summary = (
        f"OMP handed the job to a worker laptop. "
        f"The worker runs {crew_label(size)}."
    )
    metadata = dict(record.run_metadata or {})
    ledger = dict(metadata.get("ledger") or {})
    ledger["result_summary"] = summary[:200]
    ledger["kind"] = "mission"
    ledger["bounce_to"] = "none"
    metadata["ledger"] = ledger
    metadata["worker_role"] = "coder"
    metadata["crew"] = {"size": size, "host": "worker-computer", "engine": "worker-omp-image"}
    metadata["mission"] = {
        "mission_id": mission_id,
        "goal": body.goal.strip(),
        "repo": repository,
        "planners": int(size["planners"]),
        "coders": int(size["coders"]),
        "reviewers": int(size["reviewers"]),
    }
    metadata["chatter"] = [
        f"OMP: sized this as {crew_label(size)}.",
        "Orchestrator: publishing the mission plan to the worker laptops.",
        "Worker OMP: not heard from yet.",
    ]
    record.run_metadata = metadata
    upload_token, upload_url = provision_upload_capability(record)
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
        "metadata": upload_event_metadata(record, upload_token, upload_url),
    }
    try:
        await dispatch_to_worker(request.app.state.nats.jetstream(), event)
    except Exception:
        return question("OMP recorded the mission, but the orchestrator could not reach the worker laptops.")
    return mission_reply(mission_id, "queued", summary, cancel_token)


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
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
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
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else None
    reviewer_count = result.get("reviewer_count")
    if type(reviewer_count) is not int or reviewer_count < 0:
        reviewer_count = None
    summary = ledger.get("result_summary") if isinstance(ledger.get("result_summary"), str) else ""
    bounce = ledger.get("bounce") if isinstance(ledger.get("bounce"), str) else None
    return {
        "type": "mission",
        "mission_id": record.id,
        "status": record.status,
        "summary": summary[:200],
        "error": record.error,
        "question": (
            result.get("question")
            if record.status == "needs_input"
            else bounce if record.status == "bounced" else None
        ),
        "crew": metadata.get("crew") if isinstance(metadata.get("crew"), dict) else None,
        "crew_plan": result.get("crew_plan"),
        "attempts": result.get("attempts"),
        "evidence": evidence,
        "crew_outcome": result.get("crew_outcome"),
        "worker_id": record.worker_id,
        "reviewer_count": reviewer_count,
        "artifact": mission_artifact(metadata, record.id) if record.status == "completed" else None,
    }


def _bearer_value(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    return token


@router.get("/v1/omp/missions/{mission_id}/control")
async def mission_control(
    mission_id: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token = _bearer_value(authorization)
    record = await session.get(RunRecord, mission_id)
    metadata = record.run_metadata or {} if record is not None else {}
    expected_hash = metadata.get("artifact_upload_token_hash")
    if (
        record is None
        or metadata.get("operation") != "mission"
        or not isinstance(token, str)
        or not isinstance(expected_hash, str)
        or not hmac.compare_digest(upload_token_hash(token), expected_hash)
    ):
        raise HTTPException(status_code=404, detail="Mission not found")
    result = metadata.get("result") if isinstance(metadata.get("result"), dict) else {}
    return {
        "mission_id": record.id,
        "status": record.status,
        "cancel_requested": record.status in {"cancel_requested", "cancelled"},
        "summary": result.get("summary"),
        "question": result.get("question"),
        "reviewer_count": result.get("reviewer_count"),
    }


@router.post("/v1/omp/missions/{mission_id}/cancel")
async def cancel_mission(
    mission_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    token = _bearer_value(authorization)
    record = await session.get(RunRecord, mission_id, with_for_update=True)
    metadata = record.run_metadata or {} if record is not None else {}
    expected_hash = metadata.get("mission_cancel_token_hash")
    if (
        record is None
        or metadata.get("operation") != "mission"
        or not isinstance(token, str)
        or not isinstance(expected_hash, str)
        or not hmac.compare_digest(mission_cancel_token_hash(token), expected_hash)
    ):
        raise HTTPException(status_code=404, detail="Mission not found")
    if record.status in {"completed", "failed", "bounced", "cancelled"}:
        return {"mission_id": record.id, "status": record.status}
    if record.status == "cancel_requested":
        return {"mission_id": record.id, "status": record.status}

    if record.status in {"accepted", "queued", "needs_input"}:
        record.status = "cancelled"
        record.completed_at = datetime.now(timezone.utc)
    else:
        record.status = "cancel_requested"
    await session.commit()
    try:
        await request.app.state.nats.jetstream().publish(
            "agent.runs.cancelled",
            json.dumps({
                "event": "run.cancelled",
                "event_id": str(uuid.uuid4()),
                "run_id": mission_id,
            }).encode("utf-8"),
            headers={"Nats-Msg-Id": f"agent.runs.cancelled:{mission_id}"},
        )
    except Exception:
        logger.exception("failed to publish cancellation for mission %s", mission_id)
    return {"mission_id": record.id, "status": record.status}


@router.post("/v1/omp/missions/{mission_id}/resume")
async def resume_mission(
    mission_id: str,
    body: MissionClarification,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    token = _bearer_value(authorization)
    record = await session.get(RunRecord, mission_id, with_for_update=True)
    metadata = record.run_metadata or {} if record is not None else {}
    expected_hash = metadata.get("mission_cancel_token_hash")
    if (
        record is None
        or metadata.get("operation") != "mission"
        or not isinstance(token, str)
        or not isinstance(expected_hash, str)
        or not hmac.compare_digest(mission_cancel_token_hash(token), expected_hash)
    ):
        raise HTTPException(status_code=404, detail="Mission not found")
    if record.status in {"queued", "running"}:
        return mission_reply(
            record.id, record.status, "Mission is already being processed.", token
        )
    if record.status != "needs_input":
        raise HTTPException(
            status_code=409,
            detail=f"Mission cannot resume from status {record.status!r}.",
        )
    worker_id = record.worker_id
    if not isinstance(worker_id, str) or not WORKER_ID_PATTERN.fullmatch(worker_id):
        raise HTTPException(status_code=409, detail="Mission has no resumable worker checkpoint.")

    clarification = body.clarification.strip()
    if not clarification:
        raise HTTPException(status_code=422, detail="Clarification must not be empty.")
    old_result = dict(metadata.get("result") or {})
    old_ledger = dict(metadata.get("ledger") or {})
    resume_count = metadata.get("resume_count")
    if type(resume_count) is not int or resume_count < 0:
        resume_count = 0
    resume_count += 1
    history = metadata.get("clarification_history")
    history = list(history) if isinstance(history, list) else []
    history.append({
        "reviewer_count": old_result.get("reviewer_count"),
        "question": old_result.get("question") or old_result.get("summary"),
        "clarification": clarification,
    })
    metadata["clarification_history"] = history[-20:]
    metadata["resume_count"] = resume_count
    metadata["ledger"] = {
        **old_ledger,
        "result_summary": "Clarification received; restarting the review cycle.",
    }
    metadata["result"] = {
        "status": "queued",
        "reviewer_approved": False,
        "reviewer_count": 0,
        "summary": "Clarification received; restarting the review cycle.",
        "artifacts": [],
    }
    metadata.pop("artifact_upload_consumed", None)
    record.run_metadata = metadata
    record.status = "queued"
    record.error = None
    record.completed_at = None
    upload_token, upload_url = provision_upload_capability(record)
    resume_metadata = upload_event_metadata(record, upload_token, upload_url)
    resume_metadata.update({
        "worker_id": worker_id,
        "worker_role": "coder",
        "resume_from_checkpoint": True,
        "clarification": clarification,
        "reviewer_feedback": old_result.get("question") or old_result.get("summary"),
        "reviewer_count_reset": 0,
        "resume_count": resume_count,
    })
    event = {
        "event": "run.resume",
        "event_id": str(uuid.uuid4()),
        "run_id": record.id,
        "goal": record.goal,
        "forgejo_repository": record.forgejo_repository,
        "base_ref": record.base_ref,
        "model": record.model,
        "metadata": resume_metadata,
    }
    await session.commit()
    try:
        await request.app.state.nats.jetstream().publish(
            f"agent.runs.resume.{worker_id}",
            json.dumps(event).encode("utf-8"),
            headers={
                "Nats-Msg-Id": f"agent.runs.resume:{record.id}:{resume_count}"
            },
        )
    except Exception as exc:
        logger.exception("failed to resume clarified mission %s", record.id)
        metadata["result"] = old_result
        metadata["ledger"] = old_ledger
        record.run_metadata = metadata
        record.status = "needs_input"
        record.error = f"failed to queue clarified mission: {exc}"[:4000]
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail="Clarification was saved, but the worker could not be reached.",
        ) from exc
    return mission_reply(
        record.id,
        "queued",
        "Clarification received; the worker is resuming the mission.",
        token,
    )
