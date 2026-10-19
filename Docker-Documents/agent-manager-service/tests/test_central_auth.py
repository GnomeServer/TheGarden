from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from agent_manager import auth
from agent_manager.dashboard import ensure_task_permission, upsert_forgejo_user, validate_task_links
from agent_manager.settings import Settings
from agent_manager.tracking import vector_by_node


class CredentialBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_admin_token_cannot_double_as_webui_token(self):
        secret = "test-only-secret-that-must-not-be-logged"
        with self.assertRaises(ValidationError) as caught:
            Settings(AGENT_MANAGER_API_TOKEN=secret, AGENT_MANAGER_OPENWEBUI_TOKEN=secret)
        self.assertNotIn(secret, str(caught.exception))

    async def test_webui_key_is_not_general_dashboard_authentication(self):
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="webui-only")
        session = SimpleNamespace()
        with patch.object(auth.settings, "api_token", "administrator-only"):
            with self.assertRaises(HTTPException) as caught:
                await auth.api_token_user(credentials, session)
        self.assertEqual(caught.exception.status_code, 401)

    async def test_browser_dashboard_requires_an_allowlisted_session_and_csrf(self):
        with self.assertRaises(HTTPException) as caught:
            await auth.session_user(
                SimpleNamespace(cookies={}, state=SimpleNamespace()),
                SimpleNamespace(get=AsyncMock()),
            )
        self.assertEqual(caught.exception.status_code, 401)

        token, csrf = auth.create_session("member-id")
        user = SimpleNamespace(
            id="member-id", forgejo_login="raphael", role="member", active=True,
        )
        request = SimpleNamespace(
            cookies={auth.SESSION_COOKIE: token},
            state=SimpleNamespace(),
            headers={"X-CSRF-Token": "wrong"},
        )
        session = SimpleNamespace(get=AsyncMock(return_value=user))
        authenticated = await auth.session_user(request, session)
        self.assertIs(authenticated, user)
        with self.assertRaises(HTTPException) as caught:
            await auth.require_session_csrf(request, user)
        self.assertEqual(caught.exception.status_code, 403)
        request.headers["X-CSRF-Token"] = csrf
        self.assertIs(await auth.require_session_csrf(request, user), user)

    async def test_forgejo_sign_in_starts_as_admin(self):
        added = []
        session = SimpleNamespace(scalar=AsyncMock(side_effect=[None, None]), add=added.append)
        user = await upsert_forgejo_user(session, {"id": 9, "login": "michelangelo"})
        self.assertEqual(user.role, "admin")
        self.assertIs(user.active, True)
        self.assertEqual(added, [user])

    async def test_granted_account_keeps_a_session_without_the_env_name_list(self):
        token, _csrf = auth.create_session("outsider-id")
        user = SimpleNamespace(
            id="outsider-id", forgejo_login="gnomeserver", role="member", active=True,
        )
        request = SimpleNamespace(cookies={auth.SESSION_COOKIE: token}, state=SimpleNamespace())
        session = SimpleNamespace(get=AsyncMock(return_value=user))
        self.assertIs(await auth.session_user(request, session), user)
        inactive = SimpleNamespace(
            id="outsider-id", forgejo_login="gnomeserver", role="pending", active=False,
        )
        session.get = AsyncMock(return_value=inactive)
        with self.assertRaises(HTTPException) as caught:
            await auth.session_user(request, session)
        self.assertEqual(caught.exception.status_code, 401)

    async def test_viewer_cannot_dispatch_execution(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_method="bearer"))
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
            await upsert_forgejo_user(session, {"id":2,"login":"raphael"})
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(existing.forgejo_user_id, 1)
        self.assertEqual(existing.role, "admin")

    async def test_first_forgejo_login_creates_an_active_account(self):
        added = []
        session = SimpleNamespace(scalar=AsyncMock(side_effect=[None, None]), add=added.append)
        with patch("agent_manager.dashboard.settings.bootstrap_admins", "bgurrol4"):
            user = await upsert_forgejo_user(session, {"id": 7, "login": "bgurrol4"})
        self.assertIs(user.active, True)
        self.assertEqual(user.role, "admin")
        self.assertEqual(added, [user])

    def test_any_allowlisted_member_can_edit_and_reassign_a_shared_task(self):
        shared_task = SimpleNamespace(assignee_user_id="another-member", created_by="original-author")
        for member_id in ("raphael", "inkii", "bgurrol4", "naruto"):
            with self.subTest(member=member_id):
                ensure_task_permission(SimpleNamespace(role="member", id=member_id), shared_task)

    async def test_service_account_cannot_be_assigned_human_tasks(self):
        for role, active, allowed in (("service", True, False), ("member", False, False), ("member", True, True)):
            with self.subTest(role=role, active=active):
                session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
                    role=role, active=active, forgejo_login="raphael",
                )))
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
