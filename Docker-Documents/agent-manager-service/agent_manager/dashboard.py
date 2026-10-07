from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import (
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    create_oauth_state,
    create_session,
    current_user,
    require_csrf,
    session_payload,
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


class TokenLogin(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


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


def worker_out(worker: WorkerRecord) -> dict[str, Any]:
    online = worker.last_seen_at >= utcnow() - timedelta(seconds=settings.worker_offline_after_seconds)
    return {
        "id": worker.id,
        "role": worker.role,
        "hostname": worker.hostname,
        "status": worker.reported_status if online else "offline",
        "reported_status": worker.reported_status,
        "current_run_id": worker.current_run_id,
        "version": worker.version,
        "capabilities": worker.capabilities or [],
        "last_error": worker.last_error,
        "last_seen_at": iso(worker.last_seen_at),
        "registered_at": iso(worker.registered_at),
    }


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
    return {
        "id": event.id,
        "source": event.source,
        "event_type": event.event_type,
        "actor_user_id": event.actor_user_id,
        "worker_id": event.worker_id,
        "repository": event.repository,
        "task_id": event.task_id,
        "run_id": event.run_id,
        "summary": event.summary,
        "payload": event.payload or {},
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


async def upsert_forgejo_user(session: AsyncSession, data: dict[str, Any]) -> UserRecord | None:
    login = data.get("login") or data.get("username")
    if not isinstance(login, str) or not login:
        return None
    forgejo_id = data.get("id") if isinstance(data.get("id"), int) else None
    query = select(UserRecord).where(UserRecord.forgejo_login == login)
    if forgejo_id is not None:
        query = select(UserRecord).where(
            or_(UserRecord.forgejo_user_id == forgejo_id, UserRecord.forgejo_login == login)
        )
    user = (await session.execute(query)).scalars().first()
    if user is None:
        user = UserRecord(
            id=str(uuid.uuid4()),
            forgejo_user_id=forgejo_id,
            forgejo_login=login,
            role="admin" if login.lower() in settings.admin_logins else "member",
        )
        session.add(user)
    elif forgejo_id is not None:
        user.forgejo_user_id = forgejo_id
    user.forgejo_login = login
    user.display_name = str(data.get("full_name") or data.get("display_name") or login)[:255]
    user.email = str(data["email"])[:320] if data.get("email") else None
    user.avatar_url = str(data["avatar_url"])[:2048] if data.get("avatar_url") else None
    user.last_activity_at = utcnow()
    return user


def ensure_task_permission(user: UserRecord, task: TaskRecord | None = None) -> None:
    if user.role in {"admin", "manager"}:
        return
    if user.role == "member" and (task is None or task.assignee_user_id == user.id or task.created_by == user.id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task modification is not permitted")


async def validate_task_links(session: AsyncSession, values: dict[str, Any]) -> None:
    checks = (
        ("assignee_user_id", UserRecord, "assignee user"),
        ("assignee_worker_id", WorkerRecord, "assignee worker"),
        ("run_id", RunRecord, "run"),
    )
    for field, model, label in checks:
        value = values.get(field)
        if value is not None and await session.get(model, value) is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Unknown {label}")


@router.get("/auth/forgejo/login")
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


@router.post("/auth/token")
async def token_login(body: TokenLogin, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if not settings.api_token or not hmac.compare_digest(body.token.encode(), settings.api_token.encode()):
        raise HTTPException(status_code=401, detail="Invalid API token")
    result = await session.execute(select(UserRecord).where(UserRecord.forgejo_login == "api-admin"))
    user = result.scalar_one_or_none()
    if user is None:
        user = UserRecord(
            id=str(uuid.uuid4()),
            forgejo_login="api-admin",
            display_name="API administrator",
            role="admin",
            active=True,
        )
        session.add(user)
    user.last_login_at = utcnow()
    await session.flush()
    await record_activity(
        session,
        source="auth",
        source_event_id=f"login:{uuid.uuid4()}",
        event_type="auth.login",
        actor_user_id=user.id,
        summary="API administrator signed in with the bootstrap token",
        payload={"method": "bootstrap_token"},
    )
    await session.commit()
    await session.refresh(user)
    token, csrf = create_session(user.id)
    response = Response(
        content=json.dumps({"user": user_out(user), "csrf_token": csrf}),
        media_type="application/json",
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=12 * 60 * 60,
        httponly=True,
        secure=settings.session_secure,
        samesite="lax",
    )
    return response


@router.post("/auth/logout")
async def logout(
    actor: UserRecord = Depends(require_csrf),
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
    return {"forgejo_oauth": settings.oauth_configured, "token_login": True}


@router.get("/v1/session")
async def get_session_info(request: Request, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    payload = session_payload(request)
    return {
        "user": user_out(user),
        "csrf_token": payload.get("csrf") if payload else None,
        "oauth_configured": settings.oauth_configured,
    }


@router.get("/v1/dashboard/summary")
async def dashboard_summary(
    user: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    del user
    now = utcnow()
    offline_cutoff = now - timedelta(seconds=settings.worker_offline_after_seconds)
    task_counts = dict(
        (await session.execute(
            select(TaskRecord.status, func.count()).where(TaskRecord.deleted_at.is_(None)).group_by(TaskRecord.status)
        )).all()
    )
    run_counts = dict((await session.execute(select(RunRecord.status, func.count()).group_by(RunRecord.status))).all())
    workers_online = await session.scalar(select(func.count()).select_from(WorkerRecord).where(WorkerRecord.last_seen_at >= offline_cutoff))
    workers_busy = await session.scalar(
        select(func.count()).select_from(WorkerRecord).where(
            WorkerRecord.last_seen_at >= offline_cutoff, WorkerRecord.reported_status == "busy"
        )
    )
    overdue = await session.scalar(
        select(func.count()).select_from(TaskRecord).where(
            TaskRecord.deleted_at.is_(None),
            TaskRecord.due_at < now,
            TaskRecord.status.not_in(TERMINAL_TASK_STATUSES),
        )
    )
    return {
        "tasks": task_counts,
        "runs": run_counts,
        "workers": {"online": workers_online or 0, "busy": workers_busy or 0},
        "overdue": overdue or 0,
        "generated_at": iso(now),
    }


@router.get("/v1/users")
async def list_users(
    active: bool | None = None,
    user: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del user
    query = select(UserRecord).order_by(UserRecord.forgejo_login)
    if active is not None:
        query = query.where(UserRecord.active == active)
    return [user_out(item) for item in (await session.execute(query)).scalars().all()]


@router.patch("/v1/users/{user_id}")
async def update_user_role(
    user_id: str,
    body: UserRoleUpdate,
    actor: UserRecord = Depends(require_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can manage users")
    target = await session.get(UserRecord, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == actor.id and (body.role != "admin" or not body.active):
        raise HTTPException(status_code=409, detail="Administrators cannot remove their own access")
    target.role = body.role
    target.active = body.active
    await session.commit()
    await live_broker.publish("user", user_out(target))
    return user_out(target)


@router.get("/v1/workers")
async def list_workers(
    user: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del user
    workers = (await session.execute(select(WorkerRecord).order_by(WorkerRecord.id))).scalars().all()
    return [worker_out(worker) for worker in workers]


@router.get("/v1/tasks")
async def list_tasks(
    task_status: str | None = Query(default=None, alias="status"),
    assignee_user_id: str | None = None,
    overdue: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    user: UserRecord = Depends(current_user),
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
    actor: UserRecord = Depends(require_csrf),
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
        summary=f"{actor.display_name or actor.forgejo_login} created task {task.title}",
        payload={"status": task.status, "priority": task.priority},
    )
    await session.commit()
    await session.refresh(task)
    output = task_snapshot(task)
    await live_broker.publish("task", output)
    await live_broker.publish("activity", activity_out(event))
    return output


@router.get("/v1/tasks/{task_id}")
async def get_task(
    task_id: str,
    user: UserRecord = Depends(current_user),
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
    actor: UserRecord = Depends(require_csrf),
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
    if actor.role not in {"admin", "manager"} and any(
        key in changes for key in {"assignee_user_id", "assignee_worker_id", "priority", "due_at"}
    ):
        raise HTTPException(status_code=403, detail="Only managers can reassign or reschedule tasks")
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
        summary=f"{actor.display_name or actor.forgejo_login} updated task {task.title}",
        payload={"changed_fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(task)
    output = task_snapshot(task)
    await live_broker.publish("task", output)
    await live_broker.publish("activity", activity_out(event))
    return output


@router.delete("/v1/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    actor: UserRecord = Depends(require_csrf),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if actor.role not in {"admin", "manager"}:
        raise HTTPException(status_code=403, detail="Only managers can archive tasks")
    task = await session.get(TaskRecord, task_id, with_for_update=True)
    if task is None or task.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Task not found")
    before = task_snapshot(task)
    task.deleted_at = utcnow()
    task.version += 1
    session.add(
        TaskAuditRecord(
            id=str(uuid.uuid4()), task_id=task.id, actor_user_id=actor.id,
            action="archived", before=before, after=None,
        )
    )
    await record_activity(
        session,
        source="task", source_event_id=f"task-archived:{task.id}", event_type="task.archived",
        actor_user_id=actor.id, task_id=task.id,
        summary=f"{actor.display_name or actor.forgejo_login} archived task {task.title}",
    )
    await session.commit()
    await live_broker.publish("task", {"id": task.id, "deleted": True})
    return Response(status_code=204)


@router.get("/v1/activity")
async def list_activity(
    source: str | None = None,
    repository: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: UserRecord = Depends(current_user),
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
async def event_stream(user: UserRecord = Depends(current_user)) -> StreamingResponse:
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
    result: dict[str, Any] = {
        key: payload[key]
        for key in ("action", "ref", "ref_type", "before", "after", "compare_url")
        if key in payload
    }
    if event_type == "push" and isinstance(payload.get("commits"), list):
        result["commits"] = [
            {
                "id": item.get("id"),
                "message": item.get("message"),
                "url": item.get("url"),
                "author": item.get("author"),
                "committer": item.get("committer"),
            }
            for item in payload["commits"][:100]
            if isinstance(item, dict)
        ]
    for key in ("pull_request", "issue", "release"):
        item = payload.get(key)
        if isinstance(item, dict):
            result[key] = {
                name: item.get(name)
                for name in ("id", "number", "title", "state", "html_url", "merged", "created_at", "updated_at")
                if name in item
            }
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
    event_type = request.headers.get("X-Forgejo-Event", "unknown")[:128]
    delivery = request.headers.get("X-Forgejo-Delivery") or hashlib.sha256(body).hexdigest()
    duplicate = await session.scalar(
        select(ActivityRecord.id).where(ActivityRecord.source == "forgejo", ActivityRecord.source_event_id == delivery)
    )
    if duplicate:
        return {"accepted": True, "duplicate": True}
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    actor_user = await upsert_forgejo_user(session, sender)
    if actor_user is not None:
        await session.flush()
    actor = actor_user.display_name if actor_user else str(sender.get("login") or "Forgejo")
    repository_data = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    repository = repository_data.get("full_name") if isinstance(repository_data.get("full_name"), str) else None
    event = await record_activity(
        session,
        source="forgejo",
        source_event_id=delivery,
        event_type=f"forgejo.{event_type}",
        actor_user_id=actor_user.id if actor_user else None,
        repository=repository,
        summary=forgejo_summary(event_type, payload, actor, repository),
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
