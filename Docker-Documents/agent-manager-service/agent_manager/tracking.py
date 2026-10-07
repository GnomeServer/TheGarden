from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import current_user, require_csrf
from .dashboard import activity_out, record_activity, task_snapshot, user_out
from .database import SessionLocal, get_session
from .live import live_broker
from .models import (
    AccessEventRecord,
    ActivityRecord,
    IdentityMappingRecord,
    NodeCredentialRecord,
    NotificationRecord,
    TaskRecord,
    UserRecord,
    WorkSessionRecord,
)
from .settings import settings

router = APIRouter()
ACCESS_SOURCES = {"sshd", "sudo", "tailscale", "systemd", "caddy", "forgejo", "proxmox"}
WORK_SESSION_STATUSES = {"running", "stopped", "submitted", "approved", "rejected"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def key_hash(token: str) -> str:
    return hmac.new(settings.signing_key, token.encode("utf-8"), hashlib.sha256).hexdigest()


class NodeCredentialCreate(BaseModel):
    node_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._-]+$")
    label: str = Field(default="", max_length=255)


class IdentityMappingCreate(BaseModel):
    user_id: str = Field(max_length=36)
    source: str = Field(min_length=1, max_length=64)
    external_identity: str = Field(min_length=1, max_length=512)


class AccessEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    source: Literal["sshd", "sudo", "tailscale", "systemd", "caddy", "forgejo", "proxmox"]
    service: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    identity: str | None = Field(default=None, max_length=512)
    identity_source: str | None = Field(default=None, max_length=64)
    session_id: str | None = Field(default=None, max_length=255)
    remote_identity: str | None = Field(default=None, max_length=512)
    summary: str = Field(min_length=1, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        now = utcnow()
        if value > now + timedelta(minutes=5):
            raise ValueError("occurred_at is too far in the future")
        return value

    @field_validator("metadata")
    @classmethod
    def bound_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, separators=(",", ":"), default=str)) > 10000:
            raise ValueError("metadata exceeds 10000 encoded characters")
        return value


class AccessEventBatch(BaseModel):
    node_id: str = Field(min_length=1, max_length=255)
    events: list[AccessEventIn] = Field(min_length=1, max_length=100)


class WorkSessionStart(BaseModel):
    task_id: str | None = Field(default=None, max_length=36)
    user_id: str | None = Field(default=None, max_length=36)
    notes: str = Field(default="", max_length=10000)


class WorkSessionNotes(BaseModel):
    notes: str = Field(default="", max_length=10000)


class WorkSessionDecision(BaseModel):
    approved: bool
    reason: str = Field(default="", max_length=2000)


def credential_out(record: NodeCredentialRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "node_id": record.node_id,
        "label": record.label,
        "scopes": record.scopes or [],
        "active": record.active,
        "created_by": record.created_by,
        "created_at": iso(record.created_at),
        "last_used_at": iso(record.last_used_at),
    }


def access_out(record: AccessEventRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "event_id": record.source_event_id,
        "node_id": record.node_id,
        "user_id": record.user_id,
        "identity": record.identity,
        "identity_source": record.identity_source,
        "service": record.service,
        "event_type": record.event_type,
        "source": record.source,
        "session_id": record.session_id,
        "remote_identity": record.remote_identity,
        "metadata": record.event_metadata or {},
        "occurred_at": iso(record.occurred_at),
        "received_at": iso(record.received_at),
    }


def work_session_out(record: WorkSessionRecord) -> dict[str, Any]:
    duration = record.duration_seconds
    if record.status == "running":
        duration = max(0, int((utcnow() - record.started_at).total_seconds()))
    return {
        "id": record.id,
        "user_id": record.user_id,
        "task_id": record.task_id,
        "started_at": iso(record.started_at),
        "ended_at": iso(record.ended_at),
        "duration_seconds": duration,
        "source": record.source,
        "status": record.status,
        "notes": record.notes,
        "approved_by": record.approved_by,
        "approved_at": iso(record.approved_at),
        "created_at": iso(record.created_at),
        "updated_at": iso(record.updated_at),
    }


async def require_admin(user: UserRecord = Depends(require_csrf)) -> UserRecord:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


