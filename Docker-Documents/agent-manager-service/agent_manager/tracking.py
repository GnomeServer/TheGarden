from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import secrets
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import (
    GRANTED_ROLES,
    SESSION_COOKIE,
    api_token_user,
    bearer,
    has_dashboard_access,
    require_session_csrf,
    session_user,
)
from .dashboard import (
    activity_out,
    ensure_task_permission,
    record_activity,
    task_snapshot,
    validate_task_links,
)
from .database import SessionLocal, get_session
from .live import live_broker
from .models import (
    AccessEventRecord,
    ActivityEstimateReviewRecord,
    ActivityRecord,
    IdentityMappingRecord,
    NodeCredentialRecord,
    NotificationRecord,
    TaskAuditRecord,
    TaskRecord,
    UserRecord,
    WorkSessionRecord,
)
from .settings import settings

router = APIRouter()
ACCESS_SOURCES = {"ssh", "sshd", "sudo", "tailscale", "systemd", "caddy", "forgejo", "proxmox"}
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
    source: Literal["ssh", "sshd", "sudo", "tailscale", "systemd", "caddy", "forgejo", "proxmox"]
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


class ActivityPulse(BaseModel):
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    view: Literal["work", "operations"]


class EstimateReview(BaseModel):
    status: Literal["approved", "needs_correction"]
    note: str | None = Field(default=None, max_length=2000)
    adjusted_seconds: int | None = Field(default=None, ge=0, le=86400)

class WorkSessionStart(BaseModel):
    task_id: str | None = Field(default=None, max_length=36)
    user_id: str | None = Field(default=None, max_length=36)
    notes: str = Field(default="", max_length=10000)


class WorkSessionNotes(BaseModel):
    notes: str = Field(default="", max_length=10000)


class WorkSessionDecision(BaseModel):
    approved: bool
    reason: str = Field(default="", max_length=2000)


class WorkSessionEdit(BaseModel):
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=10000)

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("time must include a timezone")
        return value.astimezone(timezone.utc)


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
    event_types = {
        "login", "auth_failed", "session_opened", "session_closed", "privilege_use",
        "ssh_auth_success", "ssh_auth_failure", "ssh_session_opened", "ssh_session_closed",
        "sudo_use", "systemd_unit_state", "service_failed", "service_state",
        "activity", "push", "pull_request", "issues", "health_check",
    }
    identity = record.identity
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.+@-]{1,512}", identity):
        identity = None
    identity_source = (
        record.identity_source
        if record.identity_source in {"linux", "tailscale"}
        else None
    )
    try:
        remote_identity = str(ipaddress.ip_address(record.remote_identity))
    except (TypeError, ValueError):
        remote_identity = None
    metadata = record.event_metadata if isinstance(record.event_metadata, dict) else {}
    target_node = metadata.get("target_name")
    if (
        not isinstance(target_node, str)
        or len(target_node) > 255
        or any(ord(character) < 32 for character in target_node)
    ):
        target_node = None
    source = record.source if record.source in ACCESS_SOURCES else "unknown"
    return {
        "id": record.id,
        "event_id": record.id,
        "node_id": record.node_id,
        "target_node": target_node,
        "user_id": record.user_id,
        "attribution_status": "mapped" if record.user_id else "unmapped",
        "identity": identity,
        "identity_source": identity_source,
        "remote_identity": remote_identity,
        "service": "ssh" if source in {"ssh", "sshd"} else source,
        "event_type": record.event_type if record.event_type in event_types else "activity",
        "source": source,
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
        "approved_by": record.approved_by,
        "approved_at": iso(record.approved_at),
        "created_at": iso(record.created_at),
        "updated_at": iso(record.updated_at),
    }

def work_session_snapshot(record: WorkSessionRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "user_id": record.user_id,
        "task_id": record.task_id,
        "started_at": iso(record.started_at),
        "ended_at": iso(record.ended_at),
        "duration_seconds": record.duration_seconds,
        "source": record.source,
        "status": record.status,
        "notes": record.notes,
        "approved_by": record.approved_by,
        "approved_at": iso(record.approved_at),
    }


def identity_mapping_snapshot(record: IdentityMappingRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "user_id": record.user_id,
        "source": record.source,
        "external_identity": record.external_identity,
    }


def estimate_review_snapshot(record: ActivityEstimateReviewRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "user_id": record.user_id,
        "day": record.day.isoformat(),
        "status": record.status,
        "reviewed_by": record.reviewed_by,
        "note": record.note,
        "adjusted_seconds": record.adjusted_seconds,
        "reviewed_at": iso(record.reviewed_at),
    }


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> UserRecord:
    if request.cookies.get(SESSION_COOKIE):
        user = await session_user(request, session)
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Administrator access required")
        return user
    user = await api_token_user(credentials, session)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator API token required")
    return user


