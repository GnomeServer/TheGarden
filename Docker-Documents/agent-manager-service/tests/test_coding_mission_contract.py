from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from nats.errors import NoRespondersError

from agent_manager.main import RunCreate, create_run
from agent_manager.missions import (
    CONTRACT_VERSION,
    coding_mission_errors,
    is_coding_mission,
    normalized_coding_mission,
)
from agent_manager.models import ActivityRecord, RunRecord


def complete_mission(**overrides: object) -> dict:
    mission = {
        "allowed_actions": ["git_write", "run_approved_checks"],
        "forbidden_actions": [
            "merge",
            "push_protected",
            "production_access",
            "secret_access",
        ],
        "acceptance_criteria": ["approved checks pass on the feature branch"],
        "evidence": ["commands", "exit_codes", "test_report", "branch_ref"],
        "max_runtime_seconds": 1800,
        "max_attempts": 2,
        "budget": {
            "max_cost_microunits": 0,
            "max_input_tokens": 50000,
            "max_output_tokens": 8000,
        },
        "approval": {"required": True, "gates": ["merge"]},
    }
    mission.update(overrides)
    return {
        "operation": "coding-mission",
        "contract_version": CONTRACT_VERSION,
        "mission": mission,
    }


class CodingMissionContractTests(unittest.TestCase):
    def test_script_runs_are_not_coding_missions(self) -> None:
        self.assertFalse(is_coding_mission({}))
        self.assertFalse(
            is_coding_mission({"operation": "create-python-script", "worker_role": "coder"})
        )

    def test_operation_or_contract_marks_a_coding_mission(self) -> None:
        self.assertTrue(is_coding_mission({"operation": "coding-mission"}))
        self.assertTrue(is_coding_mission({"operation": "repository-coding"}))
        self.assertTrue(is_coding_mission({"contract_version": CONTRACT_VERSION}))
        self.assertTrue(is_coding_mission({"mission": {}}))

    def test_complete_contract_has_no_errors(self) -> None:
        metadata = complete_mission()
        errors = coding_mission_errors(
            goal="Add a failing test then make it pass",
            repository="lab/disposable",
            base_ref="main",
            metadata=metadata,
        )
        self.assertEqual(errors, [])
        attached = normalized_coding_mission(metadata=metadata)
        self.assertEqual(attached["contract_version"], CONTRACT_VERSION)
        self.assertEqual(attached["operation"], "coding-mission")
        self.assertEqual(attached["mission"]["max_attempts"], 2)
        self.assertEqual(attached["mission"]["approval"]["gates"], ["merge"])

    def test_missing_required_fields_are_named(self) -> None:
        errors = coding_mission_errors(
            goal="Add a failing test then make it pass",
            repository="lab/disposable",
            base_ref="main",
            metadata={"operation": "coding-mission"},
        )
        fields = [item["field"] for item in errors]
        self.assertEqual(
            fields,
            [
                "allowed_actions",
                "forbidden_actions",
                "acceptance_criteria",
                "evidence",
                "max_runtime_seconds",
                "max_attempts",
                "budget",
                "approval",
            ],
        )

    def test_invalid_repository_and_budget_are_rejected(self) -> None:
        metadata = complete_mission(budget={"max_input_tokens": -1})
        errors = coding_mission_errors(
            goal="Add a failing test then make it pass",
            repository="not-a-repo",
            base_ref="main",
            metadata=metadata,
        )
        self.assertIn({"field": "repository", "error": "must be owner/project"}, errors)
        self.assertIn(
            {"field": "budget.max_input_tokens", "error": "must be an integer >= 0"},
            errors,
        )

    def test_required_approval_needs_a_gate_or_role(self) -> None:
        metadata = complete_mission(approval={"required": True})
        errors = coding_mission_errors(
            goal="Add a failing test then make it pass",
            repository="lab/disposable",
            base_ref="main",
            metadata=metadata,
        )
        self.assertEqual(
            errors,
            [
                {
                    "field": "approval",
                    "error": "required approval needs approver_role or gates",
                }
            ],
        )

    def test_wrong_contract_version_is_rejected(self) -> None:
        metadata = complete_mission()
        metadata["contract_version"] = "coder-mission-0"
        errors = coding_mission_errors(
            goal="Add a failing test then make it pass",
            repository="lab/disposable",
            base_ref="main",
            metadata=metadata,
        )
        self.assertIn(
            {"field": "contract_version", "error": f"must be {CONTRACT_VERSION}"},
            errors,
        )


class CodingMissionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_coding_mission_never_reaches_worker_or_ledger(self) -> None:
        connection = SimpleNamespace(request=AsyncMock())
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(nats=connection)))
        session = SimpleNamespace(add=Mock(), flush=AsyncMock(), commit=AsyncMock())
        body = RunCreate(
            goal="Add a failing test then make it pass",
            forgejo_repository="lab/disposable",
            metadata={"operation": "coding-mission"},
        )
        with self.assertRaises(HTTPException) as caught:
            await create_run(body, request, SimpleNamespace(id="human"), session)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["code"], "INCOMPLETE_CODING_MISSION")
        self.assertEqual(caught.exception.detail["contract_version"], CONTRACT_VERSION)
        fields = [item["field"] for item in caught.exception.detail["errors"]]
        self.assertIn("allowed_actions", fields)
        self.assertIn("budget", fields)
        connection.request.assert_not_awaited()
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_complete_coding_mission_is_not_rejected_as_incomplete(self) -> None:
        connection = SimpleNamespace(request=AsyncMock(side_effect=NoRespondersError))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(nats=connection)))
        session = SimpleNamespace(add=Mock(), flush=AsyncMock(), commit=AsyncMock())
        body = RunCreate(
            goal="Add a failing test then make it pass",
            forgejo_repository="lab/disposable",
            base_ref="main",
            metadata=complete_mission(),
        )
        with patch("agent_manager.main.live_broker.publish", new_callable=AsyncMock):
            with self.assertRaises(HTTPException) as caught:
                await create_run(body, request, SimpleNamespace(id="human"), session)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "NO_WORKER_AVAILABLE")
        connection.request.assert_awaited()
        event = session.add.call_args.args[0]
        self.assertIsInstance(event, ActivityRecord)
        self.assertEqual(event.event_type, "run.rejected")
        self.assertNotIsInstance(event, RunRecord)


if __name__ == "__main__":
    unittest.main()