async def authenticate_node(
    x_node_key: str | None = Header(default=None, alias="X-Node-Key"),
    session: AsyncSession = Depends(get_session),
) -> NodeCredentialRecord:
    if not x_node_key or not x_node_key.startswith("dfn_") or "." not in x_node_key:
        raise HTTPException(status_code=401, detail="Valid node key required")
    prefix, _ = x_node_key.split(".", 1)
    credential_id = prefix.removeprefix("dfn_")
    record = await session.get(NodeCredentialRecord, credential_id)
    if (
        record is None
        or not record.active
        or "activity:write" not in (record.scopes or [])
        or not hmac.compare_digest(record.key_hash, key_hash(x_node_key))
    ):
        raise HTTPException(status_code=401, detail="Invalid or revoked node key")
    return record


@router.post("/v1/node-credentials", status_code=201)
async def create_node_credential(
    body: NodeCredentialCreate,
    actor: UserRecord = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    existing = await session.scalar(
        select(NodeCredentialRecord.id).where(NodeCredentialRecord.node_id == body.node_id)
    )
    if existing:
        raise HTTPException(status_code=409, detail="A credential already exists for this node")
    credential_id = str(uuid.uuid4())
    token = f"dfn_{credential_id}.{secrets.token_urlsafe(32)}"
    record = NodeCredentialRecord(
        id=credential_id,
        node_id=body.node_id,
        label=body.label,
        key_hash=key_hash(token),
        scopes=["activity:write"],
        active=True,
        created_by=actor.id,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    output = credential_out(record)
    output["token"] = token
    return output


@router.get("/v1/node-credentials")
async def list_node_credentials(
    actor: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    records = (
        await session.execute(select(NodeCredentialRecord).order_by(NodeCredentialRecord.node_id))
    ).scalars().all()
    return [credential_out(record) for record in records]


@router.delete("/v1/node-credentials/{credential_id}", status_code=204)
async def revoke_node_credential(
    credential_id: str,
    actor: UserRecord = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    del actor
    record = await session.get(NodeCredentialRecord, credential_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Node credential not found")
    record.active = False
    await session.commit()
    return Response(status_code=204)


@router.post("/v1/identity-mappings", status_code=201)
async def create_identity_mapping(
    body: IdentityMappingCreate,
    actor: UserRecord = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    del actor
    if await session.get(UserRecord, body.user_id) is None:
        raise HTTPException(status_code=422, detail="Unknown user")
    record = IdentityMappingRecord(
        id=str(uuid.uuid4()),
        user_id=body.user_id,
        source=body.source.lower(),
        external_identity=body.external_identity,
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Identity mapping already exists") from exc
    await session.refresh(record)
    return {
        "id": record.id,
        "user_id": record.user_id,
        "source": record.source,
        "external_identity": record.external_identity,
        "created_at": iso(record.created_at),
    }


@router.get("/v1/identity-mappings")
async def list_identity_mappings(
    actor: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if actor.role not in {"admin", "manager"}:
        raise HTTPException(status_code=403, detail="Manager access required")
    records = (
        await session.execute(
            select(IdentityMappingRecord).order_by(
                IdentityMappingRecord.user_id, IdentityMappingRecord.source
            )
        )
    ).scalars().all()
    return [
        {
            "id": record.id,
            "user_id": record.user_id,
            "source": record.source,
            "external_identity": record.external_identity,
            "created_at": iso(record.created_at),
        }
        for record in records
    ]


@router.post("/v1/ingest/events", status_code=202)
async def ingest_access_events(
    body: AccessEventBatch,
    credential: NodeCredentialRecord = Depends(authenticate_node),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if body.node_id != credential.node_id:
        raise HTTPException(status_code=403, detail="Node key does not match node_id")
    accepted = 0
    duplicates = 0
    emitted: list[dict[str, Any]] = []
    for item in body.events:
        existing = await session.scalar(
            select(AccessEventRecord.id).where(
                AccessEventRecord.node_id == body.node_id,
                AccessEventRecord.source_event_id == item.event_id,
            )
        )
        if existing:
            duplicates += 1
            continue
        mapped_user_id = None
        if item.identity and item.identity_source:
            mapped_user_id = await session.scalar(
                select(IdentityMappingRecord.user_id).where(
                    IdentityMappingRecord.source == item.identity_source.lower(),
                    IdentityMappingRecord.external_identity == item.identity,
                )
            )
        access = AccessEventRecord(
            id=str(uuid.uuid4()),
            source_event_id=item.event_id,
            node_id=body.node_id,
            user_id=mapped_user_id,
            identity=item.identity,
            identity_source=item.identity_source.lower() if item.identity_source else None,
            service=item.service,
            event_type=item.event_type,
            source=item.source,
            session_id=item.session_id,
            remote_identity=item.remote_identity,
            event_metadata=item.metadata,
            occurred_at=item.occurred_at,
        )
        try:
            async with session.begin_nested():
                session.add(access)
                await session.flush()
        except IntegrityError:
            duplicates += 1
            continue
        activity = await record_activity(
            session,
            source="access",
            source_event_id=f"{body.node_id}:{item.event_id}",
            event_type=f"access.{item.event_type}",
            actor_user_id=mapped_user_id,
            summary=item.summary,
            occurred_at=item.occurred_at,
            payload={
                "node_id": body.node_id,
                "service": item.service,
                "source": item.source,
                "identity": item.identity,
                "remote_identity": item.remote_identity,
            },
        )
        emitted.append(activity_out(activity))
        accepted += 1
    credential.last_used_at = utcnow()
    await session.commit()
    for event in emitted:
        await live_broker.publish("activity", event)
    return {"accepted": accepted, "duplicates": duplicates}


@router.get("/v1/access-events")
async def list_access_events(
    node_id: str | None = None,
    user_id: str | None = None,
    source: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    actor: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if actor.role == "viewer":
        raise HTTPException(status_code=403, detail="Member access required")
    query = select(AccessEventRecord)
    if node_id:
        query = query.where(AccessEventRecord.node_id == node_id)
    if user_id:
        query = query.where(AccessEventRecord.user_id == user_id)
    if source:
        if source not in ACCESS_SOURCES:
            raise HTTPException(status_code=422, detail="Unsupported access source")
        query = query.where(AccessEventRecord.source == source)
    records = (
        await session.execute(query.order_by(desc(AccessEventRecord.occurred_at)).limit(limit))
    ).scalars().all()
    return [access_out(record) for record in records]


@router.post("/v1/work-sessions/start", status_code=201)
async def start_work_session(
    body: WorkSessionStart,
    actor: UserRecord = Depends(require_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    user_id = body.user_id or actor.id
    if user_id != actor.id and actor.role not in {"admin", "manager"}:
        raise HTTPException(status_code=403, detail="Cannot start another user's timer")
    if await session.get(UserRecord, user_id) is None:
        raise HTTPException(status_code=422, detail="Unknown user")
    if body.task_id is not None and await session.get(TaskRecord, body.task_id) is None:
        raise HTTPException(status_code=422, detail="Unknown task")
    active = await session.scalar(
        select(WorkSessionRecord.id).where(
            WorkSessionRecord.user_id == user_id,
            WorkSessionRecord.status == "running",
        )
    )
    if active:
        raise HTTPException(status_code=409, detail="User already has a running timer")
    record = WorkSessionRecord(
        id=str(uuid.uuid4()),
        user_id=user_id,
        task_id=body.task_id,
        started_at=utcnow(),
        source="timer",
        status="running",
        notes=body.notes,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="User already has a running timer") from exc
    activity = await record_activity(
        session,
        source="time",
        source_event_id=f"timer-started:{record.id}",
        event_type="time.started",
        actor_user_id=actor.id,
        task_id=record.task_id,
        summary=f"{actor.display_name or actor.forgejo_login} started a work timer",
        payload={"work_session_id": record.id, "user_id": user_id},
    )
    await session.commit()
    await session.refresh(record)
    output = work_session_out(record)
    await live_broker.publish("time", output)
    await live_broker.publish("activity", activity_out(activity))
    return output


async def load_owned_session(
    session: AsyncSession, session_id: str, actor: UserRecord, manager_allowed: bool = True
) -> WorkSessionRecord:
    record = await session.get(WorkSessionRecord, session_id, with_for_update=True)
    if record is None:
        raise HTTPException(status_code=404, detail="Work session not found")
    if record.user_id != actor.id and not (manager_allowed and actor.role in {"admin", "manager"}):
        raise HTTPException(status_code=403, detail="Work session is not owned by this user")
    return record


@router.post("/v1/work-sessions/{session_id}/stop")
async def stop_work_session(
    session_id: str,
    body: WorkSessionNotes,
    actor: UserRecord = Depends(require_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    record = await load_owned_session(session, session_id, actor)
    if record.status != "running":
        raise HTTPException(status_code=409, detail="Only a running timer can be stopped")
    record.ended_at = utcnow()
    record.duration_seconds = max(0, int((record.ended_at - record.started_at).total_seconds()))
    record.status = "stopped"
    if body.notes:
        record.notes = body.notes
    await session.commit()
    await session.refresh(record)
    output = work_session_out(record)
    await live_broker.publish("time", output)
    return output


@router.post("/v1/work-sessions/{session_id}/submit")
async def submit_work_session(
    session_id: str,
    body: WorkSessionNotes,
    actor: UserRecord = Depends(require_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    record = await load_owned_session(session, session_id, actor, manager_allowed=False)
    if record.status not in {"stopped", "rejected"}:
        raise HTTPException(status_code=409, detail="Only stopped or rejected time can be submitted")
    if body.notes:
        record.notes = body.notes
    record.status = "submitted"
    record.approved_by = None
    record.approved_at = None
    await session.commit()
    await session.refresh(record)
    output = work_session_out(record)
    await live_broker.publish("time", output)
    return output


@router.post("/v1/work-sessions/{session_id}/decision")
async def decide_work_session(
    session_id: str,
    body: WorkSessionDecision,
    actor: UserRecord = Depends(require_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if actor.role not in {"admin", "manager"}:
        raise HTTPException(status_code=403, detail="Manager access required")
    record = await session.get(WorkSessionRecord, session_id, with_for_update=True)
    if record is None:
        raise HTTPException(status_code=404, detail="Work session not found")
    if record.status != "submitted":
        raise HTTPException(status_code=409, detail="Only submitted time can be reviewed")
    record.status = "approved" if body.approved else "rejected"
    record.approved_by = actor.id if body.approved else None
    record.approved_at = utcnow() if body.approved else None
    if body.reason:
        record.notes = f"{record.notes}\nReview: {body.reason}".strip()
    await session.commit()
    await session.refresh(record)
    output = work_session_out(record)
    await live_broker.publish("time", output)
    return output


@router.get("/v1/work-sessions")
async def list_work_sessions(
    user_id: str | None = None,
    session_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    actor: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(WorkSessionRecord)
    if actor.role not in {"admin", "manager"}:
        query = query.where(WorkSessionRecord.user_id == actor.id)
    elif user_id:
        query = query.where(WorkSessionRecord.user_id == user_id)
    if session_status:
        if session_status not in WORK_SESSION_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid work-session status")
        query = query.where(WorkSessionRecord.status == session_status)
    records = (
        await session.execute(query.order_by(desc(WorkSessionRecord.started_at)).limit(limit))
    ).scalars().all()
    return [work_session_out(record) for record in records]


@router.get("/v1/people/summary")
async def people_summary(
    actor: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del actor
    users = (await session.execute(select(UserRecord).order_by(UserRecord.forgejo_login))).scalars().all()
    totals = (
        await session.execute(
            select(
                WorkSessionRecord.user_id,
                WorkSessionRecord.status,
                func.coalesce(func.sum(WorkSessionRecord.duration_seconds), 0),
            ).group_by(WorkSessionRecord.user_id, WorkSessionRecord.status)
        )
    ).all()
    indexed: dict[str, dict[str, int]] = {}
    for user_id, session_status, duration in totals:
        indexed.setdefault(user_id, {})[session_status] = int(duration or 0)
    active = {
        record.user_id: work_session_out(record)
        for record in (
            await session.execute(
                select(WorkSessionRecord).where(WorkSessionRecord.status == "running")
            )
        ).scalars().all()
    }
    return [
        {
            "user": user_out(user),
            "seconds": indexed.get(user.id, {}),
            "active_session": active.get(user.id),
        }
        for user in users
    ]


async def prometheus_query(client: httpx.AsyncClient, expression: str) -> list[dict[str, Any]]:
    response = await client.get(
        f"{settings.prometheus_url.rstrip('/')}/api/v1/query", params={"query": expression}
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    result = payload.get("data", {}).get("result", [])
    return result if isinstance(result, list) else []


def vector_by_node(vector: list[dict[str, Any]]) -> dict[str, float]:
    values: dict[str, float] = {}
    for item in vector:
        metric = item.get("metric") if isinstance(item.get("metric"), dict) else {}
        name = metric.get("node_id") or metric.get("worker_id") or metric.get("host") or metric.get("instance")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, list) and len(value) == 2:
            try:
                values[name] = round(float(value[1]), 1)
            except (TypeError, ValueError):
                continue
    return values


def grafana_node_url(host: str = "infra-lab-services") -> str:
    base = settings.grafana_public_url.rstrip("/")
    uid = quote(settings.grafana_node_dashboard_uid, safe="")
    query = urlencode({
        "orgId": "1", "from": "now-6h", "to": "now",
        "timezone": "browser", "var-host": host, "refresh": "30s",
    })
    return f"{base}/d/{uid}/{uid}?{query}"


@router.get("/v1/nodes/health")
async def node_health(actor: UserRecord = Depends(current_user)) -> dict[str, Any]:
    del actor
    expressions = (
        'max by (node_id, host, worker_id, instance) (up{job=~".*(node-exporter|agent-worker).*"})',
        '100 - (avg by (node_id, host, worker_id, instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        '100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))',
        'max by (node_id, host, worker_id, instance) (100 * (1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}))',
    )
    try:
        async with httpx.AsyncClient(timeout=settings.prometheus_timeout) as client:
            up, cpu, memory, disk = await asyncio.gather(
                *(prometheus_query(client, expression) for expression in expressions)
            )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return {
            "available": False,
            "nodes": [],
            "error": str(exc)[:500],
            "grafana_url": grafana_node_url(),
        }
    maps = [vector_by_node(vector) for vector in (up, cpu, memory, disk)]
    names = sorted(set().union(*(mapping.keys() for mapping in maps)))
    hosts = {}
    for vector in (up, cpu, memory, disk):
        for item in vector:
            metric = item.get("metric") or {}
            identity = metric.get("node_id") or metric.get("worker_id") or metric.get("host") or metric.get("instance")
            if identity and metric.get("host"):
                hosts[identity] = metric["host"]
    return {
        "available": True,
        "nodes": [
            {
                "node_id": name,
                "up": bool(maps[0].get(name, 0)),
                "cpu_percent": maps[1].get(name),
                "memory_percent": maps[2].get(name),
                "disk_percent": maps[3].get(name),
                "grafana_url": grafana_node_url(hosts[name]) if name in hosts else None,
            }
            for name in names
        ],
        "grafana_url": grafana_node_url(),
    }


async def scan_deadline_notifications() -> int:
    if not settings.notification_webhook_url:
        return 0
    now = utcnow()
    sent = 0
    async with SessionLocal() as session:
        tasks = (
            await session.execute(
                select(TaskRecord).where(
                    TaskRecord.deleted_at.is_(None),
                    TaskRecord.due_at.is_not(None),
                    TaskRecord.due_at <= now + timedelta(hours=24),
                    TaskRecord.status.not_in({"done", "cancelled"}),
                )
            )
        ).scalars().all()
        async with httpx.AsyncClient(timeout=10) as client:
            for task in tasks:
                remaining = task.due_at - now
                kind = "overdue" if remaining.total_seconds() <= 0 else (
                    "due_2h" if remaining <= timedelta(hours=2) else "due_24h"
                )
                notification_key = f"task:{task.id}:{kind}"
                notification = await session.scalar(
                    select(NotificationRecord).where(
                        NotificationRecord.notification_key == notification_key
                    )
                )
                if notification is not None and notification.status == "sent":
                    continue
                if notification is None:
                    notification = NotificationRecord(
                        id=str(uuid.uuid4()),
                        notification_key=notification_key,
                        task_id=task.id,
                        user_id=task.assignee_user_id,
                        kind=kind,
                        status="pending",
                    )
                    session.add(notification)
                notification.attempts = (notification.attempts or 0) + 1
                payload = {
                    "event": f"task.{kind}",
                    "task": task_snapshot(task),
                    "message": f"Task '{task.title}' is {kind.replace('_', ' ')}",
                }
                headers = {"Content-Type": "application/json"}
                if settings.notification_webhook_token:
                    headers["Authorization"] = f"Bearer {settings.notification_webhook_token}"
                try:
                    response = await client.post(
                        settings.notification_webhook_url, json=payload, headers=headers
                    )
                    response.raise_for_status()
                    notification.status = "sent"
                    notification.sent_at = utcnow()
                    notification.last_error = None
                    sent += 1
                except httpx.HTTPError as exc:
                    notification.status = "failed"
                    notification.last_error = str(exc)[:2000]
                await session.commit()
                await live_broker.publish(
                    "notification",
                    {
                        "id": notification.id,
                        "task_id": task.id,
                        "kind": kind,
                        "status": notification.status,
                    },
                )
    return sent


@router.get("/v1/notifications")
async def list_notifications(
    limit: int = Query(default=100, ge=1, le=500),
    actor: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(NotificationRecord)
    if actor.role not in {"admin", "manager"}:
        query = query.where(NotificationRecord.user_id == actor.id)
    records = (
        await session.execute(query.order_by(desc(NotificationRecord.created_at)).limit(limit))
    ).scalars().all()
    return [
        {
            "id": record.id,
            "task_id": record.task_id,
            "user_id": record.user_id,
            "kind": record.kind,
            "status": record.status,
            "attempts": record.attempts,
            "last_error": record.last_error,
            "sent_at": iso(record.sent_at),
            "created_at": iso(record.created_at),
        }
        for record in records
    ]