async def estimate_totals(
    session: AsyncSession, from_day: date, to_day: date
) -> dict[tuple[str, date], dict[str, int]]:
    """Human hours estimate from foreground dashboard pulses. SSH is not included."""
    member_rows = (
        await session.execute(
            select(UserRecord.id).where(
                UserRecord.active.is_(True),
                UserRecord.role.in_(GRANTED_ROLES),
            )
        )
    ).all()
    member_ids = {row[0] for row in member_rows}
    if not member_ids:
        return {}
    first = datetime.combine(from_day, datetime.min.time(), tzinfo=timezone.utc)
    after_last = datetime.combine(to_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    output: dict[tuple[str, date], dict[str, int]] = {}

    pulse_rows = (
        await session.execute(
            select(ActivityRecord.actor_user_id, ActivityRecord.occurred_at).where(
                ActivityRecord.source == "dashboard",
                ActivityRecord.event_type == "activity.pulse",
                ActivityRecord.actor_user_id.in_(member_ids),
                ActivityRecord.occurred_at >= first,
                ActivityRecord.occurred_at < after_last,
            )
        )
    ).all()
    pulse_counts: dict[tuple[str, date], int] = {}
    for user_id, occurred in pulse_rows:
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        key = (user_id, occurred.date())
        pulse_counts[key] = pulse_counts.get(key, 0) + 1
    for key, count in pulse_counts.items():
        output.setdefault(key, {"ssh": 0, "dashboard": 0})["dashboard"] = min(43200, count * 60)
    return output


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
    record = await session.scalar(
        select(NodeCredentialRecord).where(NodeCredentialRecord.node_id == body.node_id)
    )
    if record is not None and record.active:
        raise HTTPException(status_code=409, detail="An active credential already exists for this node")
    credential_id = record.id if record is not None else str(uuid.uuid4())
    token = f"dfn_{credential_id}.{secrets.token_urlsafe(32)}"
    if record is None:
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
    else:
        record.label = body.label
        record.key_hash = key_hash(token)
        record.scopes = ["activity:write"]
        record.active = True
        record.created_by = actor.id
        record.created_at = utcnow()
        record.last_used_at = None
    await session.commit()
    await session.refresh(record)
    output = credential_out(record)
    output["token"] = token
    return output


@router.get("/v1/node-credentials")
async def list_node_credentials(
    actor: UserRecord = Depends(api_token_user),
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
    user = await session.get(UserRecord, body.user_id)
    if (
        user is None
        or not has_dashboard_access(user)
    ):
        raise HTTPException(status_code=422, detail="Identity mapping requires an active dashboard member")
    source = body.source.lower()
    existing = await session.scalar(
        select(IdentityMappingRecord)
        .where(
            IdentityMappingRecord.source == source,
            IdentityMappingRecord.external_identity == body.external_identity,
        )
        .with_for_update()
    )
    before = identity_mapping_snapshot(existing) if existing is not None else None
    statement = insert(IdentityMappingRecord).values(
        id=str(uuid.uuid4()),
        user_id=body.user_id,
        source=source,
        external_identity=body.external_identity,
    ).on_conflict_do_update(
        index_elements=[IdentityMappingRecord.source, IdentityMappingRecord.external_identity],
        set_={"user_id": body.user_id},
    ).returning(IdentityMappingRecord.id)
    mapping_id = (await session.execute(statement)).scalar_one()
    mapping = {
        "id": mapping_id,
        "user_id": body.user_id,
        "source": source,
        "external_identity": body.external_identity,
    }
    event = await record_activity(
        session,
        source="identity",
        source_event_id=f"identity-upsert:{mapping_id}:{uuid.uuid4()}",
        event_type="identity_mapping.created" if before is None else "identity_mapping.updated",
        actor_user_id=actor.id,
        summary="An access identity mapping was saved",
        payload={"undo": {"kind": "identity_mapping", "before": before, "after": mapping}},
    )
    await session.commit()
    return {
        "id": mapping_id,
        "user_id": body.user_id,
        "login": user.forgejo_login,
        "source": source,
        "external_identity": body.external_identity,
        "undo_id": event.id,
    }


@router.get("/v1/identity-mappings")
async def list_identity_mappings(
    actor: UserRecord = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del actor
    records = (
        await session.execute(
            select(IdentityMappingRecord, UserRecord.forgejo_login)
            .join(UserRecord, UserRecord.id == IdentityMappingRecord.user_id)
            .order_by(IdentityMappingRecord.user_id, IdentityMappingRecord.source)
        )
    ).all()
    return [
        {
            "id": record.id,
            "user_id": record.user_id,
            "login": login,
            "source": record.source,
            "external_identity": record.external_identity,
        }
        for record, login in records
    ]


@router.delete("/v1/identity-mappings/{mapping_id}", status_code=204)
async def delete_identity_mapping(
    mapping_id: str,
    actor: UserRecord = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    record = await session.get(IdentityMappingRecord, mapping_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Identity mapping not found")
    before = identity_mapping_snapshot(record)
    await session.delete(record)
    event = await record_activity(
        session,
        source="identity",
        source_event_id=f"identity-deleted:{record.id}:{uuid.uuid4()}",
        event_type="identity_mapping.deleted",
        actor_user_id=actor.id,
        summary="An access identity mapping was removed",
        payload={"undo": {"kind": "identity_mapping", "before": before, "after": None}},
    )
    await session.commit()
    return Response(status_code=204, headers={"X-Undo-ID": event.id})

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
            source_event_id=hashlib.sha256(
                f"{body.node_id}\0{item.event_id}".encode("utf-8")
            ).hexdigest(),
            event_type=f"access.{item.event_type if item.event_type in {
                'login', 'auth_failed', 'session_opened', 'session_closed',
                'ssh_auth_success', 'ssh_auth_failure', 'ssh_session_opened', 'ssh_session_closed',
                'privilege_use', 'service_failed', 'service_state',
            } else 'activity'}",
            actor_user_id=mapped_user_id,
            summary="Sanitized node access event",
            occurred_at=item.occurred_at,
            payload={"node_id": body.node_id, "source": item.source},
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
    actor: UserRecord = Depends(session_user),
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


@router.post("/v1/activity/pulse", status_code=202)
async def activity_pulse(
    body: ActivityPulse,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    now = utcnow()
    source_event_id = f"{actor.id}:{body.session_id}:{int(now.timestamp() // 60)}"
    event = None
    existing = await session.scalar(
        select(ActivityRecord.id).where(
            ActivityRecord.source == "dashboard",
            ActivityRecord.source_event_id == source_event_id,
        )
    )
    if existing is None:
        event = await record_activity(
            session,
            source="dashboard",
            source_event_id=source_event_id,
            event_type="activity.pulse",
            actor_user_id=actor.id,
            summary="Foreground dashboard activity pulse",
            occurred_at=now,
            payload={"session_id": body.session_id, "view": body.view},
        )
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            event = None
    actor.last_activity_at = now
    await session.commit()
    if event is not None:
        await live_broker.publish("activity", activity_out(event))
    return {"accepted": True, "duplicate": event is None}


@router.get("/v1/activity/estimates")
async def list_activity_estimates(
    from_day: date = Query(alias="from"),
    to_day: date = Query(alias="to"),
    actor: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    del actor
    if to_day < from_day or (to_day - from_day).days > 30:
        raise HTTPException(status_code=422, detail="Estimate range must be ordered and no longer than 31 days")
    users = (
        await session.execute(
            select(UserRecord).where(UserRecord.active.is_(True), UserRecord.role != "service")
        )
    ).scalars().all()
    members = [user for user in users if has_dashboard_access(user)]
    member_ids = [user.id for user in members]
    totals = await estimate_totals(session, from_day, to_day)
    reviews = (
        await session.execute(
            select(ActivityEstimateReviewRecord).where(
                ActivityEstimateReviewRecord.user_id.in_(member_ids),
                ActivityEstimateReviewRecord.day >= from_day,
                ActivityEstimateReviewRecord.day <= to_day,
            )
        )
    ).scalars().all() if member_ids else []
    review_by_key = {(item.user_id, item.day): item for item in reviews}
    result: list[dict[str, Any]] = []
    for offset in range((to_day - from_day).days + 1):
        day = from_day + timedelta(days=offset)
        for user in members:
            sources = totals.get((user.id, day), {"ssh": 0, "dashboard": 0})
            review = review_by_key.get((user.id, day))
            estimated = sources["dashboard"]
            if review is not None and review.adjusted_seconds is not None:
                estimated = review.adjusted_seconds
            result.append({
                "user_id": user.id,
                "day": day.isoformat(),
                "estimated_seconds": estimated,
                "estimate_label": "Hours-worked estimate for authority. Not SSH time and not worker runtime.",
                "sources": {"dashboard": sources["dashboard"]},
                "review_status": review.status if review is not None else "unreviewed",
                "reviewed_by": review.reviewed_by if review is not None else None,
            })
    return result


@router.post("/v1/activity/estimates/{user_id}/{day}/review")
async def review_activity_estimate(
    user_id: str,
    day: date,
    body: EstimateReview,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if actor.role not in {"admin", "manager", "member"}:
        raise HTTPException(status_code=403, detail="Member access required")
    if actor.id == user_id:
        raise HTTPException(status_code=403, detail="Members cannot review their own estimates")
    target = await session.get(UserRecord, user_id)
    if (
        target is None
        or not has_dashboard_access(target)
    ):
        raise HTTPException(status_code=404, detail="Dashboard member not found")
    query = select(ActivityEstimateReviewRecord).where(
        ActivityEstimateReviewRecord.user_id == user_id,
        ActivityEstimateReviewRecord.day == day,
    ).with_for_update()
    existing = await session.scalar(query)
    before = estimate_review_snapshot(existing) if existing is not None else None
    now = utcnow()
    values = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "day": day,
        "status": body.status,
        "reviewed_by": actor.id,
        "note": body.note,
        "adjusted_seconds": body.adjusted_seconds,
        "reviewed_at": now,
    }
    statement = insert(ActivityEstimateReviewRecord).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[
            ActivityEstimateReviewRecord.user_id,
            ActivityEstimateReviewRecord.day,
        ],
        set_={
            "status": body.status,
            "reviewed_by": actor.id,
            "note": body.note,
            "adjusted_seconds": body.adjusted_seconds,
            "reviewed_at": now,
        },
    )
    await session.execute(statement)
    current = await session.scalar(query.execution_options(populate_existing=True))
    after = estimate_review_snapshot(current)
    event = await record_activity(
        session,
        source="estimate_review",
        source_event_id=str(uuid.uuid4()),
        event_type="estimate.reviewed",
        actor_user_id=actor.id,
        summary="An activity estimate was reviewed",
        occurred_at=now,
        payload={
            "user_id": user_id,
            "day": day.isoformat(),
            "status": body.status,
            "adjusted": body.adjusted_seconds is not None,
            "undo": {"kind": "estimate_review", "before": before, "after": after},
        },
    )
    await session.commit()
    await live_broker.publish("activity", activity_out(event))
    return {
        "user_id": user_id,
        "day": day.isoformat(),
        "review_status": body.status,
        "reviewed_by": actor.id,
        "undo_id": event.id,
    }


@router.post("/v1/work-sessions/start", status_code=201)
async def start_work_session(
    body: WorkSessionStart,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_time_entry_member(actor)
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
        payload={
            "work_session_id": record.id,
            "user_id": user_id,
            "undo": {"kind": "work_session", "before": None, "after": work_session_snapshot(record)},
        },
    )
    await session.commit()
    await session.refresh(record)
    output = work_session_out(record)
    await live_broker.publish("time", output)
    await live_broker.publish("activity", activity_out(activity))
    return {**output, "undo_id": activity.id}


def require_time_entry_member(actor: UserRecord) -> None:
    if actor.role not in {"admin", "manager", "member"}:
        raise HTTPException(status_code=403, detail="Member access required")


async def load_owned_session(
    session: AsyncSession, session_id: str, actor: UserRecord, manager_allowed: bool = True
) -> WorkSessionRecord:
    record = await session.get(WorkSessionRecord, session_id, with_for_update=True)
    if record is None:
        raise HTTPException(status_code=404, detail="Work session not found")
    if record.user_id != actor.id and not (manager_allowed and actor.role in {"admin", "manager"}):
        raise HTTPException(status_code=403, detail="Work session is not owned by this user")
    return record


async def record_work_session_activity(
    session: AsyncSession,
    record: WorkSessionRecord,
    actor: UserRecord,
    *,
    event_type: str,
    source_event_id: str,
    summary: str,
    occurred_at: datetime,
    previous_status: str | None = None,
    undo: dict[str, Any] | None = None,
) -> ActivityRecord:
    payload: dict[str, Any] = {
        "work_session_id": record.id,
        "user_id": record.user_id,
        "task_id": record.task_id,
        "status": record.status,
        "started_at": iso(record.started_at),
        "ended_at": iso(record.ended_at),
        "duration_seconds": record.duration_seconds,
    }
    if previous_status is not None:
        payload["previous_status"] = previous_status
    if undo is not None:
        payload["undo"] = undo
    return await record_activity(
        session,
        source="time",
        source_event_id=source_event_id,
        event_type=event_type,
        actor_user_id=actor.id,
        task_id=record.task_id,
        summary=f"{actor.display_name or actor.forgejo_login} {summary}",
        occurred_at=occurred_at,
        payload=payload,
    )



@router.post("/v1/work-sessions/{session_id}/stop")
async def stop_work_session(
    session_id: str,
    body: WorkSessionNotes,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_time_entry_member(actor)
    record = await load_owned_session(session, session_id, actor)
    if record.status != "running":
        raise HTTPException(status_code=409, detail="Only a running timer can be stopped")
    before = work_session_snapshot(record)
    event_time = utcnow()
    record.ended_at = event_time
    record.duration_seconds = max(0, int((record.ended_at - record.started_at).total_seconds()))
    record.status = "stopped"
    if body.notes:
        record.notes = body.notes
    activity = await record_work_session_activity(
        session,
        record,
        actor,
        event_type="time.stopped",
        source_event_id=f"timer-stopped:{record.id}",
        summary="stopped a work timer",
        occurred_at=event_time,
        undo={"kind": "work_session", "before": before, "after": work_session_snapshot(record)},
    )
    await session.commit()
    await session.refresh(record)
    output = work_session_out(record)
    await live_broker.publish("time", output)
    await live_broker.publish("activity", activity_out(activity))
    return {**output, "undo_id": activity.id}



@router.delete("/v1/work-sessions/{session_id}", status_code=204)
async def discard_work_session(
    session_id: str,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> Response:
    require_time_entry_member(actor)
    record = await load_owned_session(session, session_id, actor, manager_allowed=False)
    if record.status == "deleted":
        return Response(status_code=204)
    if record.source != "timer" or record.status not in {"running", "stopped", "rejected"}:
        raise HTTPException(status_code=409, detail="Only your running, stopped, or rejected timer can be discarded")

    before = work_session_snapshot(record)
    event_time = utcnow()
    previous_status = record.status
    if previous_status == "running":
        record.ended_at = event_time
        record.duration_seconds = max(0, int((event_time - record.started_at).total_seconds()))
    record.status = "deleted"
    activity = await record_work_session_activity(
        session,
        record,
        actor,
        event_type="time.discarded",
        source_event_id=f"timer-discarded:{record.id}",
        summary="discarded a work timer",
        occurred_at=event_time,
        previous_status=previous_status,
        undo={"kind": "work_session", "before": before, "after": work_session_snapshot(record)},
    )
    await session.commit()
    await live_broker.publish(
        "time",
        {"id": record.id, "user_id": record.user_id, "status": "deleted"},
    )
    await live_broker.publish("activity", activity_out(activity))
    return Response(status_code=204, headers={"X-Undo-ID": activity.id})


MAX_EDITED_DURATION_SECONDS = 7 * 24 * 3600


@router.patch("/v1/work-sessions/{session_id}")
async def edit_work_session(
    session_id: str,
    body: WorkSessionEdit,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_time_entry_member(actor)
    record = await load_owned_session(session, session_id, actor, manager_allowed=False)
    if record.source != "timer":
        raise HTTPException(status_code=409, detail="Only a self-reported timer can be edited")
    if record.status not in {"running", "stopped", "rejected"}:
        raise HTTPException(status_code=409, detail="Submitted or approved time cannot be edited")
    if body.started_at is None and body.ended_at is None and body.notes is None:
        raise HTTPException(status_code=422, detail="Provide a start time, end time, or notes")
    before = work_session_snapshot(record)
    now = utcnow()
    started = body.started_at or record.started_at
    ended = record.ended_at if body.ended_at is None else body.ended_at
    if started > now:
        raise HTTPException(status_code=422, detail="Start time cannot be in the future")
    if ended is not None:
        if ended > now:
            raise HTTPException(status_code=422, detail="End time cannot be in the future")
        if ended < started:
            raise HTTPException(status_code=422, detail="End time cannot be before start time")
        duration = max(0, int((ended - started).total_seconds()))
        if duration > MAX_EDITED_DURATION_SECONDS:
            raise HTTPException(status_code=422, detail="Edited duration cannot exceed 7 days")
        record.ended_at = ended
        record.duration_seconds = duration
        if record.status == "running":
            record.status = "stopped"
    else:
        if record.status != "running":
            raise HTTPException(status_code=422, detail="Stopped time needs an end time")
        record.ended_at = None
        record.duration_seconds = None
    record.started_at = started
    if body.notes is not None:
        record.notes = body.notes
    activity = await record_work_session_activity(
        session,
        record,
        actor,
        event_type="time.edited",
        source_event_id=f"timer-edited:{record.id}:{uuid.uuid4()}",
        summary="edited a work timer",
        occurred_at=now,
        previous_status=before["status"],
        undo={"kind": "work_session", "before": before, "after": work_session_snapshot(record)},
    )
    await session.commit()
    await session.refresh(record)
    output = work_session_out(record)
    await live_broker.publish("time", output)
    await live_broker.publish("activity", activity_out(activity))
    return {**output, "undo_id": activity.id}


@router.post("/v1/work-sessions/{session_id}/submit")
async def submit_work_session(
    session_id: str,
    body: WorkSessionNotes,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_time_entry_member(actor)
    record = await load_owned_session(session, session_id, actor, manager_allowed=False)
    if record.status not in {"stopped", "rejected"}:
        raise HTTPException(status_code=409, detail="Only stopped or rejected time can be submitted")
    before = work_session_snapshot(record)
    if body.notes:
        record.notes = body.notes
    record.status = "submitted"
    record.approved_by = None
    record.approved_at = None
    event_time = utcnow()
    activity = await record_work_session_activity(
        session,
        record,
        actor,
        event_type="time.submitted",
        source_event_id=f"timer-submitted:{record.id}:{uuid.uuid4()}",
        summary="submitted a work-time entry",
        occurred_at=event_time,
        undo={"kind": "work_session", "before": before, "after": work_session_snapshot(record)},
    )
    await session.commit()
    await session.refresh(record)
    output = work_session_out(record)
    await live_broker.publish("time", output)
    await live_broker.publish("activity", activity_out(activity))
    return {**output, "undo_id": activity.id}



@router.post("/v1/work-sessions/{session_id}/decision")
async def decide_work_session(
    session_id: str,
    body: WorkSessionDecision,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if actor.role not in {"admin", "manager"}:
        raise HTTPException(status_code=403, detail="Manager access required")
    record = await session.get(WorkSessionRecord, session_id, with_for_update=True)
    if record is None:
        raise HTTPException(status_code=404, detail="Work session not found")
    if record.status != "submitted":
        raise HTTPException(status_code=409, detail="Only submitted time can be reviewed")
    before = work_session_snapshot(record)
    record.status = "approved" if body.approved else "rejected"
    record.approved_by = actor.id if body.approved else None
    record.approved_at = utcnow() if body.approved else None
    if body.reason:
        record.notes = f"{record.notes}\nReview: {body.reason}".strip()
    event_time = utcnow()
    outcome = "approved" if body.approved else "rejected"
    activity = await record_work_session_activity(
        session,
        record,
        actor,
        event_type=f"time.{outcome}",
        source_event_id=f"timer-reviewed:{record.id}:{uuid.uuid4()}",
        summary=f"{outcome} a work-time entry",
        occurred_at=event_time,
        undo={"kind": "work_session", "before": before, "after": work_session_snapshot(record)},
    )
    await session.commit()
    await session.refresh(record)
    output = work_session_out(record)
    await live_broker.publish("time", output)
    await live_broker.publish("activity", activity_out(activity))
    return {**output, "undo_id": activity.id}



@router.get("/v1/work-sessions")
async def list_work_sessions(
    user_id: str | None = None,
    session_status: str | None = Query(default=None, alias="status"),
    from_day: date | None = Query(default=None, alias="from"),
    to_day: date | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=500),
    actor: UserRecord = Depends(session_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(WorkSessionRecord).where(WorkSessionRecord.status != "deleted")
    if actor.role not in {"admin", "manager"}:
        query = query.where(WorkSessionRecord.user_id == actor.id)
    elif user_id:
        query = query.where(WorkSessionRecord.user_id == user_id)
    if session_status:
        if session_status not in WORK_SESSION_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid work-session status")
        query = query.where(WorkSessionRecord.status == session_status)
    if from_day and to_day and to_day < from_day:
        raise HTTPException(status_code=422, detail="Work-session date range must be ordered")
    date_filters = []
    if from_day:
        date_filters.append(
            WorkSessionRecord.started_at >= datetime.combine(from_day, datetime.min.time(), tzinfo=timezone.utc)
        )
    if to_day:
        if to_day == date.max:
            date_filters.append(
                WorkSessionRecord.started_at <= datetime.max.replace(tzinfo=timezone.utc)
            )
        else:
            date_filters.append(
                WorkSessionRecord.started_at
                < datetime.combine(to_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
            )
    if date_filters:
        query = query.where(or_(and_(*date_filters), WorkSessionRecord.status == "running"))
    records = (
        await session.execute(query.order_by(desc(WorkSessionRecord.started_at)).limit(limit))
    ).scalars().all()
    return [work_session_out(record) for record in records]


@router.get("/v1/people/summary")
async def people_summary(
    actor: UserRecord = Depends(session_user),
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
            )
            .where(WorkSessionRecord.status != "deleted")
            .group_by(WorkSessionRecord.user_id, WorkSessionRecord.status)
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
                number = float(value[1])
                if math.isfinite(number):
                    values[name] = round(number, 1)
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
async def node_health(actor: UserRecord = Depends(session_user)) -> dict[str, Any]:
    del actor
    expressions = (
        'max by (node_id, host, worker_id, instance) (up{job=~".*(node-exporter|agent-worker).*|bare-metal-server"})',
        '100 - (avg by (node_id, host, worker_id, instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        '100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))',
        'max by (node_id, host, worker_id, instance) (100 * (1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}))',
    )
    try:
        async with httpx.AsyncClient(timeout=settings.prometheus_timeout) as client:
            up, cpu, memory, disk = await asyncio.gather(
                *(prometheus_query(client, expression) for expression in expressions)
            )
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError, asyncio.TimeoutError):
        return {
            "available": False,
            "nodes": [],
            "error": "Prometheus metrics are temporarily unavailable",
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
    actor: UserRecord = Depends(session_user),
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
            "sent_at": iso(record.sent_at),
            "created_at": iso(record.created_at),
        }
        for record in records
    ]


def parse_undo_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise HTTPException(status_code=409, detail="Undo state is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Undo state is invalid") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=409, detail="Undo state is invalid")
    return parsed


def require_undo_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=409, detail="Undo state is unavailable")
    return value


@router.post("/v1/undo/{undo_id}")
async def undo_dashboard_action(
    undo_id: str,
    actor: UserRecord = Depends(require_session_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    event = await session.get(ActivityRecord, undo_id, with_for_update=True)
    if event is None or event.actor_user_id != actor.id:
        raise HTTPException(status_code=404, detail="Undo action not found")
    undo = (event.payload or {}).get("undo")
    if not isinstance(undo, dict):
        raise HTTPException(status_code=409, detail="This action cannot be undone")
    if (utcnow() - event.occurred_at).total_seconds() > 30:
        raise HTTPException(status_code=410, detail="The undo window expired")
    replay = await session.scalar(
        select(ActivityRecord.id).where(
            ActivityRecord.source == "undo",
            ActivityRecord.source_event_id == event.id,
        )
    )
    if replay:
        raise HTTPException(status_code=409, detail="This action was already undone")

    kind = undo.get("kind")
    publish_topic: str | None = None
    publish_value: dict[str, Any] | None = None

    if kind == "task":
        task_id = event.task_id
        before = undo.get("before")
        after = require_undo_snapshot(undo.get("after"))
        if not task_id:
            raise HTTPException(status_code=409, detail="The task can no longer be undone")
        task = await session.get(TaskRecord, task_id, with_for_update=True)
        if task is None:
            raise HTTPException(status_code=409, detail="The task no longer exists")
        ensure_task_permission(actor, task)
        if type(after.get("version")) is not int or task.version != after["version"]:
            raise HTTPException(status_code=409, detail="The task changed after this action")
        current = task_snapshot(task)
        if before is None:
            if task.deleted_at is not None:
                raise HTTPException(status_code=409, detail="The task is already archived")
            task.deleted_at = utcnow()
            task.version += 1
            audit_after = None
            activity_type = "task.archived"
        elif after.get("deleted") is True:
            if task.deleted_at is None:
                raise HTTPException(status_code=409, detail="The task is no longer archived")
            task.deleted_at = None
            task.version += 1
            audit_after = task_snapshot(task)
            activity_type = "task.restored"
        else:
            before = require_undo_snapshot(before)
            if task.deleted_at is not None:
                raise HTTPException(status_code=409, detail="The task is archived")
            await validate_task_links(session, before)
            for field in (
                "title", "description", "status", "priority", "assignee_user_id",
                "assignee_worker_id", "repository", "forgejo_issue_number", "run_id",
            ):
                setattr(task, field, before.get(field))
            task.due_at = parse_undo_datetime(before.get("due_at"))
            task.completed_at = parse_undo_datetime(before.get("completed_at"))
            task.version += 1
            audit_after = task_snapshot(task)
            activity_type = "task.updated"
        session.add(
            TaskAuditRecord(
                id=str(uuid.uuid4()),
                task_id=task.id,
                actor_user_id=actor.id,
                action="undone",
                before=current,
                after=audit_after,
            )
        )
        publish_topic = "task"
        publish_value = (
            {"id": task.id, "deleted": True}
            if task.deleted_at is not None
            else task_snapshot(task)
        )
        undo_summary = "A task action was undone"
        del activity_type

    elif kind == "work_session":
        before = undo.get("before")
        after = require_undo_snapshot(undo.get("after"))
        record = await session.get(WorkSessionRecord, after.get("id"), with_for_update=True)
        if record is None or record.source != "timer":
            raise HTTPException(status_code=409, detail="The time entry no longer exists")
        current = work_session_snapshot(record)
        if current != after:
            raise HTTPException(status_code=409, detail="The time entry changed after this action")
        if before is None:
            record.status = "deleted"
            record.ended_at = utcnow()
            record.duration_seconds = 0
            record.approved_by = None
            record.approved_at = None
            undo_summary = "A timer start was undone"
        else:
            before = require_undo_snapshot(before)
            for field in ("user_id", "task_id", "source", "status", "notes", "approved_by"):
                setattr(record, field, before.get(field))
            record.started_at = parse_undo_datetime(before.get("started_at"))
            record.ended_at = parse_undo_datetime(before.get("ended_at"))
            record.duration_seconds = before.get("duration_seconds")
            record.approved_at = parse_undo_datetime(before.get("approved_at"))
            undo_summary = "A time-entry action was undone"
        publish_topic = "time"
        publish_value = (
            {"id": record.id, "user_id": record.user_id, "status": "deleted"}
            if record.status == "deleted"
            else work_session_out(record)
        )

    elif kind == "identity_mapping":
        if actor.role != "admin":
            raise HTTPException(status_code=403, detail="Administrator access required")
        before = undo.get("before")
        after = undo.get("after")
        identity = require_undo_snapshot(after if after is not None else before)
        existing = await session.scalar(
            select(IdentityMappingRecord)
            .where(
                IdentityMappingRecord.source == identity["source"],
                IdentityMappingRecord.external_identity == identity["external_identity"],
            )
            .with_for_update()
        )
        if after is not None:
            after = require_undo_snapshot(after)
            if existing is None or identity_mapping_snapshot(existing) != after:
                raise HTTPException(status_code=409, detail="The identity mapping changed after this action")
            if before is None:
                await session.delete(existing)
            else:
                before = require_undo_snapshot(before)
                existing.user_id = before["user_id"]
        else:
            before = require_undo_snapshot(before)
            if existing is not None:
                raise HTTPException(status_code=409, detail="This identity is already mapped")
            statement = insert(IdentityMappingRecord).values(**before).on_conflict_do_nothing(
                index_elements=[IdentityMappingRecord.source, IdentityMappingRecord.external_identity],
            ).returning(IdentityMappingRecord.id)
            if (await session.execute(statement)).scalar_one_or_none() is None:
                raise HTTPException(status_code=409, detail="This identity is already mapped")
        undo_summary = "An identity mapping action was undone"

    elif kind == "estimate_review":
        if actor.role not in {"admin", "manager", "member"}:
            raise HTTPException(status_code=403, detail="Member access required")
        before = undo.get("before")
        after = require_undo_snapshot(undo.get("after"))
        if actor.id == after.get("user_id"):
            raise HTTPException(status_code=403, detail="Members cannot undo their own estimate review")
        current = await session.scalar(
            select(ActivityEstimateReviewRecord)
            .where(
                ActivityEstimateReviewRecord.user_id == after["user_id"],
                ActivityEstimateReviewRecord.day == date.fromisoformat(after["day"]),
            )
            .with_for_update()
        )
        if current is None or estimate_review_snapshot(current) != after:
            raise HTTPException(status_code=409, detail="The estimate review changed after this action")
        if before is None:
            await session.delete(current)
        else:
            before = require_undo_snapshot(before)
            current.status = before["status"]
            current.reviewed_by = before["reviewed_by"]
            current.note = before["note"]
            current.adjusted_seconds = before["adjusted_seconds"]
            current.reviewed_at = parse_undo_datetime(before["reviewed_at"])
        undo_summary = "An estimate review action was undone"

    else:
        raise HTTPException(status_code=409, detail="This action cannot be undone")

    await session.flush()
    undone = await record_activity(
        session,
        source="undo",
        source_event_id=event.id,
        event_type="action.undone",
        actor_user_id=actor.id,
        task_id=event.task_id,
        summary=undo_summary,
        payload={"target_activity_id": event.id},
    )
    await session.commit()
    if publish_topic and publish_value is not None:
        await live_broker.publish(publish_topic, publish_value)
    await live_broker.publish("activity", activity_out(undone))
    return {"undone": True}
