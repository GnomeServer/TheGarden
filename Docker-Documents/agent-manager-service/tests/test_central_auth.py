from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from agent_manager import auth
from agent_manager.dashboard import upsert_forgejo_user, validate_task_links
from agent_manager.settings import Settings
from agent_manager.tracking import vector_by_node


class CredentialBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_admin_token_cannot_double_as_webui_token(self):
        secret = "test-only-secret-that-must-not-be-logged"
        with self.assertRaises(ValidationError) as caught:
            Settings(AGENT_MANAGER_API_TOKEN=secret, AGENT_MANAGER_OPENWEBUI_TOKEN=secret)
        self.assertNotIn(secret, str(caught.exception))

    async def test_webui_key_is_not_general_dashboard_authentication(self):
        request = SimpleNamespace(cookies={}, state=SimpleNamespace())
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="webui-only")
        session = SimpleNamespace(get=AsyncMock())
        with patch.object(auth.settings, "api_token", "administrator-only"), patch.object(auth.settings, "openwebui_token", "webui-only"):
            with self.assertRaises(HTTPException) as caught:
                await auth.current_user(request, credentials, session)
        self.assertEqual(caught.exception.status_code, 401)
        session.get.assert_not_awaited()

    async def test_viewer_cannot_dispatch_execution(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_method="session", csrf="test-csrf"), headers={"X-CSRF-Token":"test-csrf"})
        with self.assertRaises(HTTPException) as caught:
            await auth.require_run_submission(request, SimpleNamespace(role="viewer"))
        self.assertEqual(caught.exception.status_code, 403)

    async def test_forgejo_cannot_claim_bootstrap_or_service_identity(self):
        for login in ("api-admin", "service:open-webui"):
            with self.subTest(login=login):
                session = SimpleNamespace(scalar=AsyncMock())
                with self.assertRaises(HTTPException) as caught:
                    await upsert_forgejo_user(session, {"id":55,"login":login})
                self.assertEqual(caught.exception.status_code, 409)
                session.scalar.assert_not_awaited()

    async def test_recycled_forgejo_login_cannot_take_over_existing_user(self):
        existing = SimpleNamespace(id="original-owner", role="admin", forgejo_user_id=1)
        session = SimpleNamespace(scalar=AsyncMock(side_effect=[None, existing]))
        with self.assertRaises(HTTPException) as caught:
            await upsert_forgejo_user(session, {"id":2,"login":"old-admin-name"})
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(existing.forgejo_user_id, 1)
        self.assertEqual(existing.role, "admin")

    async def test_service_account_cannot_be_assigned_human_tasks(self):
        for role, active, allowed in (("service", True, False), ("member", False, False), ("member", True, True)):
            with self.subTest(role=role, active=active):
                session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(role=role, active=active)))
                if allowed:
                    await validate_task_links(session, {"assignee_user_id": "known-user"})
                else:
                    with self.assertRaises(HTTPException) as caught:
                        await validate_task_links(session, {"assignee_user_id": "known-user"})
                    self.assertEqual(caught.exception.status_code, 422)


    def test_missing_metrics_do_not_become_nonfinite_json_or_zero(self):
        vector = [
            {"metric":{"host":"offline"},"value":[0,"NaN"]},
            {"metric":{"host":"empty-disk"},"value":[0,"+Inf"]},
            {"metric":{"host":"healthy"},"value":[0,"12.5"]},
        ]
        self.assertEqual(vector_by_node(vector), {"healthy":12.5})
