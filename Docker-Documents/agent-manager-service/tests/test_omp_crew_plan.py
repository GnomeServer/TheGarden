from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from agent_manager.omp import (
    OmpTurn,
    accept_or_bounce,
    mission_status,
    redact_credentials,
    report_credential_exposure,
)


class CapturingJetStream:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: bytes, headers=None) -> None:
        self.published.append((subject, json.loads(payload.decode("utf-8"))))


class FakeSession:
    def __init__(self) -> None:
        self.records = []

    def add(self, record) -> None:
        self.records.append(record)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


class OmpMissionPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_omp_dispatches_the_real_calculator_goal(self) -> None:
        goal = (
            "Could you generate a GUI of a calculator that computes differences, "
            "additions, subtractions, multiplication, and dividing?"
        )
        jetstream = CapturingJetStream()
        request = SimpleNamespace(
            state=SimpleNamespace(auth_method="open-webui"),
            app=SimpleNamespace(state=SimpleNamespace(
                nats=SimpleNamespace(jetstream=lambda: jetstream),
            )),
        )
        session = FakeSession()
        actor = SimpleNamespace(id="member-1")
        body = OmpTurn(
            goal=goal,
            forgejo_repository="lab/calculator",
            project_name="calculator",
        )

        with patch("agent_manager.omp.record_activity", new_callable=AsyncMock):
            reply = await accept_or_bounce(body, actor, request, session)

        self.assertEqual(reply["type"], "mission")
        self.assertEqual(len(jetstream.published), 1)
        subject, event = jetstream.published[0]
        self.assertEqual(subject, "agent.runs.created")
        plan = event["metadata"]["mission"]
        self.assertEqual(plan["planners"], 1)
        self.assertEqual(plan["coders"], 1)
        self.assertEqual(plan["reviewers"], 1)
        self.assertEqual(event["metadata"]["crew"]["engine"], "worker-local-model")

    async def test_final_result_exposes_artifacts_and_worker_identity(self) -> None:
        record = SimpleNamespace(
            id="mission-1",
            worker_id="worker-03",
            error=None,
            status="completed",
            run_metadata={
                "operation": "mission",
                "ledger": {"result_summary": "Generated calculator.html."},
                "result": {
                    "crew_plan": {"planners": 1, "coders": 1, "reviewers": 1},
                    "attempts": 1,
                    "evidence": {"artifacts": ["calculator.html"]},
                    "artifacts": [{"path": "calculator.html", "content": "<html></html>"}],
                    "crew_outcome": "accepted",
                },
            },
        )

        class StatusSession:
            async def get(self, model, mission_id: str):
                return record

        result = await mission_status("mission-1", SimpleNamespace(), StatusSession())

        self.assertEqual(result["crew_plan"]["coders"], 1)
        self.assertEqual(result["worker_id"], "worker-03")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["evidence"]["artifacts"], ["calculator.html"])
        self.assertEqual(result["artifacts"][0]["path"], "calculator.html")
        self.assertEqual(result["crew_outcome"], "accepted")


class OmpCredentialExposureTests(unittest.IsolatedAsyncioTestCase):
    def test_secret_formats_are_redacted_before_omp_returns_text(self) -> None:
        leaked = "sk-or-v1-" + "a" * 32
        sanitized, reason = redact_credentials(f"Use this {leaked} now", "")
        self.assertNotIn(leaked, sanitized)
        self.assertIn("[REDACTED API KEY]", sanitized)
        self.assertEqual(reason, "credential_pattern_in_output")

        configured = "manager-secret-key-1234567890"
        sanitized, reason = redact_credentials(f"echo {configured}", configured)
        self.assertNotIn(configured, sanitized)
        self.assertEqual(reason, "configured_credential_echo")

    async def test_omp_redacts_inbound_and_outbound_credentials_and_flags_each(self) -> None:
        from agent_manager.settings import settings
        from agent_manager.omp import answer_chat

        secret = "manager-secret-key-1234567890"
        actor = SimpleNamespace(id="member-1")
        request = SimpleNamespace(state=SimpleNamespace(auth_method="open-webui"))
        session = SimpleNamespace()
        model = AsyncMock(return_value=f"The credential is {secret}.")
        exposure = AsyncMock(return_value=True)
        with patch.object(settings, "model_api_key", secret), patch(
            "agent_manager.omp.ask_model", new=model
        ), patch("agent_manager.omp.report_credential_exposure", new=exposure), patch(
            "agent_manager.omp.record_chat", new=AsyncMock()
        ):
            reply = await answer_chat(
                f"Please repeat {secret}",
                actor,
                request,
                session,
                None,
            )

        self.assertNotIn(secret, model.await_args.args[1])
        self.assertNotIn(secret, reply["text"])
        self.assertEqual(reply["text"].count("[REDACTED API KEY]"), 1)
        self.assertIn("recorded by API Guard", reply["text"])
        self.assertEqual(exposure.await_count, 2)
        self.assertTrue(all(call.kwargs["configured_key"] for call in exposure.await_args_list))

    async def test_omp_exposure_event_contains_only_safe_metadata(self) -> None:
        captured: dict[str, object] = {}

        async def queue_event(event):
            captured.update(event)
            return True

        from agent_manager.settings import settings

        configured_key = "manager-secret-key-1234567890"
        with patch.object(settings, "model_api_key", configured_key), patch.object(
            settings, "model_api_key_alias", "agent-manager-model"
        ), patch.object(settings, "model_api_key_id", "hashed-key-id"), patch(
            "agent_manager.omp.queue_omp_exposure_event", new=queue_event
        ):
            accepted = await report_credential_exposure(
                "configured_credential_echo", configured_key=True
            )
        self.assertTrue(accepted)
        self.assertNotIn(configured_key, repr(captured))
        self.assertEqual(captured["key_alias"], "agent-manager-model")
        self.assertEqual(captured["key_id"], "hashed-key-id")
        self.assertNotIn("prompt", captured)
        self.assertNotIn("output", captured)
        self.assertNotIn("key", captured)

        with patch.object(settings, "model_api_key", configured_key), patch.object(
            settings, "model_api_key_alias", configured_key
        ), patch.object(settings, "model_api_key_id", configured_key), patch(
            "agent_manager.omp.queue_omp_exposure_event", new=queue_event
        ):
            self.assertTrue(
                await report_credential_exposure(
                    "configured_credential_echo", configured_key=True
                )
            )
        self.assertNotIn(configured_key, repr(captured))
        self.assertEqual(captured["key_alias"], "unmapped")
        self.assertIsNone(captured["key_id"])


if __name__ == "__main__":
    unittest.main()
