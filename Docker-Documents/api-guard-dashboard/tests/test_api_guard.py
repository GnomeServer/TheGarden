from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
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
            "litellm_master_key": api_guard.settings.litellm_master_key,
            "annual_budget_usd": api_guard.settings.annual_budget_usd,
        }
        api_guard.settings.state_db = str(Path(self.temp.name) / "guard.sqlite3")
        api_guard.settings.session_secret = "test-session-secret-that-is-long-enough"
        api_guard.settings.admin_logins_csv = "ops-admin"
        api_guard.settings.exposure_webhook_secret = "test-webhook-secret"
        api_guard.settings.litellm_master_key = "server-only-master-key"
        api_guard.settings.annual_budget_usd = 25_000.0
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

    async def test_exposure_webhook_requires_signature_and_rejects_secret_values(self) -> None:
        event = {
            "event_id": "omp-event-1",
            "key_alias": "agent-manager-model",
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
        stored = api_guard.event_rows()
        self.assertEqual(len(stored), 1)
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

        async def fake_litellm(method: str, path: str, *, params=None, body=None):
            if path == "/key/list":
                return {"keys": [{
                    "token_id": "abcdef0123456789",
                    "key": "sk-masked-secret-must-not-leave-server",
                    "key_alias": "worker-one",
                    "models": ["luna"],
                    "spend": 4.25,
                    "max_budget": 100,
                    "budget_duration": "30d",
                    "created_at": "2026-10-01T00:00:00Z",
                }]}
            if path == "/spend/logs/v2":
                page = params["page"]
                entry = {
                    "api_key": "abcdef0123456789",
                    "spend": 12.5 if page == 1 else 5,
                    "prompt_tokens": 100 if page == 1 else 20,
                    "completion_tokens": 23 if page == 1 else 10,
                    "total_tokens": 123 if page == 1 else 30,
                    "startTime": (
                        (api_guard.utcnow() - timedelta(seconds=5)).isoformat()
                        if page == 1
                        else (api_guard.utcnow() - timedelta(days=60)).isoformat()
                    ),
                    "messages": "sk-request-secret-must-not-reach-the-browser",
                }
                return {"data": [entry], "total_pages": 2, "total_is_capped": False}
            raise AssertionError(f"Unexpected LiteLLM endpoint: {path}")

        with patch.object(api_guard, "litellm_request", new=AsyncMock(side_effect=fake_litellm)):
            response = await self.client.get("/api-guard/api/v1/overview")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["budget"]["annual_limit_usd"], 25_000)
        self.assertEqual(result["budget"]["annual_spend_usd"], 17.5)
        self.assertEqual(result["budget"]["monthly_spend_usd"], 12.5)
        self.assertEqual(result["budget"]["monthly_target_usd"], 2_083.33)
        self.assertEqual(result["keys"][0]["key_id"], "abcdef0123456789")
        self.assertEqual(result["budget"]["monthly_tokens"], 123)
        self.assertEqual(result["budget"]["annual_tokens"], 153)
        self.assertEqual(result["keys"][0]["tokens_monthly"], 123)
        self.assertFalse(result["usage_truncated"])

        self.assertNotIn("sk-masked-secret-must-not-leave-server", response.text)
        self.assertNotIn("sk-request-secret-must-not-reach-the-browser", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_spend_log_page_cap_is_explicitly_reported(self) -> None:
        async def capped_logs(*_args, **_kwargs):
            return {"data": [], "total_pages": 11, "total_is_capped": True}

        with patch.object(api_guard, "litellm_request", new=AsyncMock(side_effect=capped_logs)) as request:
            rows, truncated = await api_guard.fetch_spend_logs(
                api_guard.utcnow() - timedelta(days=365),
                api_guard.utcnow(),
            )
        self.assertEqual(rows, [])
        self.assertTrue(truncated)
        self.assertEqual(request.await_count, api_guard.MAX_SPEND_LOG_PAGES)

    async def test_new_virtual_key_has_budget_and_returns_secret_without_persisting(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_litellm(method: str, path: str, *, params=None, body=None):
            calls.append((method, path, body))
            return {"key": "sk-one-time-new-key"}

        with patch.object(api_guard, "litellm_request", new=AsyncMock(side_effect=fake_litellm)):
            response = await self.client.post(
                "/api-guard/api/v1/keys",
                headers={"X-CSRF-Token": self.csrf},
                json={"alias": "fable-worker-01", "models": ["luna"], "max_budget_usd": 75, "budget_duration": "30d"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["key"], "sk-one-time-new-key")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(calls[0][1], "/key/generate")
        self.assertEqual(calls[0][2]["max_budget"], 75)
        self.assertEqual(calls[0][2]["models"], ["luna"])
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
            "token_id": "old-hashed-id",
            "key_alias": "worker-one",
            "models": ["luna"],
            "max_budget": 75,
            "budget_duration": "30d",
            "team_id": "shared-team",
        }
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_litellm(method: str, path: str, *, params=None, body=None):
            calls.append((method, path, body))
            if path == "/key/list":
                return {"keys": [key]}
            if path == "/key/generate":
                return {"key": "sk-replacement-one-time"}
            return {"deleted": True}

        with patch.object(api_guard, "litellm_request", new=AsyncMock(side_effect=fake_litellm)):
            rotated = await self.client.post(
                "/api-guard/api/v1/keys/rotate",
                headers={"X-CSRF-Token": self.csrf},
                json={"key_id": "old-hashed-id"},
            )
            self.assertEqual(rotated.status_code, 200)
            generation = next(body for _method, path, body in calls if path == "/key/generate")
            self.assertEqual(generation["models"], ["luna"])
            self.assertEqual(generation["max_budget"], 75)
            self.assertEqual(generation["team_id"], "shared-team")

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
