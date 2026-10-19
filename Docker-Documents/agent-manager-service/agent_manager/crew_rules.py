"""Crew sizing and review rules. No database and no NATS."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

UNSAFE_SCRIPT = re.compile(
    r"\b(subprocess|socket|urllib|requests|eval|exec|os\.system|__import__|docker|pty)\b"
)
SCRIPT_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
MAX_SCRIPT_CHARS = 20000


def crew_size(goal: str) -> dict[str, int | str]:
    """More containers for a larger job. Never a new VM."""
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
    if any(word in text for word in ("calculator", "gui", "button")):
        missing: list[str] = []
        has_ops = any(word in text for word in ("add", "subtract", "multiply", "divide", "all computation", "computation"))
        if not has_ops:
            missing.append("which operations the buttons should perform")
        if "button" not in text and "gui" not in text and "calculator" not in text:
            missing.append("whether input is buttons or a text field")
        if missing:
            return "The planner needs " + " and ".join(missing) + " before a coder starts."
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
    if any(token in folded for token in ("calculator", "gui ", "ansible", "playbook", "create a program", "create an app")):
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


def extract_script(text: str) -> str:
    match = SCRIPT_FENCE.search(text)
    script = match.group(1) if match else text
    return script.strip()[:MAX_SCRIPT_CHARS]


def review_locally(goal: str, script: str) -> str | None:
    """Reject an unsafe or incomplete script. None means the reviewer accepts it."""
    if not script or "import " not in script:
        return "The coder did not return a Python script."
    if UNSAFE_SCRIPT.search(script):
        return "The reviewer rejected the script because it tried to leave the crew."
    text = goal.casefold()
    if "calculator" not in text and "gui" not in text:
        return None
    missing: list[str] = []
    folded = script.casefold()
    if "tkinter" not in folded:
        missing.append("tkinter buttons")
    if not all(token in folded for token in ("add", "subtract", "multiply", "divide")) and not all(
        token in script for token in ("+", "-", "*", "/")
    ):
        missing.append("add, subtract, multiply, and divide")
    if "button" not in folded:
        missing.append("clickable buttons")
    if not any(color in folded for color in ("#d6eaf8", "#add8e6", "pale blue", "lightblue", "light blue")):
        missing.append("a pale blue background")
    if "white" not in folded or "black" not in folded:
        missing.append("white buttons with black text")
    if missing:
        return "The reviewer still needs " + ", ".join(missing) + "."
    return None


def container_create_body(name: str, network: str, image: str, role: str) -> dict[str, Any]:
    """One role container. No socket, no host network, no privileges, no lab env."""
    del name
    return {
        "Image": image,
        "User": "65534:65534",
        "Env": [f"ROLE={role}"],
        "Cmd": ["python", "-c", "import time; time.sleep(120)"],
        "HostConfig": {
            "NetworkMode": network,
            "ReadonlyRootfs": True,
            "Memory": 256 * 1024 * 1024,
            "PidsLimit": 64,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"},
            "Privileged": False,
        },
        "NetworkingConfig": {"EndpointsConfig": {network: {}}},
    }


def compile_script(script: str) -> str | None:
    path: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="crew-") as directory:
            path = Path(directory) / "crew_script.py"
            path.write_text(script, encoding="utf-8")
            compiled = compile(script, str(path), "exec")
            del compiled
        return None
    except SyntaxError:
        return "The reviewer rejected the script because it does not compile."
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
