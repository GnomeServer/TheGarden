from __future__ import annotations

import hashlib
import hmac
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import agent_manager.exposure_delivery as delivery
from agent_manager.models import OMPExposureDeliveryRecord
from agent_manager.settings import settings


class MemoryDatabase:
    def __init__(self) -> None:
        self.records: dict[str, OMPExposureDeliveryRecord] = {}


class MemoryResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return iter(self.values)


class MemorySession:
    def __init__(self, database: MemoryDatabase) -> None:
        self.database = database
        self.pending: OMPExposureDeliveryRecord | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _model, event_id: str):
        return self.database.records.get(event_id)

    def add(self, record: OMPExposureDeliveryRecord) -> None:
        self.pending = record

    async def commit(self) -> None:
        if self.pending is not None:
            self.database.records[self.pending.event_id] = self.pending
            self.pending = None

    async def execute(self, _statement):
        now = delivery.utcnow()
        due = [
            record.event_id
            for record in self.database.records.values()
            if record.status == "pending" and record.next_attempt_at <= now
        ]
        return MemoryResult(due)


class OMPExposureDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_outbox_retries_failed_delivery_and_never_stores_raw_key(self) -> None:
        database = MemoryDatabase()
        payload = {
            "event_id": "3d522fa3-c84f-4af9-94d5-877d314db180",
            "key_id": "safe-key-id",
            "key_alias": "agent-manager-model",
            "source": "omp",
            "reason": "configured_credential_echo",
            "occurred_at": "2026-10-22T00:00:00+00:00",
        }
        factory = lambda: MemorySession(database)
        with patch.object(delivery, "SessionLocal", factory), patch.object(
            delivery, "send_exposure_event", new=AsyncMock(return_value=False)
        ) as send:
            delivered = await delivery.queue_omp_exposure_event(payload)

        self.assertFalse(delivered)
        record = database.records[payload["event_id"]]
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.attempts, 1)
        self.assertEqual(record.last_error, "delivery_failed")
        self.assertNotIn("key", record.payload)
        self.assertNotIn("prompt", record.payload)
        self.assertNotIn("manager-secret", repr(record.payload))
        send.assert_awaited_once_with(payload)

        record.next_attempt_at = delivery.utcnow() - timedelta(seconds=1)
        with patch.object(delivery, "SessionLocal", factory), patch.object(
            delivery, "send_exposure_event", new=AsyncMock(return_value=True)
        ) as retry:
            self.assertEqual(await delivery.retry_pending_omp_exposure_events_once(), 1)

        self.assertEqual(record.status, "sent")
        self.assertEqual(record.attempts, 2)
        self.assertIsNotNone(record.delivered_at)
        retry.assert_awaited_once_with(payload)

    async def test_outbound_webhook_uses_configured_hmac_secret(self) -> None:
        captured: dict[str, object] = {}

        class CapturingResponse:
            def raise_for_status(self) -> None:
                return None

        class CapturingClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, *, content, headers):
                captured.update(url=url, content=content, headers=headers)
                return CapturingResponse()

        payload = {
            "event_id": "event-safe",
            "key_alias": "agent-manager-model",
            "reason": "credential_pattern_in_output",
        }
        with patch.object(settings, "exposure_webhook_url", "http://api-guard.test/webhook"), patch.object(
            settings, "exposure_webhook_secret", "inbound-event-secret"
        ), patch("agent_manager.exposure_delivery.httpx.AsyncClient", CapturingClient):
            self.assertTrue(await delivery.send_exposure_event(payload))

        body = captured["content"]
        expected = hmac.new(b"inbound-event-secret", body, hashlib.sha256).hexdigest()
        self.assertEqual(captured["headers"]["X-Darkfactory-Signature"], f"sha256={expected}")
        self.assertNotIn("prompt", body.decode())
        self.assertNotIn("sk-", body.decode())


if __name__ == "__main__":
    unittest.main()
