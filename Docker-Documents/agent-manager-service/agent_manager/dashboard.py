from __future__ import annotations

import hashlib
import asyncio
import hmac
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import (
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    create_oauth_state,
    create_session,
    require_session_csrf,
    session_payload,
    session_user,
    validate_oauth_state,
)
from .database import get_session
from .live import live_broker
from .models import (
    ActivityRecord,
    RunRecord,
    TaskAuditRecord,
    TaskRecord,
    UserRecord,
    WorkerRecord,
)
from .settings import settings

router = APIRouter()
TASK_STATUSES = {"todo", "in_progress", "blocked", "done", "cancelled"}
TASK_PRIORITIES = {"low", "normal", "high", "urgent"}
TERMINAL_TASK_STATUSES = {"done", "cancelled"}



class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=50000)
    status: Literal["todo", "in_progress", "blocked", "done", "cancelled"] = "todo"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    due_at: datetime | None = None
    assignee_user_id: str | None = Field(default=None, max_length=36)
    assignee_worker_id: str | None = Field(default=None, max_length=255)
    repository: str | None = Field(default=None, max_length=512)
    forgejo_issue_number: int | None = Field(default=None, ge=1)
    run_id: str | None = Field(default=None, max_length=36)

    @field_validator("due_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("due_at must include a timezone")
        return value


class TaskUpdate(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=50000)
    status: Literal["todo", "in_progress", "blocked", "done", "cancelled"] | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    due_at: datetime | None = None
    assignee_user_id: str | None = Field(default=None, max_length=36)
    assignee_worker_id: str | None = Field(default=None, max_length=255)
    repository: str | None = Field(default=None, max_length=512)
    forgejo_issue_number: int | None = Field(default=None, ge=1)
    run_id: str | None = Field(default=None, max_length=36)

    @field_validator("due_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("due_at must include a timezone")
        return value

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> "TaskUpdate":
        for name in ("title", "description", "status", "priority"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class UserRoleUpdate(BaseModel):
    role: Literal["viewer", "member", "manager", "admin"]
    active: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def user_out(user: UserRecord) -> dict[str, Any]:
    return {
        "id": user.id,
        "forgejo_user_id": user.forgejo_user_id,
        "login": user.forgejo_login,
        "display_name": user.display_name or user.forgejo_login,
        "email": user.email,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "active": user.active,
        "last_login_at": iso(user.last_login_at),
        "last_activity_at": iso(user.last_activity_at),
    }

def dashboard_user_out(user: UserRecord) -> dict[str, Any]:
    return {
        "id": user.id,
        "login": user.forgejo_login,
        "display_name": user.display_name or user.forgejo_login,
        "role": user.role,
        "active": user.active,
    }


def worker_out(worker: WorkerRecord) -> dict[str, Any]:
    online = worker.last_seen_at >= utcnow() - timedelta(seconds=settings.worker_offline_after_seconds)
    capabilities = worker.capabilities or []
    safe_capabilities = [
        item for item in capabilities
        if isinstance(item, str)
        and len(item) <= 64
        and item.replace("-", "").replace("_", "").replace(".", "").isalnum()
    ] if isinstance(capabilities, list) else []
    return {
        "id": worker.id,
        "role": worker.role,
        "hostname": worker.hostname,
        "status": worker.reported_status if online else "offline",
        "presence_status": "online" if online else "offline",
        "presence_source": "heartbeat",
        "current_run_id": worker.current_run_id,
        "last_seen_at": iso(worker.last_seen_at),
        "version": worker.version,
        "capabilities": safe_capabilities,
    }



async def worker_admission_status(connection: Any, worker: WorkerRecord) -> str:
    if connection is None:
        return "unverified"
    try:
        response = await connection.request(
            f"agent.workers.health.{worker.role}",
            json.dumps({"worker_id": worker.id}).encode("utf-8"),
            timeout=1,
        )
        result = json.loads(response.data.decode("utf-8"))
    except (NoRespondersError, NatsTimeoutError, asyncio.TimeoutError):
        return "unavailable"
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "unavailable"
    except Exception:
        return "unverified"
    if (
        isinstance(result, dict)
        and result.get("worker_id") == worker.id
        and result.get("worker_role") == worker.role
    ):
        return "available"
    return "unavailable"

def task_snapshot(task: TaskRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "due_at": iso(task.due_at),
        "assignee_user_id": task.assignee_user_id,
        "assignee_worker_id": task.assignee_worker_id,
        "repository": task.repository,
        "forgejo_issue_number": task.forgejo_issue_number,
        "run_id": task.run_id,
        "created_by": task.created_by,
        "completed_at": iso(task.completed_at),
        "version": task.version,
        "created_at": iso(task.created_at),
        "updated_at": iso(task.updated_at),
    }


def activity_out(event: ActivityRecord) -> dict[str, Any]:
    source = event.source if event.source in {
        "task", "run", "worker", "access", "forgejo", "auth",
        "dashboard", "estimate_review", "time",
    } else "activity"
    if source == "task":
        known = {"task.created", "task.updated", "task.archived"}
        event_type = event.event_type if event.event_type in known else "task.activity"
        summary = {
            "task.created": "A shared task was created",
            "task.updated": "A shared task was updated",
            "task.archived": "A shared task was archived",
        }.get(event_type, "Shared task activity")
        changed = (event.payload or {}).get("changed_fields")
        safe_payload = {
            "changed_fields": [
                field for field in changed
                if isinstance(field, str) and field in {
                    "title", "description", "status", "priority", "due_at",
                    "assignee_user_id", "assignee_worker_id",
                }
            ]
        } if isinstance(changed, list) else {}
    elif source == "run":
        event_type = event.event_type if event.event_type in {"run.created", "run.rejected"} else "run.activity"
        summary = (
            "A repository run was rejected because no worker was available"
            if event_type == "run.rejected"
            else "A repository run was requested"
        )
        safe_payload = {}
        if event_type == "run.rejected":
            original = event.payload or {}
            if original.get("code") == "NO_WORKER_AVAILABLE":
                safe_payload["code"] = "NO_WORKER_AVAILABLE"
            role = original.get("worker_role")
            if isinstance(role, str) and role.replace("-", "").replace("_", "").isalnum():
                safe_payload["worker_role"] = role[:128]
            target = original.get("target_worker_id")
            if (
                isinstance(target, str)
                and 0 < len(target) <= 255
                and target.replace("-", "").replace("_", "").replace(".", "").isalnum()
            ):
                safe_payload["target_worker_id"] = target
    elif source == "worker":
        known = {"worker.registered", "worker.status", "run.running", "run.completed", "run.failed"}
        event_type = event.event_type if event.event_type in known else "worker.activity"
        run_state = event_type.removeprefix("run.")
        summary = f"A worker {run_state} a run" if event_type.startswith("run.") else (
            "A worker became available" if event_type == "worker.registered" else "Worker status changed"
        )
        original = event.payload or {}
        safe_payload = {
            key: original[key] for key in ("previous_status", "status")
            if isinstance(original.get(key), str)
            and original[key] in {"idle", "busy", "degraded", "offline"}
        }
    elif source == "access":
        event_type = event.event_type if event.event_type in {
            "access.login", "access.auth_failed", "access.session_opened",
            "access.session_closed", "access.privilege_use", "access.sudo_use",
            "access.systemd_unit_state", "access.service_failed",
            "access.service_state", "access.ssh_auth_success", "access.ssh_auth_failure",
            "access.ssh_session_opened", "access.ssh_session_closed", "access.activity",
        } else "access.activity"
        summary = "Node access activity was observed"
        safe_payload = {}
    elif source == "forgejo":
        allowed = {"push", "pull_request", "issues", "create", "delete", "release", "wiki"}
        suffix = event.event_type.removeprefix("forgejo.")
        event_type = f"forgejo.{suffix}" if suffix in allowed else "forgejo.activity"
        summary = "Forgejo repository activity was observed"
        safe_payload = {}
    elif source == "auth":
        event_type = event.event_type if event.event_type in {"auth.login", "auth.logout"} else "auth.activity"
        summary = "A team member signed in or out"
        safe_payload = {}
    elif source == "dashboard":
        event_type = "activity.pulse"
        summary = "Recent dashboard activity was observed"
        safe_payload = {}
    elif source == "estimate_review":
        event_type = "estimate.reviewed"
        summary = "An activity estimate was reviewed"
        safe_payload = {}
    elif source == "time":
        known = {
            "time.started", "time.stopped", "time.discarded", "time.submitted",
            "time.approved", "time.rejected",
        }
        event_type = event.event_type if event.event_type in known else "time.activity"
        summary = {
            "time.started": "A self-reported timer was started",
            "time.stopped": "A self-reported timer was stopped",
            "time.discarded": "A self-reported timer was discarded",
            "time.submitted": "A self-reported timer was submitted for review",
            "time.approved": "A self-reported timer was approved",
            "time.rejected": "A self-reported timer was rejected",
        }.get(event_type, "Self-reported time activity")
        original = event.payload or {}
        safe_payload = {}
        session_id = original.get("work_session_id")
        if isinstance(session_id, str) and 0 < len(session_id) <= 36:
            safe_payload["session_id"] = session_id
        allowed_statuses = {"running", "stopped", "submitted", "approved", "rejected", "deleted"}
        status_value = original.get("status")
        if isinstance(status_value, str) and status_value in allowed_statuses:
            safe_payload["status"] = status_value
        duration = original.get("duration_seconds")
        if type(duration) is int and 0 <= duration <= 604800:
            safe_payload["duration_seconds"] = duration
        previous_status = original.get("previous_status")
        if isinstance(previous_status, str) and previous_status in allowed_statuses:
            safe_payload["previous_status"] = previous_status
    else:
        event_type = "activity.recorded"
        summary = "Dashboard activity was recorded"
        safe_payload = {}
    safe_payload.pop("undo", None)
    return {
        "id": event.id,
        "source": source,
        "event_type": event_type,
        "actor_user_id": event.actor_user_id,
        "summary": summary,
        "payload": safe_payload,
        "occurred_at": iso(event.occurred_at),
    }



async def record_activity(
    session: AsyncSession,
    *,
    source: str,
    source_event_id: str,
    event_type: str,
    summary: str,
    occurred_at: datetime | None = None,
    actor_user_id: str | None = None,
    worker_id: str | None = None,
    repository: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ActivityRecord:
    event = ActivityRecord(
        id=str(uuid.uuid4()),
        source=source,
        source_event_id=source_event_id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        worker_id=worker_id,
        repository=repository,
        task_id=task_id,
        run_id=run_id,
        summary=summary[:1000],
        payload=payload or {},
        occurred_at=occurred_at or utcnow(),
    )
    session.add(event)
    return event


async def upsert_forgejo_user(
    session: AsyncSession, data: dict[str, Any], *, allow_unlisted: bool = False
) -> UserRecord | None:
    login = data.get("login") or data.get("username")
    if not isinstance(login, str) or not login:
        return None
    normalized_login = login.strip().lower()
    if normalized_login == "api-admin" or normalized_login.startswith("service:"):
        raise HTTPException(status_code=409, detail="Reserved dashboard identity")
    if normalized_login not in settings.dashboard_members:
        if allow_unlisted:
            return None
        raise HTTPException(status_code=403, detail="Forgejo account is not allowlisted for the dashboard")
    forgejo_id = data.get("id")
    if type(forgejo_id) is not int or forgejo_id <= 0:
        return None
    user = await session.scalar(select(UserRecord).where(UserRecord.forgejo_user_id == forgejo_id))
    collision = await session.scalar(
        select(UserRecord).where(func.lower(UserRecord.forgejo_login) == normalized_login)
    )
    if collision is not None and (user is None or collision.id != user.id):
        raise HTTPException(status_code=409, detail="Forgejo login conflicts with an existing dashboard identity")
    if user is None:
        user = UserRecord(
            id=str(uuid.uuid4()),
            forgejo_user_id=forgejo_id,
            forgejo_login=login,
            role="admin" if normalized_login in settings.admin_logins else "member",
            active=True,
        )
        session.add(user)
    elif user.role not in {"admin", "manager"}:
        user.role = "member"
    user.forgejo_login = login
    user.display_name = str(data.get("full_name") or data.get("display_name") or login)[:255]
    user.email = str(data["email"])[:320] if data.get("email") else None
    user.avatar_url = str(data["avatar_url"])[:2048] if data.get("avatar_url") else None
    user.last_activity_at = utcnow()
    return user


def ensure_task_permission(user: UserRecord, task: TaskRecord | None = None) -> None:
    del task
    if user.role not in {"admin", "manager", "member"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task modification is not permitted")

async def validate_task_links(session: AsyncSession, values: dict[str, Any]) -> None:
    checks = (
        ("assignee_user_id", UserRecord, "assignee user"),
        ("assignee_worker_id", WorkerRecord, "assignee worker"),
        ("run_id", RunRecord, "run"),
    )
    for field, model, label in checks:
        value = values.get(field)
        if value is not None:
            linked = await session.get(model, value)
            if linked is None or (
                field == "assignee_user_id"
                and (
                    not linked.active
                    or linked.role == "service"
                    or linked.forgejo_login.lower() not in settings.dashboard_members
                )
            ):
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Unknown {label}")


@router.get("/auth/forgejo/start")
async def forgejo_login() -> Response:
    if not settings.oauth_configured:
        raise HTTPException(status_code=503, detail="Forgejo OAuth is not configured")
    state = create_oauth_state()
    callback = f"{settings.public_url.rstrip('/')}/auth/forgejo/callback"
    query = urlencode(
        {
            "client_id": settings.forgejo_oauth_client_id,
            "redirect_uri": callback,
            "response_type": "code",
            "state": state,
        }
    )
    response = RedirectResponse(f"{settings.forgejo_public_url.rstrip('/')}/login/oauth/authorize?{query}")
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=settings.session_secure,
        samesite="lax",
    )
    return response


@router.get("/auth/forgejo/callback")
async def forgejo_callback(
    code: str,
    state: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE, "")
    if not cookie_state or not hmac.compare_digest(cookie_state, state) or not validate_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    callback = f"{settings.public_url.rstrip('/')}/auth/forgejo/callback"
    internal_base = settings.forgejo_api_url.removesuffix("/api/v1")
    async with httpx.AsyncClient(timeout=15) as client:
        token_response = await client.post(
            f"{internal_base}/login/oauth/access_token",
            data={
                "client_id": settings.forgejo_oauth_client_id,
                "client_secret": settings.forgejo_oauth_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": callback,
            },
            headers={"Accept": "application/json"},
        )
        if token_response.status_code != 200:
            raise HTTPException(status_code=502, detail="Forgejo rejected the OAuth exchange")
        access_token = token_response.json().get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise HTTPException(status_code=502, detail="Forgejo OAuth response had no access token")
        user_response = await client.get(
            f"{settings.forgejo_api_url.rstrip('/')}/user",
            headers={"Authorization": f"token {access_token}"},
        )
        if user_response.status_code != 200:
            raise HTTPException(status_code=502, detail="Could not read the Forgejo user")
    user = await upsert_forgejo_user(session, user_response.json())
    if user is None:
        raise HTTPException(status_code=502, detail="Forgejo returned an invalid user")
    if not user.active:
        raise HTTPException(status_code=403, detail="Dashboard account is disabled")
    user.last_login_at = utcnow()
    await session.flush()
    await record_activity(
        session,
        source="auth",
        source_event_id=f"login:{uuid.uuid4()}",
        event_type="auth.login",
        actor_user_id=user.id,
        summary=f"{user.display_name or user.forgejo_login} signed in through Forgejo",
        payload={"method": "forgejo_oauth"},
    )
    await session.commit()
    token, _ = create_session(user.id)
    response = RedirectResponse("/dashboard/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=12 * 60 * 60,
        httponly=True,
        secure=settings.session_secure,
        samesite="lax",
    )
    response.delete_cookie(OAUTH_STATE_COOKIE)
    return response




@router.post("/auth/logout")
async def logout(
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await record_activity(
        session,
        source="auth",
        source_event_id=f"logout:{uuid.uuid4()}",
        event_type="auth.logout",
        actor_user_id=actor.id,
        summary=f"{actor.display_name or actor.forgejo_login} signed out",
    )
    await session.commit()
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/v1/auth/config")
async def auth_config() -> dict[str, bool]:
    return {"forgejo_oauth": settings.oauth_configured}


@router.get("/v1/session")
async def get_session_info(request: Request, user: UserRecord = Depends(session_user)) -> dict[str, Any]:
    payload = session_payload(request) or {}
    return {
        "user": {
            "id": user.id,
            "login": user.forgejo_login,
            "display_name": user.display_name or user.forgejo_login,
            "avatar_url": user.avatar_url,
            "role": user.role,
        },
        "csrf_token": payload.get("csrf"),
    }


@router.get("/v1/dashboard/summary")
async def dashboard_summary(
    user: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    del user
    now = utcnow()
    offline_cutoff = now - timedelta(seconds=settings.worker_offline_after_seconds)
    task_counts = dict(
        (await session.execute(
            select(TaskRecord.status, func.count())
            .where(TaskRecord.deleted_at.is_(None))
            .group_by(TaskRecord.status)
        )).all()
    )
    run_counts = dict(
        (await session.execute(
            select(RunRecord.status, func.count()).group_by(RunRecord.status)
        )).all()
    )
    workers_online = await session.scalar(
        select(func.count()).select_from(WorkerRecord).where(WorkerRecord.last_seen_at >= offline_cutoff)
    )
    workers_busy = await session.scalar(
        select(func.count()).select_from(WorkerRecord).where(
            WorkerRecord.last_seen_at >= offline_cutoff, WorkerRecord.reported_status == "busy"
        )
    )
    from .tracking import estimate_totals

    observed = await estimate_totals(session, now.date(), now.date())
    observed_seconds = sum(
        values["ssh"] + values["dashboard"] for values in observed.values()
    )
    return {
        "tasks": {key: task_counts.get(key, 0) for key in ("todo", "in_progress", "blocked", "done")},
        "workers": {"online": workers_online or 0, "busy": workers_busy or 0},
        "runs": {key: run_counts.get(key, 0) for key in ("queued", "running", "completed", "failed")},
        "observed_seconds_today": observed_seconds,
        "observed_time_is_estimate": True,
    }


@router.get("/v1/users")
async def list_users(
    active: bool | None = None,
    user: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del user
    query = select(UserRecord).order_by(UserRecord.forgejo_login)
    if active is not None:
        query = query.where(UserRecord.active == active)
    users = (await session.execute(query)).scalars().all()
    return [
        dashboard_user_out(item) for item in users
        if item.forgejo_login.lower() in settings.dashboard_members and item.role != "service"
    ]


@router.patch("/v1/users/{user_id}")
async def update_user_role(
    user_id: str,
    body: UserRoleUpdate,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can manage users")
    target = await session.get(UserRecord, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == "service":
        raise HTTPException(status_code=409, detail="Service credentials are managed through deployment configuration")
    if target.id == actor.id and (body.role != "admin" or not body.active):
        raise HTTPException(status_code=409, detail="Administrators cannot remove their own access")
    target.role = body.role
    target.active = body.active
    await session.commit()
    await live_broker.publish("user", user_out(target))
    return user_out(target)


@router.get("/v1/workers")
async def list_workers(
    request: Request,
    user: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del user
    workers = (await session.execute(select(WorkerRecord).order_by(WorkerRecord.id))).scalars().all()
    connection = getattr(request.app.state, "nats", None)
    if (
        connection is None
        or getattr(connection, "is_closed", True)
        or not getattr(connection, "is_connected", False)
    ):
        connection = None
    admission = await asyncio.gather(
        *(worker_admission_status(connection, worker) for worker in workers)
    )
    output = []
    for worker, admission_status in zip(workers, admission):
        item = worker_out(worker)
        item["admission_status"] = admission_status
        item["admission_source"] = (
            "core_nats_health_probe" if admission_status != "unverified" else "unverified"
        )
        output.append(item)
    return output


@router.get("/v1/dashboard/runs")
async def dashboard_runs(
    limit: int = Query(default=100, ge=1, le=200),
    user: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del user
    records = (
        await session.execute(select(RunRecord).order_by(desc(RunRecord.created_at)).limit(limit))
    ).scalars().all()
    statuses = {"queued", "running", "completed", "failed", "cancel_requested", "cancelled"}
    output: list[dict[str, Any]] = []
    for record in records:
        metadata = record.run_metadata or {}
        role = metadata.get("worker_role", "unknown")
        if (
            not isinstance(role, str)
            or len(role) > 128
            or not role.replace("-", "").replace("_", "").isalnum()
        ):
            role = "unknown"
        project_name = metadata.get("project_name")
        if (
            not isinstance(project_name, str)
            or not project_name
            or len(project_name) > 120
            or any(not character.isprintable() for character in project_name)
        ):
            project_name = None
        result = metadata.get("result")
        raw_result_code = result.get("returncode") if isinstance(result, dict) else None
        result_code = (
            raw_result_code
            if type(raw_result_code) is int and -255 <= raw_result_code <= 255
            else None
        )
        worker_id = record.worker_id
        if (
            not isinstance(worker_id, str)
            or len(worker_id) > 255
            or not worker_id.replace("-", "").replace("_", "").replace(".", "").isalnum()
        ):
            worker_id = None
        run_status = record.status if record.status in statuses else "unknown"
        error_summary = None
        if run_status == "failed":
            suffix = f" (result code {result_code})" if result_code is not None else ""
            error_summary = f"Worker reported a failure{suffix}; raw diagnostics are withheld."
        output.append({
            "id": record.id,
            "request_summary": (
                f"Project: {project_name}"
                if project_name
                else "Automated repository work request"
            ),
            "worker_role": role,
            "status": run_status,
            "worker_id": worker_id,
            "result_code": result_code,
            "error_summary": error_summary,
            "created_at": iso(record.created_at),
            "updated_at": iso(record.updated_at),
        })
    return output


@router.get("/v1/tasks")
async def list_tasks(
    task_status: str | None = Query(default=None, alias="status"),
    assignee_user_id: str | None = None,
    overdue: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    user: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del user
    query = select(TaskRecord).where(TaskRecord.deleted_at.is_(None))
    if task_status:
        if task_status not in TASK_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid task status")
        query = query.where(TaskRecord.status == task_status)
    if assignee_user_id:
        query = query.where(TaskRecord.assignee_user_id == assignee_user_id)
    if overdue:
        query = query.where(TaskRecord.due_at < utcnow(), TaskRecord.status.not_in(TERMINAL_TASK_STATUSES))
    query = query.order_by(TaskRecord.due_at.asc().nulls_last(), desc(TaskRecord.created_at)).limit(limit)
    return [task_snapshot(task) for task in (await session.execute(query)).scalars().all()]


@router.post("/v1/tasks", status_code=201)
async def create_task(
    body: TaskCreate,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    ensure_task_permission(actor)
    values = body.model_dump()
    await validate_task_links(session, values)
    task = TaskRecord(
        id=str(uuid.uuid4()),
        created_by=actor.id,
        completed_at=utcnow() if body.status in TERMINAL_TASK_STATUSES else None,
        **values,
    )
    session.add(task)
    await session.flush()
    snapshot = task_snapshot(task)
    session.add(
        TaskAuditRecord(
            id=str(uuid.uuid4()), task_id=task.id, actor_user_id=actor.id,
            action="created", before=None, after=snapshot,
        )
    )
    event = await record_activity(
        session,
        source="task",
        source_event_id=f"task-created:{task.id}",
        event_type="task.created",
        actor_user_id=actor.id,
        task_id=task.id,
        summary="A shared task was created",
        payload={
            "status": task.status,
            "priority": task.priority,
            "undo": {"kind": "task", "before": None, "after": snapshot},
        },
    )
    await session.commit()
    await session.refresh(task)
    output = task_snapshot(task)
    await live_broker.publish("task", output)
    await live_broker.publish("activity", activity_out(event))
    return {**output, "undo_id": event.id}


@router.get("/v1/tasks/{task_id}")
async def get_task(
    task_id: str,
    user: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    del user
    task = await session.get(TaskRecord, task_id)
    if task is None or task.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Task not found")
    audits = (
        await session.execute(
            select(TaskAuditRecord).where(TaskAuditRecord.task_id == task_id).order_by(desc(TaskAuditRecord.created_at)).limit(100)
        )
    ).scalars().all()
    output = task_snapshot(task)
    output["audit"] = [
        {
            "id": item.id, "actor_user_id": item.actor_user_id, "action": item.action,
            "before": item.before, "after": item.after, "created_at": iso(item.created_at),
        }
        for item in audits
    ]
    return output


@router.patch("/v1/tasks/{task_id}")
async def update_task(
    task_id: str,
    body: TaskUpdate,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await session.get(TaskRecord, task_id, with_for_update=True)
    if task is None or task.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Task not found")
    ensure_task_permission(actor, task)
    if task.version != body.version:
        raise HTTPException(status_code=409, detail={"message": "Task changed since it was loaded", "current": task_snapshot(task)})
    changes = body.model_dump(exclude_unset=True)
    changes.pop("version", None)
    await validate_task_links(session, changes)
    before = task_snapshot(task)
    for field, value in changes.items():
        setattr(task, field, value)
    if "status" in changes:
        task.completed_at = utcnow() if task.status in TERMINAL_TASK_STATUSES else None
    task.version += 1
    await session.flush()
    await session.refresh(task)
    after = task_snapshot(task)
    session.add(
        TaskAuditRecord(
            id=str(uuid.uuid4()), task_id=task.id, actor_user_id=actor.id,
            action="updated", before=before, after=after,
        )
    )
    event = await record_activity(
        session,
        source="task",
        source_event_id=f"task-updated:{task.id}:{task.version}",
        event_type="task.updated",
        actor_user_id=actor.id,
        task_id=task.id,
        summary="A shared task was updated",
        payload={
            "changed_fields": sorted(changes),
            "undo": {"kind": "task", "before": before, "after": after},
        },
    )
    await session.commit()
    await session.refresh(task)
    output = task_snapshot(task)
    await live_broker.publish("task", output)
    await live_broker.publish("activity", activity_out(event))
    return {**output, "undo_id": event.id}


@router.delete("/v1/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> Response:
    task = await session.get(TaskRecord, task_id, with_for_update=True)
    if task is None or task.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Task not found")
    ensure_task_permission(actor, task)
    before = task_snapshot(task)
    task.deleted_at = utcnow()
    task.version += 1
    session.add(
        TaskAuditRecord(
            id=str(uuid.uuid4()), task_id=task.id, actor_user_id=actor.id,
            action="archived", before=before, after=None,
        )
    )
    event = await record_activity(
        session,
        source="task", source_event_id=f"task-archived:{task.id}", event_type="task.archived",
        actor_user_id=actor.id, task_id=task.id,
        summary="A shared task was archived",
        payload={"undo": {"kind": "task", "before": before, "after": {"deleted": True, "version": task.version}}},
    )
    await session.commit()
    await live_broker.publish("task", {"id": task.id, "deleted": True})
    await live_broker.publish("activity", activity_out(event))
    return Response(status_code=204, headers={"X-Undo-ID": event.id})


@router.get("/v1/activity")
async def list_activity(
    source: str | None = None,
    repository: str | None = None,
    limit: int = Query(default=150, ge=1, le=500),
    user: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del user
    query = select(ActivityRecord)
    if source:
        query = query.where(ActivityRecord.source == source)
    if repository:
        query = query.where(ActivityRecord.repository == repository)
    query = query.order_by(desc(ActivityRecord.occurred_at)).limit(limit)
    return [activity_out(item) for item in (await session.execute(query)).scalars().all()]


@router.get("/v1/events/stream")
async def event_stream(user: UserRecord = Depends(session_user)) -> StreamingResponse:
    del user
    return StreamingResponse(
        live_broker.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def parse_forgejo_time(payload: dict[str, Any]) -> datetime:
    candidates = [payload.get("timestamp"), payload.get("created"), payload.get("updated_at")]
    head = payload.get("head_commit")
    if isinstance(head, dict):
        candidates.append(head.get("timestamp"))
    for value in candidates:
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return utcnow()


def forgejo_summary(event_type: str, payload: dict[str, Any], actor: str, repository: str | None) -> str:
    repo = repository or "an unknown repository"
    if event_type == "push":
        commits = payload.get("commits") if isinstance(payload.get("commits"), list) else []
        ref = str(payload.get("ref") or "").removeprefix("refs/heads/")
        return f"{actor} pushed {len(commits)} commit{'s' if len(commits) != 1 else ''} to {repo}:{ref or 'unknown'}"
    if event_type == "pull_request":
        action = payload.get("action", "updated")
        pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
        return f"{actor} {action} pull request #{pr.get('number', '?')} in {repo}"
    if event_type == "issues":
        action = payload.get("action", "updated")
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        return f"{actor} {action} issue #{issue.get('number', '?')} in {repo}"
    if event_type == "create":
        return f"{actor} created {payload.get('ref_type', 'ref')} {payload.get('ref', '')} in {repo}"
    if event_type == "delete":
        return f"{actor} deleted {payload.get('ref_type', 'ref')} {payload.get('ref', '')} in {repo}"
    return f"{actor} triggered {event_type} in {repo}"


def compact_forgejo_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    action = payload.get("action")
    if isinstance(action, str) and action in {
        "opened", "closed", "reopened", "edited", "synchronized",
        "created", "deleted", "published", "released", "merged",
    }:
        result["action"] = action
    if event_type == "push":
        commits = payload.get("commits")
        result["commit_count"] = len(commits) if isinstance(commits, list) else 0
    for key in ("pull_request", "issue", "release"):
        item = payload.get(key)
        if isinstance(item, dict):
            details: dict[str, Any] = {}
            number = item.get("number")
            if type(number) is int and number >= 0:
                details["number"] = number
            state = item.get("state")
            if isinstance(state, str) and state in {"open", "closed", "merged", "draft"}:
                details["state"] = state
            if details:
                result[key] = details
    return result


@router.post("/v1/webhooks/forgejo", status_code=202)
async def forgejo_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if not settings.forgejo_webhook_secret:
        raise HTTPException(status_code=503, detail="Forgejo webhook ingestion is not configured")
    body = await request.body()
    supplied = request.headers.get("X-Forgejo-Signature", "")
    expected = hmac.new(settings.forgejo_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(supplied.lower(), expected):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook body must be an object")
    supplied_type = request.headers.get("X-Forgejo-Event", "unknown").lower()
    event_type = supplied_type if supplied_type in {
        "push", "pull_request", "issues", "create", "delete", "release", "wiki", "package",
    } else "unknown"
    delivery_header = request.headers.get("X-Forgejo-Delivery", "")
    delivery = hashlib.sha256((delivery_header or hashlib.sha256(body).hexdigest()).encode()).hexdigest()
    duplicate = await session.scalar(
        select(ActivityRecord.id).where(ActivityRecord.source == "forgejo", ActivityRecord.source_event_id == delivery)
    )
    if duplicate:
        return {"accepted": True, "duplicate": True}
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    actor_user = await upsert_forgejo_user(session, sender, allow_unlisted=True)
    if actor_user is not None:
        await session.flush()
    repository_data = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    repository = repository_data.get("full_name") if isinstance(repository_data.get("full_name"), str) else None
    event = await record_activity(
        session,
        source="forgejo",
        source_event_id=delivery,
        event_type=f"forgejo.{event_type}",
        actor_user_id=actor_user.id if actor_user else None,
        repository=repository,
        summary="Forgejo repository activity observed",
        occurred_at=parse_forgejo_time(payload),
        payload=compact_forgejo_payload(event_type, payload),
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        duplicate = await session.scalar(
            select(ActivityRecord.id).where(
                ActivityRecord.source == "forgejo",
                ActivityRecord.source_event_id == delivery,
            )
        )
        if duplicate:
            return {"accepted": True, "duplicate": True}
        raise
    output = activity_out(event)
    await live_broker.publish("activity", output)
    return {"accepted": True, "duplicate": False, "event": output}
