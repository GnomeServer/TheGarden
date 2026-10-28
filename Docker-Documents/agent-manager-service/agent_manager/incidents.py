from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import require_session_csrf, session_user
from .database import SessionLocal, get_session
from .models import ApiGuardIncidentRecord, UserRecord
from .settings import settings


logger = logging.getLogger("agent_manager.incidents")
router = APIRouter()
MAX_EVENT_BODY_BYTES = 4096
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
SAFE_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
KNOWN_CREDENTIAL_PATTERN = re.compile(
    r"(?:sk-(?:proj-|live-|or-v1-)?[A-Za-z0-9_-]{20,}|"
    r"sk-ant-api\d{2}-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,})",
    re.IGNORECASE,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def api_guard_event_secret_configured() -> bool:
    return bool(settings.api_guard_alert_webhook_secret)


def notification_configured() -> bool:
    return bool(settings.notification_webhook_url.strip())


def ack_callback_configured() -> bool:
    return bool(settings.api_guard_alert_ack_url.strip() and settings.api_guard_alert_ack_secret)


def _safe_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not IDENTIFIER_PATTERN.fullmatch(value)
        or value.casefold().startswith("sk-")
        or KNOWN_CREDENTIAL_PATTERN.search(value)
    ):
        raise ValueError("identifier contains unsupported or secret material")
    return value


class APIExposureEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    worker_id: str | None = Field(max_length=128)
    session_id: str | None = Field(max_length=128)
    key_alias: str = Field(min_length=1, max_length=100)
    reason: Literal["configured_credential_echo", "credential_pattern_in_output"]
    occurred_at: datetime

    @field_validator("event_id", "worker_id", "session_id", mode="before")
    @classmethod
    def require_string_identifiers(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, str):
            raise ValueError("identifier must be a string")
        return value


    @field_validator("event_id", "worker_id", "session_id")
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        return _safe_identifier(value)

    @field_validator("key_alias", mode="before")
    @classmethod
    def require_string_alias(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("key alias must be a string")
        return value

    @field_validator("key_alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        if (
            (
                not SAFE_ALIAS_PATTERN.fullmatch(value)
                and value not in {"Restricted key alias", "Unnamed key"}
            )
            or value.casefold().startswith("sk-")
            or KNOWN_CREDENTIAL_PATTERN.search(value)
        ):
            raise ValueError("key alias contains unsupported or secret material")
        return value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def require_timestamp_string(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("occurred_at must be a timestamp string")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(timezone.utc)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


async def _read_event_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_EVENT_BODY_BYTES:
                raise HTTPException(status_code=413, detail="API Guard event is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_EVENT_BODY_BYTES:
            raise HTTPException(status_code=413, detail="API Guard event is too large")
        body.extend(chunk)
    return bytes(body)


async def parse_signed_event(request: Request) -> APIExposureEvent:
    if not api_guard_event_secret_configured():
        raise HTTPException(status_code=503, detail="API Guard event authentication is not configured")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Expected a JSON API Guard event")
    body = await _read_event_body(request)
    signature_headers = request.headers.getlist("X-Darkfactory-Signature")
    supplied = signature_headers[0] if len(signature_headers) == 1 else ""
    if not re.fullmatch(r"sha256=[0-9a-f]{64}", supplied):
        raise HTTPException(status_code=401, detail="Invalid API Guard signature")
    expected = "sha256=" + hmac.new(
        settings.api_guard_alert_webhook_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid API Guard signature")
    try:
        payload = json.loads(body, object_pairs_hook=_reject_duplicate_pairs)
        event = APIExposureEvent.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid API Guard event") from None
    if set(payload) != {"event_id", "worker_id", "session_id", "key_alias", "reason", "occurred_at"}:
        raise HTTPException(status_code=422, detail="Invalid API Guard event fields")
    return event


async def persist_incident(event: APIExposureEvent, session: AsyncSession) -> bool:
    received_at = utcnow()
    relay_configured = notification_configured()
    session.add(
        ApiGuardIncidentRecord(
            event_id=event.event_id,
            worker_id=event.worker_id,
            session_id=event.session_id,
            key_alias=event.key_alias,
            reason=event.reason,
            occurred_at=event.occurred_at,
            received_at=received_at,
            notification_status="pending" if relay_configured else "unconfigured",
            notification_attempts=0,
            notification_next_attempt_at=received_at if relay_configured else None,
            ack_callback_status=None,
            ack_callback_attempts=0,
            escalation_status="waiting_for_delivery" if relay_configured else "unconfigured",
            escalation_level=0,
            escalation_attempts=0,
            escalation_next_attempt_at=None,
            escalation_delivered_at=None,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return False
    await session.commit()
    return True



@router.get("/v1/webhooks/api-guard/status")
async def api_guard_delivery_status(request: Request) -> dict[str, bool]:
    if not api_guard_event_secret_configured():
        raise HTTPException(status_code=503, detail="API Guard status authentication is not configured")
    signature_headers = request.headers.getlist("X-Darkfactory-Signature")
    supplied = signature_headers[0] if len(signature_headers) == 1 else ""
    if not re.fullmatch(r"sha256=[0-9a-f]{64}", supplied):
        raise HTTPException(status_code=401, detail="Invalid API Guard signature")
    message = b"GET:/v1/webhooks/api-guard/status"
    expected = "sha256=" + hmac.new(
        settings.api_guard_alert_webhook_secret.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid API Guard signature")
    return {
        "receiver_configured": api_guard_event_secret_configured(),
        "external_notification_configured": notification_configured(),
        "ack_callback_configured": ack_callback_configured(),
    }

@router.post("/v1/webhooks/api-guard/exposure", status_code=status.HTTP_202_ACCEPTED)
async def receive_api_guard_exposure(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    event = await parse_signed_event(request)
    created = await persist_incident(event, session)
    return {"accepted": True, "duplicate": not created}


async def require_incident_admin(user: UserRecord = Depends(session_user)) -> UserRecord:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


async def require_incident_admin_csrf(
    user: UserRecord = Depends(require_session_csrf),
) -> UserRecord:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def incident_out(record: ApiGuardIncidentRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "worker_id": record.worker_id,
        "session_id": record.session_id,
        "key_alias": record.key_alias,
        "reason": record.reason,
        "occurred_at": record.occurred_at.isoformat(),
        "received_at": record.received_at.isoformat(),
        "notification_status": record.notification_status,
        "notification_attempts": record.notification_attempts,
        "notification_delivered_at": (
            record.notification_delivered_at.isoformat()
            if record.notification_delivered_at
            else None
        ),
        "acknowledged_by": record.acknowledged_by,
        "acknowledged_at": record.acknowledged_at.isoformat() if record.acknowledged_at else None,
        "ack_callback_status": record.ack_callback_status,
        "ack_callback_attempts": record.ack_callback_attempts,
        "ack_callback_delivered_at": (
            record.ack_callback_delivered_at.isoformat()
            if record.ack_callback_delivered_at
            else None
        ),
        "escalation_status": record.escalation_status,
        "escalation_level": record.escalation_level,
        "escalation_attempts": record.escalation_attempts,
        "escalation_next_attempt_at": (
            record.escalation_next_attempt_at.isoformat()
            if record.escalation_next_attempt_at
            else None
        ),
        "escalation_delivered_at": (
            record.escalation_delivered_at.isoformat()
            if record.escalation_delivered_at
            else None
        ),
    }


@router.get("/v1/api-guard/incidents")
async def list_api_guard_incidents(
    limit: int = Query(default=100, ge=1, le=500),
    _actor: UserRecord = Depends(require_incident_admin),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    records = (
        await session.execute(
            select(ApiGuardIncidentRecord)
            .order_by(ApiGuardIncidentRecord.received_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [incident_out(record) for record in records]


@router.post("/v1/api-guard/incidents/{event_id}/ack")
async def acknowledge_api_guard_incident(
    event_id: str,
    actor: UserRecord = Depends(require_incident_admin_csrf),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    record = await session.get(ApiGuardIncidentRecord, event_id, with_for_update=True)
    if record is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    changed = False
    if record.acknowledged_at is None:
        acknowledged_at = utcnow()
        record.acknowledged_by = actor.forgejo_login
        record.acknowledged_at = acknowledged_at
        record.ack_callback_status = "pending" if ack_callback_configured() else "unconfigured"
        record.ack_callback_attempts = 0
        record.ack_callback_next_attempt_at = acknowledged_at if ack_callback_configured() else None
        changed = True
    if record.escalation_status != "acknowledged" or record.escalation_next_attempt_at is not None:
        record.escalation_status = "acknowledged"
        record.escalation_next_attempt_at = None
        changed = True
    if changed:
        await session.commit()
    return incident_out(record)


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * (2 ** min(max(attempts - 1, 0), 7)), 3600))


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _incident_notification_payload(record: ApiGuardIncidentRecord) -> dict[str, Any]:
    return {
        "event": "api_guard.exposure",
        "severity": "critical",
        "event_id": record.event_id,
        "worker_id": record.worker_id,
        "session_id": record.session_id,
        "key_alias": record.key_alias,
        "reason": record.reason,
        "occurred_at": _timestamp(record.occurred_at),
    }

def _incident_escalation_payload(record: ApiGuardIncidentRecord) -> dict[str, Any]:
    payload = _incident_notification_payload(record)
    payload["event"] = "api_guard.exposure.escalated"
    payload["escalation_level"] = record.escalation_level
    return payload


def _ack_callback_payload(record: ApiGuardIncidentRecord) -> dict[str, str]:
    assert record.acknowledged_at is not None and record.acknowledged_by is not None
    return {
        "event_id": record.event_id,
        "acknowledged_by": record.acknowledged_by,
        "acknowledged_at": _timestamp(record.acknowledged_at),
    }


def _signed_headers(body: bytes, secret: str) -> dict[str, str]:
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Darkfactory-Signature": f"sha256={signature}",
    }


async def _post_notification(payload: dict[str, Any]) -> bool:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.notification_webhook_token:
        headers["Authorization"] = f"Bearer {settings.notification_webhook_token}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                settings.notification_webhook_url,
                content=body,
                headers=headers,
            )
            response.raise_for_status()
    except httpx.HTTPError:
        return False
    return True


async def _post_ack_callback(payload: dict[str, str]) -> bool:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                settings.api_guard_alert_ack_url,
                content=body,
                headers=_signed_headers(body, settings.api_guard_alert_ack_secret),
            )
            response.raise_for_status()
    except httpx.HTTPError:
        return False
    return True


async def deliver_incident_notification(event_id: str) -> bool:
    if not notification_configured():
        return False
    now = utcnow()
    async with SessionLocal() as session:
        record = await session.get(ApiGuardIncidentRecord, event_id)
        if record is None or record.notification_status == "delivered":
            return record is not None
        if record.notification_next_attempt_at and record.notification_next_attempt_at > now:
            return False
        payload = _incident_notification_payload(record)
        record.notification_attempts += 1
        record.notification_status = "pending"
        record.notification_next_attempt_at = now + _retry_delay(record.notification_attempts)
        await session.commit()
    delivered = await _post_notification(payload)
    delivered_at = utcnow()
    async with SessionLocal() as session:
        record = await session.get(ApiGuardIncidentRecord, event_id)
        if record is None or record.notification_status == "delivered":
            return delivered
        if delivered:
            record.notification_status = "delivered"
            record.notification_delivered_at = delivered_at
            record.notification_next_attempt_at = None
            record.escalation_attempts = 0
            record.escalation_status = (
                "acknowledged" if record.acknowledged_at is not None
                else "scheduled" if notification_configured()
                else "unconfigured"
            )
            record.escalation_next_attempt_at = (
                delivered_at + timedelta(seconds=settings.api_guard_alert_escalate_after_seconds)
                if record.acknowledged_at is None
                else None
            )
        else:
            record.notification_status = "failed"
            if record.acknowledged_at is not None:
                record.escalation_status = "acknowledged"
                record.escalation_next_attempt_at = None
        await session.commit()
    if not delivered:
        logger.warning("API Guard incident %s remains queued for external notification delivery", event_id)
    return delivered


async def deliver_incident_escalation(event_id: str) -> bool:
    if not notification_configured():
        return False
    now = utcnow()
    async with SessionLocal() as session:
        record = await session.get(ApiGuardIncidentRecord, event_id)
        if (
            record is None
            or record.acknowledged_at is not None
            or record.notification_status != "delivered"
            or record.escalation_status not in {"scheduled", "pending", "failed"}
        ):
            return False
        if record.escalation_next_attempt_at and record.escalation_next_attempt_at > now:
            return False
        if record.escalation_status == "scheduled":
            record.escalation_level += 1
            record.escalation_attempts = 0
        record.escalation_attempts += 1
        payload = _incident_escalation_payload(record)
        record.escalation_status = "pending"
        record.escalation_next_attempt_at = now + _retry_delay(record.escalation_attempts)
        await session.commit()
    delivered = await _post_notification(payload)
    delivered_at = utcnow()
    async with SessionLocal() as session:
        record = await session.get(ApiGuardIncidentRecord, event_id)
        if record is None:
            return delivered
        if record.acknowledged_at is not None:
            record.escalation_status = "acknowledged"
            record.escalation_next_attempt_at = None
        elif delivered:
            record.escalation_status = "scheduled" if notification_configured() else "unconfigured"
            record.escalation_attempts = 0
            record.escalation_delivered_at = delivered_at
            record.escalation_next_attempt_at = (
                delivered_at + timedelta(seconds=settings.api_guard_alert_escalate_after_seconds)
            )
        else:
            record.escalation_status = "failed" if notification_configured() else "unconfigured"
        await session.commit()
    if not delivered:
        logger.warning("API Guard incident %s escalation remains queued for external notification delivery", event_id)
    return delivered


async def deliver_incident_ack_callback(event_id: str) -> bool:
    if not ack_callback_configured():
        return False
    now = utcnow()
    async with SessionLocal() as session:
        record = await session.get(ApiGuardIncidentRecord, event_id)
        if (
            record is None
            or record.acknowledged_at is None
            or record.ack_callback_status == "delivered"
        ):
            return record is not None and record.ack_callback_status == "delivered"
        if record.ack_callback_next_attempt_at and record.ack_callback_next_attempt_at > now:
            return False
        payload = _ack_callback_payload(record)
        record.ack_callback_attempts += 1
        record.ack_callback_status = "pending"
        record.ack_callback_next_attempt_at = now + _retry_delay(record.ack_callback_attempts)
        await session.commit()
    delivered = await _post_ack_callback(payload)
    async with SessionLocal() as session:
        record = await session.get(ApiGuardIncidentRecord, event_id)
        if record is None or record.ack_callback_status == "delivered":
            return delivered
        if delivered:
            record.ack_callback_status = "delivered"
            record.ack_callback_delivered_at = utcnow()
            record.ack_callback_next_attempt_at = None
        else:
            record.ack_callback_status = "failed"
        await session.commit()
    if not delivered:
        logger.warning("API Guard incident %s acknowledgment callback remains queued", event_id)
    return delivered


async def _mark_unconfigured_deliveries() -> None:
    async with SessionLocal() as session:
        changed = False
        records = (
            await session.execute(
                select(ApiGuardIncidentRecord).where(
                    ApiGuardIncidentRecord.notification_status.in_(
                        {"pending", "failed", "unconfigured"}
                    )
                )
            )
        ).scalars().all()
        for record in records:
            if not notification_configured():
                if (
                    record.notification_status != "unconfigured"
                    or record.notification_next_attempt_at is not None
                ):
                    record.notification_status = "unconfigured"
                    record.notification_next_attempt_at = None
                    changed = True
            elif (
                record.notification_status == "unconfigured"
                and record.notification_next_attempt_at is None
            ):
                record.notification_next_attempt_at = utcnow()
                changed = True
        callbacks = (
            await session.execute(
                select(ApiGuardIncidentRecord).where(
                    ApiGuardIncidentRecord.acknowledged_at.is_not(None),
                    ApiGuardIncidentRecord.ack_callback_status.in_(
                        {"pending", "failed", "unconfigured"}
                    ),
                )
            )
        ).scalars().all()
        for record in callbacks:
            if not ack_callback_configured():
                if (
                    record.ack_callback_status != "unconfigured"
                    or record.ack_callback_next_attempt_at is not None
                ):
                    record.ack_callback_status = "unconfigured"
                    record.ack_callback_next_attempt_at = None
                    changed = True
            elif (
                record.ack_callback_status == "unconfigured"
                and record.ack_callback_next_attempt_at is None
            ):
                record.ack_callback_next_attempt_at = utcnow()
                changed = True
        escalations = (
            await session.execute(
                select(ApiGuardIncidentRecord).where(
                    ApiGuardIncidentRecord.acknowledged_at.is_(None),
                    ApiGuardIncidentRecord.escalation_status.in_(
                        {"waiting_for_delivery", "scheduled", "pending", "failed", "unconfigured"}
                    ),
                )
            )
        ).scalars().all()
        for record in escalations:
            if not notification_configured():
                if record.escalation_status != "unconfigured":
                    record.escalation_status = "unconfigured"
                    changed = True
            elif record.notification_status == "delivered":
                if record.escalation_status == "waiting_for_delivery":
                    record.escalation_status = "scheduled"
                    record.escalation_next_attempt_at = (
                        utcnow() + timedelta(seconds=settings.api_guard_alert_escalate_after_seconds)
                    )
                    changed = True
                elif record.escalation_status == "unconfigured":
                    record.escalation_status = (
                        "failed" if record.escalation_attempts else "scheduled"
                    )
                    if record.escalation_next_attempt_at is None:
                        record.escalation_next_attempt_at = (
                            utcnow() + _retry_delay(record.escalation_attempts)
                            if record.escalation_attempts
                            else utcnow() + timedelta(seconds=settings.api_guard_alert_escalate_after_seconds)
                        )
                    changed = True
            elif record.escalation_status in {"scheduled", "pending", "failed", "unconfigured"}:
                record.escalation_status = "waiting_for_delivery"
                record.escalation_next_attempt_at = None
                changed = True
        acknowledged_escalations = (
            await session.execute(
                select(ApiGuardIncidentRecord).where(
                    ApiGuardIncidentRecord.acknowledged_at.is_not(None),
                    or_(
                        ApiGuardIncidentRecord.escalation_status != "acknowledged",
                        ApiGuardIncidentRecord.escalation_next_attempt_at.is_not(None),
                    ),
                )
            )
        ).scalars().all()
        for record in acknowledged_escalations:
            record.escalation_status = "acknowledged"
            record.escalation_next_attempt_at = None
            changed = True
        if changed:
            await session.commit()


async def retry_incident_deliveries_once() -> tuple[int, int, int]:
    await _mark_unconfigured_deliveries()
    now = utcnow()
    notification_ids: list[str] = []
    callback_ids: list[str] = []
    escalation_ids: list[str] = []
    async with SessionLocal() as session:
        if notification_configured():
            notification_ids = list(
                (
                    await session.execute(
                        select(ApiGuardIncidentRecord.event_id)
                        .where(
                            ApiGuardIncidentRecord.notification_status.in_(
                                {"pending", "failed", "unconfigured"}
                            ),
                            or_(
                                ApiGuardIncidentRecord.notification_status == "unconfigured",
                                ApiGuardIncidentRecord.notification_next_attempt_at <= now,
                            ),
                        )
                        .order_by(ApiGuardIncidentRecord.received_at)
                        .limit(100)
                    )
                ).scalars()
            )
            escalation_ids = list(
                (
                    await session.execute(
                        select(ApiGuardIncidentRecord.event_id)
                        .where(
                            ApiGuardIncidentRecord.acknowledged_at.is_(None),
                            ApiGuardIncidentRecord.notification_status == "delivered",
                            ApiGuardIncidentRecord.escalation_status.in_(
                                {"scheduled", "pending", "failed"}
                            ),
                            ApiGuardIncidentRecord.escalation_next_attempt_at <= now,
                        )
                        .order_by(ApiGuardIncidentRecord.escalation_next_attempt_at)
                        .limit(100)
                    )
                ).scalars()
            )
        if ack_callback_configured():
            callback_ids = list(
                (
                    await session.execute(
                        select(ApiGuardIncidentRecord.event_id)
                        .where(
                            ApiGuardIncidentRecord.acknowledged_at.is_not(None),
                            ApiGuardIncidentRecord.ack_callback_status.in_(
                                {"pending", "failed", "unconfigured"}
                            ),
                            or_(
                                ApiGuardIncidentRecord.ack_callback_status == "unconfigured",
                                ApiGuardIncidentRecord.ack_callback_next_attempt_at <= now,
                            ),
                        )
                        .order_by(ApiGuardIncidentRecord.acknowledged_at)
                        .limit(100)
                    )
                ).scalars()
            )
    notifications_delivered = 0
    for event_id in notification_ids:
        if await deliver_incident_notification(event_id):
            notifications_delivered += 1
    callbacks_delivered = 0
    for event_id in callback_ids:
        if await deliver_incident_ack_callback(event_id):
            callbacks_delivered += 1
    escalations_delivered = 0
    for event_id in escalation_ids:
        if await deliver_incident_escalation(event_id):
            escalations_delivered += 1
    return notifications_delivered, callbacks_delivered, escalations_delivered


async def incident_delivery_loop() -> None:
    while True:
        try:
            await retry_incident_deliveries_once()
        except Exception:
            logger.warning("API Guard incident outbox pass failed; queued deliveries will retry")
        await asyncio.sleep(5)
