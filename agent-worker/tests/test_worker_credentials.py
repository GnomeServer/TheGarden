from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
SPEC = importlib.util.spec_from_file_location(
    "worker_credentials_under_test", Path(__file__).parents[1] / "worker.py"
)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


SESSION = {
    "session_id": "session-42",
    "key": "opaque-session-key",
    "key_id": "key-42",
    "key_alias": "worker-session",
    "expires_at": "2030-01-01T00:00:00Z",
    "model_base_url": "https://models.example/v1",
    "model": "luna",
}


class CapturingJetStream:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.events.append((subject, json.loads(data.decode("utf-8"))))


class WorkerCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def run_event(
        self,
        goal: str = "Build a small utility",
        *,
        session_request=None,
        revoke_request=None,
    ) -> tuple[dict, CapturingJetStream]:
        session_request = session_request or AsyncMock(return_value=SESSION)
        revoke_request = revoke_request or AsyncMock(return_value=True)
        event = {
            "run_id": "run-credential-test",
            "goal": goal,
            "forgejo_repository": "test/repository",
            "metadata": {
                "operation": "mission",
                "worker_role": "coder",
                "mission": {"coders": 1, "reviewers": 1},
            },
        }
        message = SimpleNamespace(
            data=json.dumps(event).encode("utf-8"),
            ack=AsyncMock(),
        )
        js = CapturingJetStream()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(worker, "WORKSPACE", Path(directory)),
            patch.object(worker, "request_keybroker_session", session_request),
            patch.object(worker, "revoke_keybroker_session", revoke_request),
            patch("builtins.print"),
        ):
            await worker.process_message(js, message)
        message.ack.assert_awaited_once()
        completion = next(payload for subject, payload in js.events if subject == "agent.runs.completed")
        return completion, js

    async def test_keybroker_session_is_minted_and_revoked_for_completed_run(self) -> None:
        model_requests = []

        async def model_chat(system: str, user: str, max_tokens: int, **kwargs: str) -> str:
            model_requests.append((system, user, max_tokens, kwargs))
            return "safe model output"

        async def execute(event, metadata, *, model_call, workspace):
            await model_call("system prompt", "clean user prompt", 123)
            return {"status": "completed", "summary": "safe result"}

        session_request = AsyncMock(return_value=SESSION)
        revoke_request = AsyncMock(return_value=True)
        with (
            patch.object(worker, "model_chat", model_chat),
            patch.object(worker, "execute_mission", execute),
        ):
            completion, _ = await self.run_event(
                session_request=session_request,
                revoke_request=revoke_request,
            )

        session_request.assert_awaited_once_with("run-credential-test", "mission")
        revoke_request.assert_awaited_once_with("session-42")
        self.assertEqual(completion["status"], "completed")
        self.assertEqual(len(model_requests), 1)
        self.assertEqual(model_requests[0][3]["api_key"], SESSION["key"])
        self.assertEqual(model_requests[0][3]["model"], SESSION["model"])
        self.assertEqual(model_requests[0][3]["model_base_url"], SESSION["model_base_url"])

    async def test_keybroker_session_is_revoked_after_model_failure(self) -> None:
        session_request = AsyncMock(return_value=SESSION)
        revoke_request = AsyncMock(return_value=True)
        model_chat = AsyncMock(side_effect=RuntimeError("model unavailable"))

        async def fail_mission(event, metadata, *, model_call, workspace):
            await model_call("system", "safe prompt", 100)

        with (
            patch.object(worker, "model_chat", model_chat),
            patch.object(worker, "execute_mission", fail_mission),
        ):
            completion, _ = await self.run_event(
                session_request=session_request,
                revoke_request=revoke_request,
            )

        session_request.assert_awaited_once_with("run-credential-test", "mission")
        revoke_request.assert_awaited_once_with("session-42")
        model_chat.assert_awaited_once()
        self.assertEqual(completion["status"], "failed")
        self.assertIn("model unavailable", completion["error"])

    async def test_keybroker_conflict_fails_closed_before_model_call(self) -> None:
        session_request = AsyncMock(
            side_effect=RuntimeError("Keybroker returned HTTP 409")
        )
        revoke_request = AsyncMock()
        model_chat = AsyncMock(return_value="must not be called")
        execute = AsyncMock()

        with (
            patch.object(worker, "model_chat", model_chat),
            patch.object(worker, "execute_mission", execute),
        ):
            completion, _ = await self.run_event(
                session_request=session_request,
                revoke_request=revoke_request,
            )

        session_request.assert_awaited_once_with("run-credential-test", "mission")
        revoke_request.assert_not_awaited()
        execute.assert_not_awaited()
        model_chat.assert_not_awaited()
        self.assertEqual(completion["status"], "failed")
        self.assertIn("HTTP 409", completion["error"])

    async def test_keybroker_http_409_is_reported_without_response_body(self) -> None:
        conflict = worker.urllib.error.HTTPError(
            "https://keybroker/v1/sessions", 409, "Conflict", {}, None
        )
        with patch.object(
            worker.urllib.request, "urlopen", side_effect=conflict
        ) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "Keybroker returned HTTP 409"):
                await worker.request_keybroker_session("run-conflict", "mission")

        urlopen.assert_called_once()

    async def test_prompt_credential_pattern_is_blocked_before_model_forwarding(self) -> None:
        credential = "sk-proj-" + "A" * 28
        model_chat = AsyncMock(return_value="must not be called")
        exposure = AsyncMock(side_effect=RuntimeError("Keybroker exposure endpoint unavailable"))

        async def execute(event, metadata, *, model_call, workspace):
            await model_call("system", f"prompt includes {credential}", 100)
            return {"status": "completed"}

        with (
            patch.object(worker, "model_chat", model_chat),
            patch.object(worker, "report_key_exposure", exposure),
            patch.object(worker, "execute_mission", execute),
        ):
            completion, js = await self.run_event()

        model_chat.assert_not_awaited()
        exposure.assert_awaited_once_with("session-42", session_key_match=False)
        self.assertIn("Potential credential exposure detected", completion["security_notice"])
        self.assertIn("alert delivery could not be confirmed", completion["security_notice"])
        self.assertNotIn(credential, json.dumps(completion))
        self.assertNotIn(credential, json.dumps(js.events))

    async def test_exact_session_key_echo_stops_queued_calls_and_returns_safe_alert(self) -> None:
        model_chat = AsyncMock(return_value=f"model echoed {SESSION['key']}")
        exposure = AsyncMock(return_value=(True, True))

        async def execute(event, metadata, *, model_call, workspace):
            await asyncio.gather(
                model_call("system", "first call", 100),
                model_call("system", "second call", 100),
            )
            return {"status": "completed", "summary": "must not be returned"}

        with (
            patch.object(worker, "model_chat", model_chat),
            patch.object(worker, "report_key_exposure", exposure),
            patch.object(worker, "execute_mission", execute),
        ):
            completion, js = await self.run_event()

        model_chat.assert_awaited_once()
        exposure.assert_awaited_once_with("session-42", session_key_match=True)
        self.assertEqual(completion["status"], "failed")
        self.assertIn("session key was revoked", completion["security_notice"])
        self.assertNotIn(SESSION["key"], json.dumps(completion))
        self.assertNotIn(SESSION["key"], json.dumps(js.events))

    async def test_exposure_webhook_payload_contains_no_secret_or_prompt(self) -> None:
        captured = []

        def keybroker_request(method: str, path: str, payload: dict) -> dict:
            captured.append((method, path, payload))
            return {"stop_run": True, "revoked": True}

        with patch.object(worker, "keybroker_request", keybroker_request):
            response = await worker.report_key_exposure(
                "session-42", session_key_match=True
            )
        self.assertEqual(response, (True, True))

        self.assertEqual(len(captured), 1)
        method, path, payload = captured[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/v1/sessions/session-42/exposure")
        self.assertEqual(set(payload), {"event_id", "reason", "session_key_match"})
        self.assertEqual(payload["reason"], "session_key_echo_detected")
        self.assertTrue(payload["session_key_match"])
        self.assertNotIn(SESSION["key"], json.dumps(payload))

    async def test_redactor_recognizes_supported_provider_key_shapes(self) -> None:
        text = " ".join((
            "sk-" + "o" * 24,
            "sk-or-v1-" + "a" * 32,
            "sk-ant-" + "b" * 24,
            "AIza" + "C" * 35,
        ))
        cleaned, found, session_match = worker.redact_credentials(text)

        self.assertTrue(found)
        self.assertFalse(session_match)
        self.assertNotIn("sk-" + "o" * 24, cleaned)
        self.assertNotIn("sk-or-v1-" + "a" * 32, cleaned)
        self.assertNotIn("sk-ant-" + "b" * 24, cleaned)
        self.assertNotIn("AIza" + "C" * 35, cleaned)
        self.assertEqual(cleaned.count(worker.REDACTED_CREDENTIAL), 4)


if __name__ == "__main__":
    unittest.main()
