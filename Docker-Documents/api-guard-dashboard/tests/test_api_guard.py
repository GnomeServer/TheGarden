from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

import app as api_guard


class ApiGuardContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_values = {
            "state_db": api_guard.settings.state_db,
            "session_secret": api_guard.settings.session_secret,
            "admin_logins_csv": api_guard.settings.admin_logins_csv,
            "exposure_webhook_secret": api_guard.settings.exposure_webhook_secret,
            "keybroker_base_url": api_guard.settings.keybroker_base_url,
            "keybroker_dashboard_token": api_guard.settings.keybroker_dashboard_token,
            "alert_webhook_url": api_guard.settings.alert_webhook_url,
            "alert_webhook_secret": api_guard.settings.alert_webhook_secret,
            "annual_budget_usd": api_guard.settings.annual_budget_usd,
            "openai_admin_api_key": api_guard.settings.openai_admin_api_key,
            "openai_project_id": api_guard.settings.openai_project_id,
        }
        api_guard.settings.state_db = str(Path(self.temp.name) / "guard.sqlite3")
        api_guard.settings.session_secret = "test-session-secret-that-is-long-enough"
        api_guard.settings.admin_logins_csv = "ops-admin"
        api_guard.settings.exposure_webhook_secret = "test-webhook-secret"
        api_guard.settings.keybroker_base_url = "http://keybroker.test:8000"
        api_guard.settings.keybroker_dashboard_token = "dashboard-only-test-token"
        api_guard.settings.alert_webhook_url = ""
        api_guard.settings.alert_webhook_secret = ""
        api_guard.settings.annual_budget_usd = 25_000.0
        api_guard.settings.openai_admin_api_key = ""
        api_guard.settings.openai_project_id = ""
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_guard.app),
            base_url="http://api-guard.test",
        )
        self.csrf = "csrf-test-token"
        self.client.cookies.set(
            api_guard.SESSION_COOKIE,
            api_guard.encode_signed({"login": "ops-admin", "csrf": self.csrf, "exp": 4_102_444_800}),
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for name, value in self.old_values.items():
            setattr(api_guard.settings, name, value)
        self.temp.cleanup()

    def signed_headers(self, body: bytes) -> dict[str, str]:
        signature = hmac.new(
            api_guard.settings.exposure_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return {"Content-Type": "application/json", "X-Darkfactory-Signature": f"sha256={signature}"}

    async def test_keybroker_proxy_uses_dashboard_token_and_safe_errors(self) -> None:
        captured: dict[str, Any] = {}

        class FakeResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"revoked": True, "key_id": "safe-token-hash"}

        class FakeClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def request(self, method, url, **kwargs):
                captured.update({"method": method, "url": url, **kwargs})
                return FakeResponse()

        with patch("app.httpx.AsyncClient", FakeClient):
            result = await api_guard.keybroker_request(
                "POST",
                "/v1/admin/keys/revoke",
                body={"key_id": "safe-token-hash"},
            )
        self.assertEqual(result, {"revoked": True, "key_id": "safe-token-hash"})
        self.assertEqual(captured["url"], "http://keybroker.test:8000/v1/admin/keys/revoke")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer dashboard-only-test-token")
        self.assertEqual(captured["json"], {"key_id": "safe-token-hash"})
        self.assertNotIn("master", captured["headers"]["Authorization"].casefold())

        class UnavailableClient(FakeClient):
            async def request(self, *_args, **_kwargs):
                raise httpx.ConnectError("remote credential must not leak")

        with patch("app.httpx.AsyncClient", UnavailableClient):
            with self.assertRaises(api_guard.HTTPException) as error:
                await api_guard.keybroker_request("GET", "/v1/admin/keys")
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(error.exception.detail, "Keybroker is unavailable")
        self.assertNotIn("remote credential", error.exception.detail)

    async def test_legacy_exposure_database_migrates_worker_metadata_columns(self) -> None:
        with sqlite3.connect(api_guard.settings.state_db) as connection:
            connection.execute(
                "CREATE TABLE exposure_events ("
                "event_id TEXT PRIMARY KEY, key_id TEXT, key_alias TEXT NOT NULL, "
                "source TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL, "
                "received_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'flagged')"
            )
            connection.execute(
                "INSERT INTO exposure_events "
                "(event_id,key_alias,source,reason,occurred_at,received_at) "
                "VALUES ('legacy-event','legacy-worker','omp','configured_credential_echo',"
                "'2026-10-22T00:00:00+00:00','2026-10-22T00:00:01+00:00')"
            )
        connection.close()

        with api_guard.db_connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(exposure_events)")}
        self.assertIn("worker_id", columns)
        self.assertIn("session_id", columns)
        legacy = api_guard.event_rows()[0]
        self.assertEqual(legacy["event_id"], "legacy-event")
        self.assertIsNone(legacy["worker_id"])
        self.assertIsNone(legacy["session_id"])

    async def test_exposure_notification_contains_only_safe_metadata(self) -> None:
        api_guard.settings.alert_webhook_url = "https://notify.example/hook"
        api_guard.settings.alert_webhook_secret = "distinct-outbound-alert-secret"
        captured: list[dict[str, Any]] = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, *, content, headers):
                captured.append({"url": url, "content": content, "headers": headers})
                return FakeResponse()

        event = {
            "event_id": "worker-alert-1",
            "key_alias": "worker-session-key",
            "worker_id": "Raphael",
            "session_id": "session-abc123",
            "source": "keybroker",
            "reason": "configured_credential_echo",
            "occurred_at": "2026-10-22T00:00:00+00:00",
        }
        body = json.dumps(event).encode()
        with patch("app.httpx.AsyncClient", FakeClient):
            response = await self.client.post(
                "/api-guard/api/v1/webhooks/exposure",
                content=body,
                headers=self.signed_headers(body),
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["notification_queued"])
        self.assertEqual(captured, [])
        with patch("app.httpx.AsyncClient", FakeClient):
            self.assertEqual(await api_guard.deliver_pending_alerts(), 1)
        self.assertEqual(captured[0]["url"], "https://notify.example/hook")
        safe_payload = json.loads(captured[0]["content"])
        self.assertEqual(
            set(safe_payload),
            {"event_id", "worker_id", "session_id", "key_alias", "reason", "occurred_at"},
        )
        self.assertEqual(safe_payload["worker_id"], "Raphael")
        self.assertEqual(safe_payload["event_id"], "worker-alert-1")
        self.assertNotIn("prompt", repr(safe_payload))
        expected = hmac.new(
            api_guard.settings.alert_webhook_secret.encode(),
            captured[0]["content"],
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(captured[0]["headers"]["X-Darkfactory-Signature"], f"sha256={expected}")
        inbound_signature = hmac.new(
            api_guard.settings.exposure_webhook_secret.encode(),
            captured[0]["content"],
            hashlib.sha256,
        ).hexdigest()
        self.assertNotEqual(expected, inbound_signature)
        delivered = api_guard.event_rows()[0]
        self.assertEqual(delivered["notification_status"], "sent")
        self.assertEqual(delivered["notification_attempts"], 1)
        duplicate = await self.client.post(
            "/api-guard/api/v1/webhooks/exposure",
            content=body,
            headers=self.signed_headers(body),
        )
        self.assertEqual(duplicate.status_code, 202)
        self.assertFalse(duplicate.json()["notification_queued"])
        with patch("app.httpx.AsyncClient", FakeClient):
            self.assertEqual(await api_guard.deliver_pending_alerts(), 0)
        self.assertEqual(len(captured), 1)

    async def test_alert_webhook_needs_separate_outbound_secret(self) -> None:
        api_guard.settings.alert_webhook_url = "https://notify.example/hook"
        api_guard.settings.alert_webhook_secret = ""
        event = {
            "event_id": "unconfigured-alert",
            "key_alias": "worker-session-key",
            "worker_id": "Inkii",
            "session_id": "session-no-outbound-secret",
            "source": "keybroker",
            "reason": "configured_credential_echo",
        }
        body = json.dumps(event).encode()
        response = await self.client.post(
            "/api-guard/api/v1/webhooks/exposure",
            content=body,
            headers=self.signed_headers(body),
        )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["notification_queued"])
        self.assertEqual(api_guard.pending_alert_count(), 0)

    async def test_notification_outbox_retries_until_delivered(self) -> None:
        api_guard.settings.alert_webhook_url = "https://notify.example/hook"
        api_guard.settings.alert_webhook_secret = "distinct-outbound-alert-secret"
        event = {
            "event_id": "retry-event",
            "key_alias": "worker-session-key",
            "worker_id": "Donatello",
            "session_id": "session-retry",
            "source": "keybroker",
            "reason": "credential_pattern_in_output",
        }
        body = json.dumps(event).encode()
        response = await self.client.post(
            "/api-guard/api/v1/webhooks/exposure",
            content=body,
            headers=self.signed_headers(body),
        )
        self.assertEqual(response.status_code, 202)

        class FailedResponse:
            def raise_for_status(self) -> None:
                raise httpx.ConnectError("notification service unavailable")

        class FailedClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return FailedResponse()

        with patch("app.httpx.AsyncClient", FailedClient):
            self.assertEqual(await api_guard.deliver_pending_alerts(), 0)
        failed = api_guard.event_rows()[0]
        self.assertEqual(failed["notification_status"], "pending")
        self.assertEqual(failed["notification_attempts"], 1)
        self.assertEqual(api_guard.pending_alert_count(), 1)

        with api_guard.db_connect() as connection:
            connection.execute(
                "UPDATE alert_deliveries SET next_attempt_at=? WHERE event_id='retry-event'",
                ((api_guard.utcnow() - timedelta(seconds=1)).isoformat(),),
            )

        class SuccessfulResponse:
            def raise_for_status(self) -> None:
                return None

        class SuccessfulClient(FailedClient):
            async def post(self, *_args, **_kwargs):
                return SuccessfulResponse()

        with patch("app.httpx.AsyncClient", SuccessfulClient):
            self.assertEqual(await api_guard.deliver_pending_alerts(), 1)
        delivered = api_guard.event_rows()[0]
        self.assertEqual(delivered["notification_status"], "sent")
        self.assertEqual(delivered["notification_attempts"], 2)
        self.assertEqual(api_guard.pending_alert_count(), 0)

    async def test_openai_cost_report_uses_scoped_admin_billing_api(self) -> None:
        api_guard.settings.openai_admin_api_key = "organization-admin-read-key"
        api_guard.settings.openai_project_id = "proj-darkfactory"
        now = api_guard.utcnow()
        captured: list[dict[str, Any]] = []

        class FakeResponse:
            def __init__(self, payload: dict[str, Any]) -> None:
                self.payload = payload

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return self.payload

        class FakeClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, *, headers, params):
                captured.append({"url": url, "headers": headers, "params": params})
                if len(captured) == 1:
                    return FakeResponse({
                        "data": [{
                            "start_time": int((now - timedelta(days=60)).timestamp()),
                            "results": [{"amount": {"value": 3.25, "currency": "usd"}}],
                        }],
                        "has_more": True,
                        "next_page": "page-2",
                    })
                return FakeResponse({
                    "data": [{
                        "start_time": int((now - timedelta(days=5)).timestamp()),
                        "results": [{"amount": {"value": 1.25, "currency": "usd"}}],
                    }],
                    "has_more": False,
                    "next_page": None,
                })

        with patch("app.httpx.AsyncClient", FakeClient):
            result = await api_guard.openai_cost_report(
                now - timedelta(days=365),
                now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
                now,
            )

        self.assertTrue(result["available"])
        self.assertEqual(result["scope"], "project proj-darkfactory")
        self.assertEqual(result["rolling_365d_usd"], 4.5)
        self.assertEqual(result["month_to_date_usd"], 1.25)
        self.assertEqual(captured[0]["headers"]["Authorization"], "Bearer organization-admin-read-key")
        self.assertIn(("project_ids[]", "proj-darkfactory"), captured[0]["params"])
        self.assertIn(("page", "page-2"), captured[1]["params"])

    async def test_exposure_webhook_requires_signature_and_rejects_secret_values(self) -> None:
        event = {
            "event_id": "omp-event-1",
            "key_alias": "agent-manager-model",
            "worker_id": "Donatello",
            "session_id": "session-12345",
            "source": "omp",
            "reason": "configured_credential_echo",
            "occurred_at": "2026-10-22T00:00:00+00:00",
        }
        body = json.dumps(event).encode()
        denied = await self.client.post("/api-guard/api/v1/webhooks/exposure", content=body)
        self.assertEqual(denied.status_code, 403)

        accepted = await self.client.post(
            "/api-guard/api/v1/webhooks/exposure",
            content=body,
            headers=self.signed_headers(body),
        )
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json()["flagged"], True)

        unsafe = {**event, "event_id": "omp-event-secret", "key": "sk-never-store-this-secret"}
        unsafe_body = json.dumps(unsafe).encode()
        rejected = await self.client.post(
            "/api-guard/api/v1/webhooks/exposure",
            content=unsafe_body,
            headers=self.signed_headers(unsafe_body),
        )
        self.assertEqual(rejected.status_code, 422)
        credential_alias = {**event, "event_id": "omp-event-alias-secret", "key_alias": "sk-never-store-this-secret"}
        credential_alias_body = json.dumps(credential_alias).encode()
        credential_alias_rejected = await self.client.post(
            "/api-guard/api/v1/webhooks/exposure",
            content=credential_alias_body,
            headers=self.signed_headers(credential_alias_body),
        )
        self.assertEqual(credential_alias_rejected.status_code, 422)
        stored = api_guard.event_rows()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["worker_id"], "Donatello")
        self.assertEqual(stored[0]["session_id"], "session-12345")
        self.assertNotIn("sk-never-store-this-secret", repr(stored))
        self.assertEqual(stored[0]["status"], "flagged")
        oversized = await self.client.post(
            "/api-guard/api/v1/webhooks/exposure",
            content=b"x" * 16_385,
        )
        self.assertEqual(oversized.status_code, 413)

    async def test_dashboard_requires_admin_session_and_reports_sanitized_usage(self) -> None:
        self.client.cookies.clear()
        unauthorized = await self.client.get("/api-guard/api/v1/overview")
        self.assertEqual(unauthorized.status_code, 401)
        self.client.cookies.set(
            api_guard.SESSION_COOKIE,
            api_guard.encode_signed({"login": "ops-admin", "csrf": self.csrf, "exp": 4_102_444_800}),
        )
        worker_event = {
            "event_id": "unassociated-session-event",
            "key_id": None,
            "key_alias": "worker-one",
            "worker_id": "Donatello",
            "session_id": "unassociated-session-1",
            "source": "keybroker",
            "reason": "credential_pattern_in_output",
        }
        worker_body = json.dumps(worker_event).encode()
        accepted = await self.client.post(
            "/api-guard/api/v1/webhooks/exposure",
            content=worker_body,
            headers=self.signed_headers(worker_body),
        )
        self.assertEqual(accepted.status_code, 202)

        async def fake_broker(method: str, path: str, *, params=None, body=None):
            if path == "/v1/admin/keys":
                return [{
                    "key_id": "abcdef0123456789",
                    "key": "sk-masked-secret-must-not-leave-server",
                    "alias": "worker-one",
                    "models": ["luna"],
                    "spend_usd": 4.25,
                    "max_budget_usd": 100,
                    "budget_duration": "30d",
                    "created_at": "2026-10-01T00:00:00Z",
                }]
            if path == "/v1/admin/spend-logs":
                page = params["page"]
                now = api_guard.utcnow()
                if page == 1:
                    entries = [
                        {
                            "api_key": "abcdef0123456789",
                            "spend": 12.5,
                            "prompt_tokens": 100,
                            "completion_tokens": 23,
                            "total_tokens": 123,
                            "startTime": (now - timedelta(seconds=5)).isoformat(),
                            "messages": "sk-request-secret-must-not-reach-the-browser",
                        },
                        {
                            "api_key": "abcdef0123456789",
                            "spend": 5,
                            "prompt_tokens": 20,
                            "completion_tokens": 10,
                            "total_tokens": 30,
                            "startTime": (now - timedelta(days=60)).isoformat(),
                        },
                    ]
                else:
                    entries = [{
                        "api_key": "retired-key-hash",
                        "spend": 0,
                        "prompt_tokens": 40,
                        "completion_tokens": 10,
                        "total_tokens": 50,
                        "startTime": (now - timedelta(seconds=10)).isoformat(),
                    }]
                return {"data": entries, "total_pages": 2, "total_is_capped": False}
            raise AssertionError(f"Unexpected Keybroker endpoint: {path}")

        with patch.object(api_guard, "keybroker_request", new=AsyncMock(side_effect=fake_broker)):
            response = await self.client.get("/api-guard/api/v1/overview")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        incident = result["exposure_events"][0]
        self.assertIsNone(incident["key_id"])
        self.assertIsNone(incident["matched_key_id"])
        self.assertIsNone(incident["matched_alias"])
        self.assertEqual(incident["worker_id"], "Donatello")
        self.assertEqual(incident["session_id"], "unassociated-session-1")
        self.assertEqual(result["budget"]["annual_limit_usd"], 25_000)
        self.assertEqual(result["budget"]["annual_spend_usd"], 17.5)
        self.assertEqual(result["budget"]["monthly_spend_usd"], 12.5)
        self.assertEqual(result["budget"]["monthly_target_usd"], 2_083.33)
        self.assertEqual(result["keys"][0]["key_id"], "abcdef0123456789")
        self.assertEqual(result["budget"]["monthly_tokens"], 173)
        self.assertEqual(result["budget"]["annual_tokens"], 203)
        self.assertEqual(result["budget"]["zero_cost_requests_monthly"], 1)
        self.assertEqual(result["budget"]["zero_cost_tokens_monthly"], 50)
        self.assertEqual(result["keys"][0]["tokens_monthly"], 123)
        self.assertEqual(result["unmatched_usage_key_count"], 1)
        self.assertFalse(result["usage_truncated"])
        self.assertFalse(result["openai_costs"]["configured"])

        self.assertNotIn("sk-masked-secret-must-not-leave-server", response.text)
        self.assertNotIn("sk-request-secret-must-not-reach-the-browser", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_spend_log_page_cap_is_explicitly_reported(self) -> None:
        async def capped_logs(*_args, **_kwargs):
            return {"data": [], "total_pages": 11, "total_is_capped": True}

        with patch.object(api_guard, "keybroker_request", new=AsyncMock(side_effect=capped_logs)) as request:
            rows, truncated = await api_guard.fetch_spend_logs(
                api_guard.utcnow() - timedelta(days=365),
                api_guard.utcnow(),
            )
        self.assertEqual(rows, [])
        self.assertTrue(truncated)
        self.assertEqual(request.await_count, api_guard.MAX_SPEND_LOG_PAGES)

    async def test_new_virtual_key_has_budget_and_returns_secret_without_persisting(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_broker(method: str, path: str, *, params=None, body=None):
            calls.append((method, path, body))
            return {"key": "sk-one-time-new-key", "key_id": "new-token-hash"}

        with patch.object(api_guard, "keybroker_request", new=AsyncMock(side_effect=fake_broker)):
            response = await self.client.post(
                "/api-guard/api/v1/keys",
                headers={"X-CSRF-Token": self.csrf},
                json={"alias": "fable-worker-01", "models": ["luna"], "max_budget_usd": 75, "budget_duration": "30d"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["key"], "sk-one-time-new-key")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(calls[0][1], "/v1/admin/keys/generate")
        self.assertEqual(calls[0][2]["max_budget_usd"], 75)
        self.assertEqual(calls[0][2]["models"], ["luna"])
        self.assertEqual(calls[0][2]["duration"], f"{api_guard.settings.rotation_days * 2}d")
        self.assertNotIn("sk-one-time-new-key", repr(api_guard.event_rows()))

    async def test_rotation_preserves_scope_and_revoke_resolves_alias_only_alert(self) -> None:
        event = {
            "event_id": "omp-alias-event",
            "key_alias": "worker-one",
            "source": "omp",
            "reason": "configured_credential_echo",
        }
        event_body = json.dumps(event).encode()
        reported = await self.client.post(
            "/api-guard/api/v1/webhooks/exposure",
            content=event_body,
            headers=self.signed_headers(event_body),
        )
        self.assertEqual(reported.status_code, 202)

        key = {
            "key_id": "old-hashed-id",
            "alias": "worker-one",
            "models": ["luna"],
            "max_budget_usd": 75,
            "budget_duration": "30d",
            "team_id": "shared-team",
        }
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_broker(method: str, path: str, *, params=None, body=None):
            calls.append((method, path, body))
            if path == "/v1/admin/keys":
                return [key]
            if path == "/v1/admin/keys/generate":
                return {"key": "sk-replacement-one-time", "key_id": "new-hashed-id"}
            return {"revoked": True, "key_id": "old-hashed-id"}

        with patch.object(api_guard, "keybroker_request", new=AsyncMock(side_effect=fake_broker)):
            rotated = await self.client.post(
                "/api-guard/api/v1/keys/rotate",
                headers={"X-CSRF-Token": self.csrf},
                json={"key_id": "old-hashed-id"},
            )
            self.assertEqual(rotated.status_code, 200)
            generation = next(body for _method, path, body in calls if path == "/v1/admin/keys/generate")
            self.assertEqual(generation["models"], ["luna"])
            self.assertEqual(generation["max_budget_usd"], 75)
            self.assertEqual(generation["duration"], f"{api_guard.settings.rotation_days * 2}d")

            revoked = await self.client.post(
                "/api-guard/api/v1/keys/revoke",
                headers={"X-CSRF-Token": self.csrf},
                json={"key_id": "old-hashed-id"},
            )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(api_guard.event_rows()[0]["status"], "revoked")
        self.assertNotIn("sk-replacement-one-time", repr(api_guard.event_rows()))

    async def test_mutating_dashboard_request_requires_csrf(self) -> None:
        self.client.cookies.clear()
        self.client.cookies.set(
            api_guard.SESSION_COOKIE,
            api_guard.encode_signed({"login": "ops-admin", "csrf": self.csrf, "exp": 4_102_444_800}),
        )
        response = await self.client.post(
            "/api-guard/api/v1/keys",
            json={"alias": "test", "models": ["luna"], "max_budget_usd": 1, "budget_duration": "30d"},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
