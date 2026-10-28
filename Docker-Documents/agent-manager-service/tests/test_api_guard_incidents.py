from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

import httpx
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy.exc import IntegrityError

import agent_manager.incidents as incidents
from agent_manager.models import ApiGuardIncidentRecord
from agent_manager.settings import Settings, settings


EVENT_SECRET = "distinct-api-guard-alert-secret"
ACK_SECRET = "distinct-api-guard-ack-secret"


def event_payload(event_id: str = "alert-001") -> dict[str, object]:
    return {
        "event_id": event_id,
        "worker_id": "Raphael",
        "session_id": "session-abc123",
        "key_alias": "worker-session-key",
        "reason": "configured_credential_echo",
        "occurred_at": "2026-10-27T12:00:00+00:00",
    }


def signed_request(payload: bytes, secret: str = EVENT_SECRET, *, content_length: int | None = None) -> Request:
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    headers = [
        (b"content-type", b"application/json"),
        (b"x-darkfactory-signature", f"sha256={signature}".encode()),
    ]
    headers.append((b"content-length", str(content_length if content_length is not None else len(payload)).encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/webhooks/api-guard/exposure",
        "raw_path": b"/v1/webhooks/api-guard/exposure",
        "query_string": b"",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


def signed_status_request(secret: str = EVENT_SECRET) -> Request:
    message = b"GET:/v1/webhooks/api-guard/status"
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/webhooks/api-guard/status",
        "raw_path": b"/v1/webhooks/api-guard/status",
        "query_string": b"",
        "headers": [(b"x-darkfactory-signature", f"sha256={signature}".encode())],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class MemoryDatabase:
    def __init__(self) -> None:
        self.records: dict[str, ApiGuardIncidentRecord] = {}


class ScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class MemoryResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return ScalarRows(self.rows)


class MemorySession:
    def __init__(self, database: MemoryDatabase) -> None:
        self.database = database
        self.pending: ApiGuardIncidentRecord | None = None
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def add(self, record: ApiGuardIncidentRecord) -> None:
        self.pending = record

    async def flush(self) -> None:
        if self.pending is not None and self.pending.event_id in self.database.records:
            raise IntegrityError("insert", None, RuntimeError("duplicate event"))

    async def rollback(self) -> None:
        self.pending = None

    async def get(self, _model, event_id: str, **_kwargs):
        return self.database.records.get(event_id)

    async def commit(self) -> None:
        self.commits += 1
        if self.pending is not None:
            self.database.records[self.pending.event_id] = self.pending
            self.pending = None

    async def execute(self, statement):
        query = str(statement)
        where = query.partition("WHERE")[2]
        selected = query.partition("FROM")[0].strip()
        is_callback = "ack_callback_status" in where
        is_escalation = "escalation_status" in where
        is_id_list = selected == "SELECT api_guard_incidents.event_id"
        now = incidents.utcnow()
        if is_escalation and is_id_list:
            rows = [
                item.event_id for item in self.database.records.values()
                if item.acknowledged_at is None
                and item.notification_status == "delivered"
                and item.escalation_status in {"scheduled", "pending", "failed"}
                and item.escalation_next_attempt_at is not None
                and item.escalation_next_attempt_at <= now
            ]
        elif is_escalation:
            if "acknowledged_at IS NOT NULL" in where:
                rows = [
                    item for item in self.database.records.values()
                    if item.acknowledged_at is not None
                    and item.escalation_status != "acknowledged"
                ]
            else:
                rows = [
                    item for item in self.database.records.values()
                    if item.acknowledged_at is None
                    and item.escalation_status in {
                        "waiting_for_delivery", "scheduled", "pending", "failed", "unconfigured"
                    }
                ]
        elif is_callback and is_id_list:
            rows = [
                item.event_id for item in self.database.records.values()
                if item.acknowledged_at is not None
                and item.ack_callback_status in {"pending", "failed", "unconfigured"}
                and (item.ack_callback_status == "unconfigured" or
                     (item.ack_callback_next_attempt_at is not None and item.ack_callback_next_attempt_at <= now))
            ]
        elif is_callback:
            rows = [
                item for item in self.database.records.values()
                if item.acknowledged_at is not None
                and item.ack_callback_status in {"pending", "failed", "unconfigured"}
            ]
        elif is_id_list:
            rows = [
                item.event_id for item in self.database.records.values()
                if item.notification_status in {"pending", "failed", "unconfigured"}
                and (item.notification_status == "unconfigured" or
                     (item.notification_next_attempt_at is not None and item.notification_next_attempt_at <= now))
            ]
        else:
            rows = [
                item for item in self.database.records.values()
                if item.notification_status in {"pending", "failed", "unconfigured"}
            ]
        return MemoryResult(rows)


def make_incident(*, acknowledged: bool = False) -> ApiGuardIncidentRecord:
    now = incidents.utcnow()
    return ApiGuardIncidentRecord(
        event_id="alert-001",
        worker_id="Raphael",
        session_id="session-abc123",
        key_alias="worker-session-key",
        reason="configured_credential_echo",
        occurred_at=now,
        received_at=now,
        notification_status="pending",
        notification_attempts=0,
        notification_next_attempt_at=now,
        notification_delivered_at=None,
        acknowledged_by="admin-user" if acknowledged else None,
        acknowledged_at=now if acknowledged else None,
        ack_callback_status="pending" if acknowledged else None,
        ack_callback_attempts=0,
        ack_callback_next_attempt_at=now if acknowledged else None,
        ack_callback_delivered_at=None,
        escalation_status="acknowledged" if acknowledged else "waiting_for_delivery",
        escalation_level=0,
        escalation_attempts=0,
        escalation_next_attempt_at=None,
        escalation_delivered_at=None,
    )


class APIExposureReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_contract_accepts_exact_safe_fields(self) -> None:
        body = json.dumps(event_payload(), separators=(",", ":")).encode()
        with patch.object(settings, "api_guard_alert_webhook_secret", EVENT_SECRET):
            parsed = await incidents.parse_signed_event(signed_request(body))
        self.assertEqual(parsed.event_id, "alert-001")
        self.assertEqual(parsed.occurred_at.utcoffset(), timedelta(0))


    async def test_signed_status_reports_only_delivery_configuration(self) -> None:
        with (
            patch.object(settings, "api_guard_alert_webhook_secret", EVENT_SECRET),
            patch.object(settings, "notification_webhook_url", "https://forge.example/private-hook"),
            patch.object(settings, "api_guard_alert_ack_url", "http://api-guard:8000/private"),
            patch.object(settings, "api_guard_alert_ack_secret", ACK_SECRET),
        ):
            result = await incidents.api_guard_delivery_status(signed_status_request())
        self.assertEqual(
            result,
            {
                "receiver_configured": True,
                "external_notification_configured": True,
                "ack_callback_configured": True,
            },
        )
        self.assertEqual(
            set(result),
            {"receiver_configured", "external_notification_configured", "ack_callback_configured"},
        )
        self.assertNotIn("private-hook", repr(result))
        self.assertNotIn(ACK_SECRET, repr(result))

        with (
            patch.object(settings, "api_guard_alert_webhook_secret", EVENT_SECRET),
            patch.object(settings, "notification_webhook_url", ""),
            patch.object(settings, "api_guard_alert_ack_url", ""),
            patch.object(settings, "api_guard_alert_ack_secret", ""),
        ):
            unconfigured = await incidents.api_guard_delivery_status(signed_status_request())
        self.assertEqual(
            unconfigured,
            {
                "receiver_configured": True,
                "external_notification_configured": False,
                "ack_callback_configured": False,
            },
        )

    async def test_signed_status_rejects_invalid_or_missing_authentication(self) -> None:
        with patch.object(settings, "api_guard_alert_webhook_secret", EVENT_SECRET):
            with self.assertRaises(HTTPException) as invalid:
                await incidents.api_guard_delivery_status(signed_status_request("wrong-secret"))
        self.assertEqual(invalid.exception.status_code, 401)

        with patch.object(settings, "api_guard_alert_webhook_secret", ""):
            with self.assertRaises(HTTPException) as missing:
                await incidents.api_guard_delivery_status(signed_status_request())
        self.assertEqual(missing.exception.status_code, 503)

    async def test_receiver_rejects_bad_signature_oversize_and_extra_secret_fields(self) -> None:
        body = json.dumps(event_payload(), separators=(",", ":")).encode()
        with patch.object(settings, "api_guard_alert_webhook_secret", EVENT_SECRET):
            bad_signature_request = signed_request(body, secret="wrong-secret")
            with self.assertRaises(HTTPException) as bad_signature:
                await incidents.parse_signed_event(bad_signature_request)
            self.assertEqual(bad_signature.exception.status_code, 401)

            oversized = signed_request(b"{}", content_length=incidents.MAX_EVENT_BODY_BYTES + 1)
            with self.assertRaises(HTTPException) as too_large:
                await incidents.parse_signed_event(oversized)
            self.assertEqual(too_large.exception.status_code, 413)

            unsafe = event_payload()
            unsafe["secret"] = "never-persist-this"
            unsafe_body = json.dumps(unsafe, separators=(",", ":")).encode()
            with self.assertRaises(HTTPException) as extra_field:
                await incidents.parse_signed_event(signed_request(unsafe_body))
            self.assertEqual(extra_field.exception.status_code, 422)
            unsafe_severity = event_payload()
            unsafe_severity["severity"] = "low"
            unsafe_body = json.dumps(unsafe_severity, separators=(",", ":")).encode()
            with self.assertRaises(HTTPException) as severity_field:
                await incidents.parse_signed_event(signed_request(unsafe_body))
            self.assertEqual(severity_field.exception.status_code, 422)

            unsafe_alias = event_payload()
            unsafe_alias["key_alias"] = "sk-proj-" + "A" * 32
            unsafe_body = json.dumps(unsafe_alias, separators=(",", ":")).encode()
            with self.assertRaises(HTTPException) as credential_alias:
                await incidents.parse_signed_event(signed_request(unsafe_body))
            self.assertEqual(credential_alias.exception.status_code, 422)

    async def test_event_id_is_durably_deduplicated_without_sensitive_payload_storage(self) -> None:
        database = MemoryDatabase()
        parsed = incidents.APIExposureEvent.model_validate(event_payload())
        first_session = MemorySession(database)
        duplicate_session = MemorySession(database)
        self.assertTrue(await incidents.persist_incident(parsed, first_session))
        self.assertFalse(await incidents.persist_incident(parsed, duplicate_session))
        self.assertEqual(set(database.records), {"alert-001"})
        persisted = database.records["alert-001"]
        self.assertFalse(hasattr(persisted, "payload"))
        self.assertFalse(hasattr(persisted, "run_id"))
        self.assertNotIn("prompt", repr(persisted))
        self.assertNotIn("never-persist-this", repr(persisted))
        output = incidents.incident_out(persisted)
        self.assertNotIn("payload", output)
        self.assertNotIn("run_id", output)
        self.assertNotIn("prompt", repr(output))


class IncidentAdminAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledgment_is_admin_only_local_and_idempotent(self) -> None:
        member = SimpleNamespace(role="member")
        with self.assertRaises(HTTPException) as forbidden:
            await incidents.require_incident_admin(member)
        self.assertEqual(forbidden.exception.status_code, 403)
        with self.assertRaises(HTTPException) as forbidden_ack:
            await incidents.require_incident_admin_csrf(member)
        self.assertEqual(forbidden_ack.exception.status_code, 403)

        admin = SimpleNamespace(role="admin", forgejo_login="forgejo-admin")
        record = make_incident()
        session = SimpleNamespace(
            get=AsyncMock(return_value=record),
            commit=AsyncMock(),
        )
        with patch("agent_manager.incidents.ack_callback_configured", return_value=True):
            result = await incidents.acknowledge_api_guard_incident("alert-001", admin, session)
            saved_ack_at = record.acknowledged_at
            repeat = await incidents.acknowledge_api_guard_incident(
                "alert-001", SimpleNamespace(role="admin", forgejo_login="other-admin"), session
            )
        self.assertEqual(result["acknowledged_by"], "forgejo-admin")
        self.assertEqual(repeat["acknowledged_by"], "forgejo-admin")
        self.assertEqual(record.acknowledged_at, saved_ack_at)
        self.assertEqual(record.ack_callback_status, "pending")
        self.assertEqual(record.escalation_status, "acknowledged")
        self.assertIsNone(record.escalation_next_attempt_at)
        session.commit.assert_awaited_once()

    async def test_external_delivery_retries_and_sends_only_safe_fields(self) -> None:
        database = MemoryDatabase()
        record = make_incident()
        database.records[record.event_id] = record
        posted: list[dict[str, object]] = []
        failures = iter([True, False])

        class FakeResponse:
            def __init__(self, fail: bool) -> None:
                self.fail = fail

            def raise_for_status(self) -> None:
                if self.fail:
                    raise httpx.ConnectError("relay unavailable")

        class FakeClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, *, content, headers):
                posted.append({"url": url, "content": content, "headers": headers})
                return FakeResponse(next(failures))

        with (
            patch("agent_manager.incidents.SessionLocal", side_effect=lambda: MemorySession(database)),
            patch("agent_manager.incidents.httpx.AsyncClient", FakeClient),
            patch.object(settings, "notification_webhook_url", "https://forge.example/alerts"),
            patch.object(settings, "notification_webhook_token", "relay-token"),
            patch.object(settings, "api_guard_alert_escalate_after_seconds", 900),
        ):
            self.assertFalse(await incidents.deliver_incident_notification(record.event_id))
            self.assertEqual(record.notification_status, "failed")
            self.assertEqual(record.notification_attempts, 1)
            record.notification_next_attempt_at = incidents.utcnow() - timedelta(seconds=1)
            self.assertEqual(await incidents.retry_incident_deliveries_once(), (1, 0, 0))

        self.assertEqual(record.notification_status, "delivered")
        self.assertEqual(record.notification_attempts, 2)
        self.assertEqual(record.escalation_status, "scheduled")
        self.assertEqual(
            (record.escalation_next_attempt_at - record.notification_delivered_at).total_seconds(),
            900,
        )
        self.assertEqual(len(posted), 2)
        payload = json.loads(posted[-1]["content"])
        self.assertEqual(
            set(payload),
            {"event", "severity", "event_id", "worker_id", "session_id", "key_alias", "reason", "occurred_at"},
        )
        self.assertEqual(payload["severity"], "critical")
        self.assertEqual(posted[-1]["headers"]["Authorization"], "Bearer relay-token")
        self.assertNotIn("prompt", repr(payload))
        self.assertNotIn("run_id", payload)

    async def test_no_ack_escalations_retry_independently_until_acknowledged(self) -> None:
        database = MemoryDatabase()
        record = make_incident()
        now = incidents.utcnow()
        record.notification_status = "delivered"
        record.notification_attempts = 4
        record.notification_delivered_at = now
        record.notification_next_attempt_at = None
        record.escalation_status = "scheduled"
        record.escalation_next_attempt_at = now - timedelta(seconds=1)
        database.records[record.event_id] = record
        posted: list[dict[str, object]] = []
        failures = iter([True, False, False])

        class FakeResponse:
            def __init__(self, fail: bool) -> None:
                self.fail = fail

            def raise_for_status(self) -> None:
                if self.fail:
                    raise httpx.ConnectError("relay unavailable")

        class FakeClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, *, content, headers):
                posted.append({"url": url, "content": content, "headers": headers})
                return FakeResponse(next(failures))

        with (
            patch("agent_manager.incidents.SessionLocal", side_effect=lambda: MemorySession(database)),
            patch("agent_manager.incidents.httpx.AsyncClient", FakeClient),
            patch.object(settings, "notification_webhook_url", "https://forge.example/alerts"),
            patch.object(settings, "notification_webhook_token", ""),
            patch.object(settings, "api_guard_alert_ack_secret", ""),
        ):
            self.assertFalse(await incidents.deliver_incident_escalation(record.event_id))
            self.assertEqual(record.escalation_status, "failed")
            self.assertEqual(record.escalation_level, 1)
            self.assertEqual(record.escalation_attempts, 1)
            self.assertEqual(record.notification_status, "delivered")
            self.assertEqual(record.notification_attempts, 4)

            record.escalation_next_attempt_at = incidents.utcnow() - timedelta(seconds=1)
            self.assertEqual(await incidents.retry_incident_deliveries_once(), (0, 0, 1))
            self.assertEqual(record.escalation_status, "scheduled")
            self.assertEqual(record.escalation_level, 1)
            self.assertEqual(record.escalation_attempts, 0)

            record.escalation_next_attempt_at = incidents.utcnow() - timedelta(seconds=1)
            self.assertEqual(await incidents.retry_incident_deliveries_once(), (0, 0, 1))
            self.assertEqual(record.escalation_status, "scheduled")
            self.assertEqual(record.escalation_level, 2)
            self.assertEqual(record.notification_attempts, 4)

            record.acknowledged_at = incidents.utcnow()
            record.escalation_status = "acknowledged"
            record.escalation_next_attempt_at = None
            self.assertEqual(await incidents.retry_incident_deliveries_once(), (0, 0, 0))

        self.assertEqual(len(posted), 3)
        payloads = [json.loads(item["content"]) for item in posted]
        self.assertEqual([payload["escalation_level"] for payload in payloads], [1, 1, 2])
        self.assertTrue(all(payload["event"] == "api_guard.exposure.escalated" for payload in payloads))
        expected_fields = {
            "event", "severity", "event_id", "worker_id", "session_id", "key_alias",
            "reason", "occurred_at", "escalation_level",
        }
        self.assertTrue(all(set(payload) == expected_fields for payload in payloads))
        self.assertTrue(all(payload["severity"] == "critical" for payload in payloads))
        self.assertTrue(all("prompt" not in repr(payload) for payload in payloads))

    async def test_signed_ack_callback_retries_with_distinct_secret(self) -> None:
        database = MemoryDatabase()
        record = make_incident(acknowledged=True)
        database.records[record.event_id] = record
        posted: list[dict[str, object]] = []
        failures = iter([True, False])

        class FakeResponse:
            def __init__(self, fail: bool) -> None:
                self.fail = fail

            def raise_for_status(self) -> None:
                if self.fail:
                    raise httpx.ConnectError("API Guard unavailable")

        class FakeClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, *, content, headers):
                posted.append({"url": url, "content": content, "headers": headers})
                return FakeResponse(next(failures))

        with (
            patch("agent_manager.incidents.SessionLocal", side_effect=lambda: MemorySession(database)),
            patch("agent_manager.incidents.httpx.AsyncClient", FakeClient),
            patch.object(settings, "notification_webhook_url", ""),
            patch.object(settings, "api_guard_alert_ack_url", "http://api-guard:8000/api-guard/api/v1/webhooks/alerts/ack"),
            patch.object(settings, "api_guard_alert_ack_secret", ACK_SECRET),
        ):
            self.assertFalse(await incidents.deliver_incident_ack_callback(record.event_id))
            self.assertEqual(record.ack_callback_status, "failed")
            self.assertEqual(record.ack_callback_attempts, 1)
            record.ack_callback_next_attempt_at = incidents.utcnow() - timedelta(seconds=1)
            self.assertEqual(await incidents.retry_incident_deliveries_once(), (0, 1, 0))

        self.assertEqual(record.ack_callback_status, "delivered")
        self.assertEqual(record.notification_status, "unconfigured")
        self.assertEqual(record.notification_attempts, 0)
        self.assertEqual(record.ack_callback_attempts, 2)
        body = posted[-1]["content"]
        callback = json.loads(body)
        self.assertEqual(set(callback), {"event_id", "acknowledged_by", "acknowledged_at"})
        expected = hmac.new(ACK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(posted[-1]["headers"]["X-Darkfactory-Signature"], f"sha256={expected}")
        self.assertEqual(posted[-1]["url"], "http://api-guard:8000/api-guard/api/v1/webhooks/alerts/ack")

    def test_alert_hmac_secrets_must_remain_distinct(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                AGENT_MANAGER_API_TOKEN="api-token",
                API_GUARD_EXPOSURE_WEBHOOK_SECRET="same-secret",
                API_GUARD_ALERT_WEBHOOK_SECRET="same-secret",
            )


if __name__ == "__main__":
    unittest.main()
