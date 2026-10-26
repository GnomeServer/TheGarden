from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select

from .database import SessionLocal
from .models import OMPExposureDeliveryRecord
from .settings import settings


logger = logging.getLogger("agent_manager.exposure_delivery")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def send_exposure_event(payload: dict[str, Any]) -> bool:
    if not settings.exposure_webhook_url or not settings.exposure_webhook_secret:
        return False
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        settings.exposure_webhook_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                settings.exposure_webhook_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Darkfactory-Signature": f"sha256={signature}",
                },
            )
            response.raise_for_status()
    except httpx.HTTPError:
        return False
    return True


async def deliver_omp_exposure_event(event_id: str) -> bool:
    async with SessionLocal() as session:
        record = await session.get(OMPExposureDeliveryRecord, event_id)
        if record is None:
            return False
        if record.status == "sent":
            return True
        if record.next_attempt_at and record.next_attempt_at > utcnow():
            return False
        payload = dict(record.payload)

    delivered = await send_exposure_event(payload)
    now = utcnow()
    async with SessionLocal() as session:
        record = await session.get(OMPExposureDeliveryRecord, event_id)
        if record is None or record.status == "sent":
            return delivered
        record.attempts += 1
        if delivered:
            record.status = "sent"
            record.delivered_at = now
            record.last_error = None
        else:
            delay = min(60 * (2 ** min(record.attempts - 1, 6)), 3600)
            record.status = "pending"
            record.next_attempt_at = now + timedelta(seconds=delay)
            record.last_error = "delivery_failed"
        await session.commit()
    if not delivered:
        logger.warning("OMP exposure event %s remains queued for API Guard delivery", event_id)
    return delivered


async def queue_omp_exposure_event(payload: dict[str, Any]) -> bool:
    event_id = payload["event_id"]
    now = utcnow()
    async with SessionLocal() as session:
        record = await session.get(OMPExposureDeliveryRecord, event_id)
        if record is None:
            session.add(
                OMPExposureDeliveryRecord(
                    event_id=event_id,
                    payload=payload,
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            await session.commit()
    return await deliver_omp_exposure_event(event_id)


async def retry_pending_omp_exposure_events_once() -> int:
    now = utcnow()
    async with SessionLocal() as session:
        event_ids = list(
            (
                await session.execute(
                    select(OMPExposureDeliveryRecord.event_id)
                    .where(
                        OMPExposureDeliveryRecord.status == "pending",
                        OMPExposureDeliveryRecord.next_attempt_at <= now,
                    )
                    .order_by(OMPExposureDeliveryRecord.created_at)
                    .limit(100)
                )
            ).scalars()
        )
    delivered = 0
    for event_id in event_ids:
        if await deliver_omp_exposure_event(event_id):
            delivered += 1
    return delivered


async def omp_exposure_delivery_loop() -> None:
    while True:
        try:
            await retry_pending_omp_exposure_events_once()
        except Exception:
            logger.warning("OMP exposure outbox pass failed; pending events will retry")
        await asyncio.sleep(5)
