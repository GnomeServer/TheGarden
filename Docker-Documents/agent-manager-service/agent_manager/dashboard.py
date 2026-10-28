from __future__ import annotations

import hashlib
import asyncio
import hmac
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, desc, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import (
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    create_oauth_state,
    create_session,
    has_dashboard_access,
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
CANONICAL_WORKERS: tuple[dict[str, Any], ...] = (
    {
        "id": "worker-01", "display_name": "Raphael", "hostname": "raphael",
        "tailnet_ip": "100.116.35.103", "aliases": ("raphael",), "ansible_managed": True,
    },
    {
        "id": "worker-02", "display_name": "Inkii", "hostname": "inkii",
        "tailnet_ip": "100.98.125.127", "aliases": ("inkii", "worker-1", "worker-2"),
        "ansible_managed": True,
    },
    {
        "id": "worker-03", "display_name": "Donatello", "hostname": "donatello",
        "tailnet_ip": "100.94.145.104", "aliases": ("donatello",), "ansible_managed": True,
    },
    {
        "id": "worker-04", "display_name": "Naruto", "hostname": "naruto-dell-pro-max-14-mc14250",
        "tailnet_ip": "100.88.92.78",
        "aliases": ("naruto", "naruto-dell-pro-max-14-mc14250"), "ansible_managed": True,
    },
    {
        "id": "worker-05", "display_name": "Sammy", "hostname": "dell-drd17c4",
        "tailnet_ip": "100.79.110.117", "aliases": ("sammy", "dell-drd17c4"),
        "ansible_managed": True,
    },
    {
        "id": "worker-06", "display_name": "Michelangelo", "hostname": "michelangelo",
        "tailnet_ip": "100.70.246.104", "aliases": ("michelangelo",),
        "ansible_managed": True,
    },
)
WORKER_SPECS_BY_ID = {worker["id"]: worker for worker in CANONICAL_WORKERS}




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
    display_name: str | None = Field(default=None, max_length=255)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None

MACHINE_TITLES = {"api administrator", "api admin"}


def stored_person_name(login: str, raw: str) -> str:
    cleaned = raw.strip()
    if login.lower() in {"api-admin"} or login.lower().startswith("service:"):
        return "API administrator"
    if not cleaned or cleaned.lower() in MACHINE_TITLES:
        return login[:1].upper() + login[1:] if login else "Unassigned"
    return cleaned[:255]


def person_name(user: UserRecord) -> str:
    return stored_person_name(user.forgejo_login or "", user.display_name or "")


def user_out(user: UserRecord) -> dict[str, Any]:
    return {
        "id": user.id,
        "forgejo_user_id": user.forgejo_user_id,
        "login": user.forgejo_login,
        "display_name": person_name(user),
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
        "display_name": person_name(user),
        "role": user.role,
        "active": user.active,
        "last_activity_at": iso(user.last_activity_at),
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


def canonical_worker_id(worker: WorkerRecord) -> str | None:
    worker_id = worker.id.casefold()
    if worker_id in WORKER_SPECS_BY_ID:
        return worker_id
    hostname = (worker.hostname or "").casefold()
    for spec in CANONICAL_WORKERS:
        aliases = spec["aliases"]
        if worker_id in aliases or hostname in aliases or hostname == spec["hostname"]:
            return spec["id"]
    return None


async def canonical_worker_out(
    spec: dict[str, Any],
    candidates: list[WorkerRecord],
    connection: Any,
) -> dict[str, Any]:
    canonical_id = spec["id"]
    canonical_record = next((item for item in candidates if item.id == canonical_id), None)
    record = max(candidates, key=lambda item: item.last_seen_at) if candidates else None

    legacy_ids = sorted({item.id for item in candidates if item.id != canonical_id})
    legacy_online_ids = sorted(
        item.id for item in candidates
        if item.id != canonical_id
        and item.last_seen_at >= utcnow() - timedelta(seconds=settings.worker_offline_after_seconds)
    )
    if record is None:
        return {
            "id": canonical_id,
            "display_name": spec["display_name"],
            "role": "coder",
            "hostname": spec["hostname"],
            "tailnet_ip": spec["tailnet_ip"],
            "status": "offline",
            "presence_status": "offline",
            "presence_source": "no_heartbeat",
            "current_run_id": None,
            "last_seen_at": None,
            "version": None,
            "capabilities": [],
            "admission_status": "unavailable",
            "admission_source": "none",
            "is_roster_member": True,
            "has_record": False,
            "assignable": False,
            "has_canonical_identity": False,
            "ansible_managed": spec["ansible_managed"],
            "enrollment_status": (
                "Ansible inventory; awaiting worker heartbeat"
                if spec["ansible_managed"]
                else "Keybroker-only; SSH/Ansible onboarding pending"
            ),
            "reported_worker_ids": [],
            "legacy_worker_ids": [],
            "legacy_online_worker_ids": [],
        }

    item = worker_out(record)
    item.update(
        {
            "id": canonical_id,
            "display_name": spec["display_name"],
            "record_id": record.id,
            "hostname": record.hostname or spec["hostname"],
            "tailnet_ip": spec["tailnet_ip"],
            "is_roster_member": True,
            "has_canonical_identity": canonical_record is not None,
            "assignable": canonical_record is not None,
            "ansible_managed": spec["ansible_managed"],
            "enrollment_status": (
                "Legacy heartbeat identity; migrate to canonical worker ID"
                if canonical_record is None
                else "Ansible inventory; egress policy not attested"
                if spec["ansible_managed"]
                else "Keybroker-only; no Ansible egress policy"
            ),
            "reported_worker_ids": sorted({candidate.id for candidate in candidates}),
            "legacy_worker_ids": legacy_ids,
            "legacy_online_worker_ids": legacy_online_ids,
        }
    )
    if canonical_record is not None:
        admission_status = await worker_admission_status(connection, canonical_record)
        item["admission_status"] = admission_status
        item["admission_source"] = (
            "core_nats_health_probe" if admission_status != "unverified" else "unverified"
        )
    else:
        item["admission_status"] = "legacy_identity"
        item["admission_source"] = "not_canonical"
    return item


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def run_duration_seconds(
    status: str,
    started_at: datetime | None,
    completed_at: datetime | None,
    *,
    now: datetime | None = None,
) -> int | None:
    if not isinstance(started_at, datetime):
        return None
    if isinstance(completed_at, datetime):
        end = _utc(completed_at)
    elif status == "running":
        end = _utc(now or utcnow())
    else:
        return None
    return max(0, int((end - _utc(started_at)).total_seconds()))


def aggregate_worker_execution(
    rows: list[tuple[Any, ...]],
    from_day: date,
    to_day: date,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Aggregate elapsed worker-run time by worker and UTC day."""
    if to_day < from_day or (to_day - from_day).days > 30:
        raise ValueError("Worker execution range must be ordered and no longer than 31 days")

    current = _utc(now or utcnow())
    first = datetime.combine(from_day, datetime.min.time(), tzinfo=timezone.utc)
    after_last = datetime.combine(to_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    totals: dict[tuple[str, date], dict[str, Any]] = {}

    for worker_id, status, started_at, completed_at in rows:
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or len(worker_id) > 255
            or not worker_id.replace("-", "").replace("_", "").replace(".", "").isalnum()
            or status not in {"running", "completed", "failed"}
            or not isinstance(started_at, datetime)
        ):
            continue
        start = _utc(started_at)
        if start >= current:
            continue
        if completed_at is None:
            if status != "running":
                continue
            end = current
        elif isinstance(completed_at, datetime):
            end = min(_utc(completed_at), current)
        else:
            continue
        if end <= start:
            continue

        cursor = max(start, first)
        finish = min(end, after_last)
        while cursor < finish:
            day = cursor.date()
            boundary = min(
                finish,
                datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
            )
            seconds = int((boundary - cursor).total_seconds())
            if seconds > 0:
                key = (worker_id, day)
                item = totals.setdefault(key, {
                    "worker_id": worker_id,
                    "day": day.isoformat(),
                    "duration_seconds": 0,
                    "run_count": 0,
                    "active_runs": 0,
                })
                item["duration_seconds"] += seconds
                item["run_count"] += 1
                if status == "running":
                    item["active_runs"] += 1
            cursor = boundary

    return sorted(
        totals.values(),
        key=lambda item: (item["day"], item["worker_id"]),
        reverse=True,
    )


async def query_worker_execution(
    session: AsyncSession,
    from_day: date,
    to_day: date,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if to_day < from_day or (to_day - from_day).days > 30:
        raise HTTPException(status_code=422, detail="Worker execution range must be ordered and no longer than 31 days")
    first = datetime.combine(from_day, datetime.min.time(), tzinfo=timezone.utc)
    after_last = datetime.combine(to_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    rows = (
        await session.execute(
            select(
                RunRecord.worker_id,
                RunRecord.status,
                RunRecord.started_at,
                RunRecord.completed_at,
            ).where(
                RunRecord.worker_id.is_not(None),
                RunRecord.started_at.is_not(None),
                RunRecord.status.in_({"running", "completed", "failed"}),
                RunRecord.started_at < after_last,
                or_(RunRecord.completed_at.is_(None), RunRecord.completed_at > first),
            )
        )
    ).all()
    return aggregate_worker_execution(rows, from_day, to_day, now=now)



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
            "time.approved", "time.rejected", "time.edited",
        }
        event_type = event.event_type if event.event_type in known else "time.activity"
        summary = {
            "time.started": "A self-reported timer was started",
            "time.stopped": "A self-reported timer was stopped",
            "time.discarded": "A self-reported timer was discarded",
            "time.submitted": "A self-reported timer was submitted for review",
            "time.approved": "A self-reported timer was approved",
            "time.rejected": "A self-reported timer was rejected",
            "time.edited": "A self-reported timer was edited",
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
    listed = normalized_login in settings.dashboard_members
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
            role="admin",
            active=True,
        )
        session.add(user)
    elif user.active and user.role != "service":
        user.role = "admin"
    elif user.role == "pending":
        user.role = "admin"
        user.active = True
    user.forgejo_login = login
    raw_name = str(data.get("full_name") or data.get("display_name") or "").strip()
    stored = (user.display_name or "").strip()
    if not stored or stored.lower() in MACHINE_TITLES:
        user.display_name = stored_person_name(login, raw_name)
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
                and not has_dashboard_access(linked)
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
    if not user.active or user.role == "pending":
        await session.commit()
        response = RedirectResponse("/dashboard/?access=pending", status_code=303)
        response.delete_cookie(OAUTH_STATE_COOKIE)
        response.delete_cookie(SESSION_COOKIE)
        return response
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
    worker_records = (
        await session.execute(select(WorkerRecord).order_by(WorkerRecord.last_seen_at.desc()))
    ).scalars().all()
    online_worker_ids: set[str] = set()
    busy_worker_ids: set[str] = set()
    for worker in worker_records:
        worker_id = canonical_worker_id(worker)
        if worker_id is None or worker.last_seen_at < offline_cutoff:
            continue
        online_worker_ids.add(worker_id)
        if worker.reported_status == "busy":
            busy_worker_ids.add(worker_id)
    workers_online = len(online_worker_ids)
    workers_busy = len(busy_worker_ids)
    worker_execution = await query_worker_execution(
        session, now.date(), now.date(), now=now
    )
    worker_execution_seconds = sum(
        item["duration_seconds"] for item in worker_execution
    )
    worker_runs_today = sum(item["run_count"] for item in worker_execution)
    return {
        "tasks": {key: task_counts.get(key, 0) for key in ("todo", "in_progress", "blocked", "done")},
        "workers": {"online": workers_online or 0, "busy": workers_busy or 0},
        "runs": {key: run_counts.get(key, 0) for key in ("queued", "running", "completed", "failed")},
        "worker_execution_seconds_today": worker_execution_seconds,
        "worker_runs_today": worker_runs_today,
    }


@router.get("/v1/users")
async def list_users(
    active: bool | None = None,
    actor: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(UserRecord).order_by(UserRecord.forgejo_login)
    if active is not None:
        query = query.where(UserRecord.active == active)
    users = (await session.execute(query)).scalars().all()
    visible = []
    for item in users:
        login = (item.forgejo_login or "").lower()
        if item.role == "service" or login == "api-admin" or login.startswith("service:"):
            continue
        visible.append(dashboard_user_out(item))
    return visible


@router.patch("/v1/users/{user_id}")
async def update_user_role(
    user_id: str,
    body: UserRoleUpdate,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if actor.role not in {"admin", "manager", "member"}:
        raise HTTPException(status_code=403, detail="Board access is required to change status")
    target = await session.get(UserRecord, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == "service":
        raise HTTPException(status_code=409, detail="Service credentials are managed through deployment configuration")
    if target.id == actor.id and (body.role != "admin" or not body.active):
        raise HTTPException(status_code=409, detail="Administrators cannot remove their own access")
    target.role = body.role
    target.active = body.active
    if body.display_name is not None:
        target.display_name = stored_person_name(target.forgejo_login, body.display_name)
    await session.commit()
    await live_broker.publish("user", user_out(target))
    return user_out(target)


@router.delete("/v1/users/{user_id}", status_code=204)
async def delete_dashboard_user(
    user_id: str,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if actor.role not in {"admin", "manager", "member"}:
        raise HTTPException(status_code=403, detail="Board access is required to delete a person")
    target = await session.get(UserRecord, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == "service" or target.forgejo_login.lower().startswith("service:"):
        raise HTTPException(status_code=409, detail="Service accounts cannot be deleted here")
    if target.id == actor.id:
        raise HTTPException(status_code=409, detail="You cannot delete your own account")
    keeper = {"keeper": actor.id, "uid": target.id}
    await session.execute(update(TaskRecord).where(TaskRecord.created_by == target.id).values(created_by=actor.id))
    await session.execute(update(TaskAuditRecord).where(TaskAuditRecord.actor_user_id == target.id).values(actor_user_id=actor.id))
    for statement in (
        "UPDATE node_credentials SET created_by = :keeper WHERE created_by = :uid",
        "UPDATE activity_estimate_reviews SET reviewed_by = :keeper WHERE reviewed_by = :uid",
    ):
        await session.execute(text(statement), keeper)
    await session.delete(target)
    await session.commit()
    await live_broker.publish("user", {"id": target.id, "deleted": True})
    return Response(status_code=204)
@router.get("/v1/workers")
async def list_workers(
    request: Request,
    user: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del user
    workers = (await session.execute(select(WorkerRecord).order_by(WorkerRecord.id))).scalars().all()
    grouped: dict[str, list[WorkerRecord]] = {spec["id"]: [] for spec in CANONICAL_WORKERS}
    unregistered: list[WorkerRecord] = []
    for worker in workers:
        worker_id = canonical_worker_id(worker)
        if worker_id is None:
            unregistered.append(worker)
        else:
            grouped[worker_id].append(worker)
    connection = getattr(request.app.state, "nats", None)
    if (
        connection is None
        or getattr(connection, "is_closed", True)
        or not getattr(connection, "is_connected", False)
    ):
        connection = None
    canonical = await asyncio.gather(
        *(
            canonical_worker_out(spec, grouped[spec["id"]], connection)
            for spec in CANONICAL_WORKERS
        )
    )
    legacy = []
    for worker in unregistered:
        item = worker_out(worker)
        item.update(
            {
                "display_name": f"Unregistered: {worker.id}",
                "is_roster_member": False,
                "has_record": True,
                "has_canonical_identity": False,
                "assignable": False,
                "ansible_managed": False,
                "enrollment_status": "Unregistered legacy heartbeat",
                "reported_worker_ids": [worker.id],
                "legacy_worker_ids": [],
                "legacy_online_worker_ids": [],
                "admission_status": "unavailable",
                "admission_source": "unregistered",
            }
        )
        legacy.append(item)
    return [*canonical, *legacy]


def safe_run_label(value: Any, allowed: set[str]) -> str | None:
    if not isinstance(value, str) or value not in allowed:
        return None
    return value


def safe_script_name(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 128
        or not value.endswith(".py")
        or "/" in value
        or "\\" in value
        or not value[:-3].replace("-", "").replace("_", "").isalnum()
    ):
        return None
    return value


def ledger_state(metadata: dict[str, Any]) -> dict[str, str | None]:
    ledger = metadata.get("ledger") if isinstance(metadata.get("ledger"), dict) else {}
    kind = ledger.get("kind")
    bounce = ledger.get("bounce")
    bounce_to = ledger.get("bounce_to")
    result_summary = ledger.get("result_summary")
    return {
        "ledger": kind if kind in {"mission", "run"} else "run",
        "bounce": bounce if isinstance(bounce, str) and len(bounce) <= 200 and bounce.isprintable() else None,
        "bounce_to": bounce_to if bounce_to in {"omp", "none"} else None,
        "result_summary": (
            result_summary
            if isinstance(result_summary, str) and len(result_summary) <= 200 and result_summary.isprintable()
            else None
        ),
    }


def run_phase(status: str, metadata: dict[str, Any]) -> str:
    if metadata.get("operation") == "chat" and status == "completed":
        return "Luna answered through OMP"
    ledger = ledger_state(metadata)
    if ledger["bounce_to"] == "omp" or status == "bounced":
        return "Bounced to OMP"
    if metadata.get("operation") == "mission" and status == "queued":
        return "Orchestrator handed the sized crew to the worker computers"
    if metadata.get("operation") == "mission" and status == "running":
        return "Worker OMP is running the ephemeral image"
    if metadata.get("operation") == "mission" and status == "completed":
        return "Worker returned the revised result. No second review."
    if metadata.get("operation") == "mission" and status == "needs_input":
        return "Waiting for user clarification after reviewer limit"
    if status == "planning":
        return "Planner is checking the request"
    if status == "coding":
        return "Coder is writing the script"
    if status == "review":
        return "Reviewer is checking the script"
    if status == "accepted":
        return "Accepted by the orchestrator. The crew is starting."
    result = metadata.get("result") if isinstance(metadata.get("result"), dict) else {}
    origin = result.get("source") if isinstance(result, dict) else None
    if status == "queued":
        return "Waiting for a worker"
    if status == "running":
        return "Worker is writing and running the script"
    if status == "completed" and origin == "model":
        return "Finished: model wrote the script, then the worker ran it"
    if status == "completed" and origin == "crew":
        return "Reviewer accepted the script. The crew has been removed."
    if status == "completed" and origin == "provided":
        return "Finished: worker ran the supplied script"
    if status == "completed":
        return "Finished"
    if status == "failed":
        return "Stopped after a worker failure. Raw output is withheld."
    if status == "cancel_requested":
        return "Cancellation requested"
    if status == "cancelled":
        return "Cancelled"
    return "Unknown"


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
    statuses = {"queued", "running", "completed", "failed", "cancel_requested", "cancelled", "needs_input", "accepted", "bounced", "planning", "coding", "review"}
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
        chatter = metadata.get("chatter") if isinstance(metadata.get("chatter"), list) else []
        safe_chatter = [
            line.strip()[:180]
            for line in chatter
            if isinstance(line, str) and line.strip() and "sk-" not in line and "api_key" not in line.casefold()
        ][:12]
        output.append({
            "id": record.id,
            "request_summary": (
                f"Chat: {project_name}"
                if metadata.get("operation") == "chat" and project_name
                else f"Project: {project_name}"
                if project_name
                else "Automated repository work request"
            ),
            "worker_role": role,
            "operation": safe_run_label(metadata.get("operation"), {"create-python-script", "mission", "chat"}),
            "source": safe_run_label(metadata.get("source"), {"open-webui", "api"}) or "unknown",
            "script_name": safe_script_name(metadata.get("script_name")),
            "ledger": ledger_state(metadata)["ledger"],
            "bounce": ledger_state(metadata)["bounce"],
            "result_summary": ledger_state(metadata)["result_summary"],
            "chatter": safe_chatter,
            "phase": run_phase(run_status, metadata),
            "status": run_status,
            "worker_id": worker_id,
            "result_code": result_code,
            "error_summary": error_summary,
            "created_at": iso(record.created_at),
            "started_at": iso(getattr(record, "started_at", None)),
            "duration_seconds": run_duration_seconds(
                run_status,
                getattr(record, "started_at", None),
                getattr(record, "completed_at", None),
            ),
            "updated_at": iso(record.updated_at),
        })
    return output

@router.get("/v1/dashboard/worker-execution")
async def dashboard_worker_execution(
    from_day: date = Query(alias="from"),
    to_day: date = Query(alias="to"),
    user: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del user
    return await query_worker_execution(session, from_day, to_day)




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
