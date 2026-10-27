"""Crew sizing and review rules. No database and no NATS."""

from __future__ import annotations

import re

MISSION_REQUEST = re.compile(
    r"^\s*(?:(?:please\s+)?(?:(?:can|could|would)\s+you(?:\s+please)?\s+)?)?"
    r"(?:build|create|generate|implement|deploy|fix|write|make|design|modify|"
    r"change|update|revise|edit|add|remove|replace|develop|produce|construct)\b",
    re.IGNORECASE,
)



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




def is_chat_turn(goal: str) -> bool:
    """Route conversational questions to Luna and task instructions to a crew."""
    text = goal.strip()
    folded = text.casefold()
    if folded.startswith("### task:"):
        return True
    if folded in {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}:
        return True
    if folded.startswith(("hey ", "write a snippet", "can you write a snippet", "could you write a snippet")):
        return True
    if MISSION_REQUEST.match(folded):
        return False
    if text.endswith("?") or folded.startswith((
        "show me", "can you", "could you", "would you", "tell me", "what ", "who ",
        "why ", "how ", "explain", "give me", "retry",
    )):
        return True
    return False
