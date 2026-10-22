"""Versioned repository coding mission contract (CODER-01).

Incomplete coding missions must not be queued. Script/chat runs without
coding-mission markers keep the existing /v1/runs admission path.
"""

from __future__ import annotations

import re
from typing import Any

CONTRACT_VERSION = "coder-mission-1"
CODING_MISSION_OPERATIONS = frozenset({"coding-mission", "repository-coding"})
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

MAX_LIST_ITEMS = 32
MAX_ITEM_LENGTH = 512
MIN_RUNTIME_SECONDS = 1
MAX_RUNTIME_SECONDS = 24 * 60 * 60
MIN_ATTEMPTS = 1
MAX_ATTEMPTS = 10

_LIST_FIELDS = (
    "allowed_actions",
    "forbidden_actions",
    "acceptance_criteria",
    "evidence",
)
_BUDGET_KEYS = ("max_cost_microunits", "max_input_tokens", "max_output_tokens")


def is_coding_mission(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    operation = metadata.get("operation")
    if isinstance(operation, str) and operation in CODING_MISSION_OPERATIONS:
        return True
    if metadata.get("contract_version"):
        return True
    return isinstance(metadata.get("mission"), dict)


def coding_mission_errors(
    *,
    goal: str,
    repository: str,
    base_ref: str,
    metadata: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Return field errors in stable order. Empty means the contract is complete."""
    payload = metadata if isinstance(metadata, dict) else {}
    mission = payload.get("mission")
    nested = mission if isinstance(mission, dict) else {}
    errors: list[dict[str, str]] = []

    version = payload.get("contract_version")
    if version not in (None, "", CONTRACT_VERSION):
        errors.append(
            {
                "field": "contract_version",
                "error": f"must be {CONTRACT_VERSION}",
            }
        )

    if not str(goal or "").strip():
        errors.append({"field": "goal", "error": "required"})
    if not REPO_PATTERN.match(str(repository or "").strip()):
        errors.append(
            {
                "field": "repository",
                "error": "must be owner/project",
            }
        )
    ref = str(base_ref or "").strip()
    if not ref or any(not character.isprintable() for character in ref):
        errors.append({"field": "base_ref", "error": "required"})

    for field in _LIST_FIELDS:
        errors.extend(_list_errors(field, _lookup(nested, payload, field)))

    runtime = _lookup(nested, payload, "max_runtime_seconds")
    if runtime is None:
        runtime = _lookup(nested, payload, "runtime_seconds")
    if runtime is None:
        runtime = _lookup(nested, payload, "runtime")
    errors.extend(
        _int_errors(
            "max_runtime_seconds",
            runtime,
            minimum=MIN_RUNTIME_SECONDS,
            maximum=MAX_RUNTIME_SECONDS,
        )
    )

    attempts = _lookup(nested, payload, "max_attempts")
    if attempts is None:
        attempts = _lookup(nested, payload, "attempts")
    errors.extend(
        _int_errors(
            "max_attempts",
            attempts,
            minimum=MIN_ATTEMPTS,
            maximum=MAX_ATTEMPTS,
        )
    )

    errors.extend(_budget_errors(_lookup(nested, payload, "budget")))
    errors.extend(_approval_errors(_lookup(nested, payload, "approval")))
    return errors


def normalized_coding_mission(
    *,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Return metadata keys to merge after coding_mission_errors is empty."""
    mission = metadata.get("mission")
    nested = mission if isinstance(mission, dict) else {}
    runtime = _lookup(nested, metadata, "max_runtime_seconds")
    if runtime is None:
        runtime = _lookup(nested, metadata, "runtime_seconds")
    if runtime is None:
        runtime = _lookup(nested, metadata, "runtime")
    attempts = _lookup(nested, metadata, "max_attempts")
    if attempts is None:
        attempts = _lookup(nested, metadata, "attempts")
    budget = _lookup(nested, metadata, "budget")
    approval = _lookup(nested, metadata, "approval")
    operation = metadata.get("operation")
    if operation not in CODING_MISSION_OPERATIONS:
        operation = "coding-mission"
    return {
        "contract_version": CONTRACT_VERSION,
        "operation": operation,
        "mission": {
            "allowed_actions": _clean_list(_lookup(nested, metadata, "allowed_actions")),
            "forbidden_actions": _clean_list(
                _lookup(nested, metadata, "forbidden_actions")
            ),
            "acceptance_criteria": _clean_list(
                _lookup(nested, metadata, "acceptance_criteria")
            ),
            "evidence": _clean_list(_lookup(nested, metadata, "evidence")),
            "max_runtime_seconds": int(runtime),
            "max_attempts": int(attempts),
            "budget": {
                key: int(budget[key])
                for key in _BUDGET_KEYS
                if isinstance(budget, dict) and key in budget
            },
            "approval": {
                "required": bool(approval.get("required"))
                if isinstance(approval, dict)
                else False,
                "gates": _clean_list(
                    approval.get("gates") if isinstance(approval, dict) else None
                ),
                "approver_role": str(approval.get("approver_role") or "").strip()
                if isinstance(approval, dict)
                else "",
            },
        },
    }


def _lookup(nested: dict[str, Any], payload: dict[str, Any], field: str) -> Any:
    if field in nested:
        return nested[field]
    return payload.get(field)


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            items.append(item.strip())
    return items


def _list_errors(field: str, value: Any) -> list[dict[str, str]]:
    if value is None:
        return [{"field": field, "error": "required"}]
    if not isinstance(value, list):
        return [{"field": field, "error": "must be a list of strings"}]
    if not value or len(value) > MAX_LIST_ITEMS:
        return [{"field": field, "error": "must contain 1 to 32 items"}]
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return [{"field": field, "error": "items must be non-empty strings"}]
        if len(item.strip()) > MAX_ITEM_LENGTH:
            return [{"field": field, "error": "items must be at most 512 characters"}]
    return []


def _int_errors(
    field: str,
    value: Any,
    *,
    minimum: int,
    maximum: int,
) -> list[dict[str, str]]:
    if value is None:
        return [{"field": field, "error": "required"}]
    if isinstance(value, bool) or not isinstance(value, int):
        return [{"field": field, "error": "must be an integer"}]
    if value < minimum or value > maximum:
        return [{"field": field, "error": f"must be between {minimum} and {maximum}"}]
    return []


def _budget_errors(value: Any) -> list[dict[str, str]]:
    if value is None:
        return [{"field": "budget", "error": "required"}]
    if not isinstance(value, dict) or isinstance(value, bool):
        return [{"field": "budget", "error": "must be an object"}]
    present = [key for key in _BUDGET_KEYS if key in value]
    if not present:
        return [
            {
                "field": "budget",
                "error": "must include max_cost_microunits, max_input_tokens, or max_output_tokens",
            }
        ]
    for key in present:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return [{"field": f"budget.{key}", "error": "must be an integer >= 0"}]
    return []


def _approval_errors(value: Any) -> list[dict[str, str]]:
    if value is None:
        return [{"field": "approval", "error": "required"}]
    if not isinstance(value, dict) or isinstance(value, bool):
        return [{"field": "approval", "error": "must be an object"}]
    if "required" not in value or not isinstance(value["required"], bool):
        return [{"field": "approval.required", "error": "must be a boolean"}]
    if not value["required"]:
        return []
    role = value.get("approver_role")
    gates = value.get("gates")
    has_role = isinstance(role, str) and bool(role.strip())
    has_gates = isinstance(gates, list) and any(
        isinstance(item, str) and item.strip() for item in gates
    )
    if has_role or has_gates:
        return []
    return [
        {
            "field": "approval",
            "error": "required approval needs approver_role or gates",
        }
    ]
