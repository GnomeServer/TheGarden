"""Crew sizing and review rules. No database and no NATS."""

from __future__ import annotations

import re



def crew_size(goal: str) -> dict[str, int | str]:
    """Scale planner/coder/reviewer counts with mission size; never add VMs."""
    length = len(goal)
    if length > 2500:
        return {"size": "large", "planners": 1, "coders": 3, "reviewers": 2}
    if length > 800:
        return {"size": "medium", "planners": 1, "coders": 2, "reviewers": 1}
    return {"size": "small", "planners": 1, "coders": 1, "reviewers": 1}


def crew_label(size: dict[str, int | str]) -> str:
    return (
        f"{size['size']}: {size['planners']} planner, "
        f"{size['coders']} coder(s), {size['reviewers']} reviewer(s)"
    )


def missing_requirements(goal: str) -> str | None:
    """Planner gate. A gap here bounces to OMP before any coder starts."""
    text = goal.casefold()
    if re.search(r"\bcalculator\b", text):
        operation_patterns = (
            r"\b(?:add|addition|additions|adding|sum|plus)\b",
            r"\b(?:subtract|subtraction|subtractions|subtracting|difference|differences|minus)\b",
            r"\b(?:multiply|multiplies|multiplying|multiplication|multiplications|times|product)\b",
            r"\b(?:divide|divides|dividing|division|divisions|quotient)\b",
        )
        has_ops = any(re.search(pattern, text) for pattern in operation_patterns)
        if not has_ops:
            return "The planner needs which operations the buttons should perform before a coder starts."
        return None
    if len(goal.strip()) < 40:
        return "The planner needs a concrete deliverable before a coder starts."
    return None


def is_chat_turn(goal: str) -> bool:
    """A question for Luna through OMP, not a request to start a crew."""
    text = goal.strip()
    folded = text.casefold()
    if folded.startswith("### task:"):
        return True
    if any(token in folded for token in ("calculator", "gui ", "tkinter", "ansible", "playbook", "create a program", "create an app", "error handling", "write the code", "complete code")):
        return False
    if folded.startswith(("build ", "implement ", "deploy ", "fix the ")):
        return False
    if folded in {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"} or folded.startswith("hey "):
        return True
    if text.endswith("?") or folded.startswith((
        "show me", "can you", "could you", "tell me", "what ", "who ", "why ",
        "how ", "explain", "give me", "write a snippet", "retry",
    )):
        return True
    return False


def attach_project_reply(goal: str, pending_goal: str | None) -> tuple[str, str | None]:
    """Use a short reply as the project name for the previous work request."""
    text = goal.strip()
    if (
        pending_goal
        and text
        and len(text) <= 120
        and "\n" not in text
        and not is_chat_turn(text)
    ):
        name = text.removeprefix("Project:").strip()
        return pending_goal, name or None
    return goal, None
