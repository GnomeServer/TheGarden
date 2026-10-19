"""Whiteboard crew loop over the NATS server already listening on this laptop.

Does not publish agent.runs.created. That subject is the live laptop-worker queue.
"""

from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from typing import Any

import nats

from agent_manager.crew_rules import crew_label, crew_size, missing_requirements, review_locally

NATS_URL = "nats://127.0.0.1:4222"
CALCULATOR = (
    "i would like a GUI calculator that can add, subtract, divide, and multiply. "
    "input should be in the form of buttons for the user to click on. "
    "make the background color a pale blue and the buttons just white with black text"
)
CALCULATOR_SCRIPT = """import tkinter as tk

def main():
    root = tk.Tk()
    root.configure(bg="#d6eaf8")
    for label in ("add", "subtract", "multiply", "divide"):
        tk.Button(root, text=label, bg="white", fg="black").pack()
    root.mainloop()

if __name__ == "__main__":
    main()
"""


def dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def loads(data: bytes) -> dict[str, Any]:
    parsed = json.loads(data.decode())
    if not isinstance(parsed, dict):
        raise TypeError("crew message must be an object")
    return parsed


async def wait_for(queue: asyncio.Queue[dict[str, Any]], timeout: float = 5) -> dict[str, Any]:
    return await asyncio.wait_for(queue.get(), timeout)


async def run_whiteboard(nc: nats.NATS, prefix: str) -> list[dict[str, Any]]:
    chat: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    conversations: dict[str, dict[str, str]] = {}
    revisions = {"count": 0}

    async def publish(subject: str, payload: dict[str, Any]) -> None:
        if not subject.startswith(prefix):
            raise RuntimeError(f"refusing to publish outside the test prefix: {subject}")
        await nc.publish(subject, dumps(payload))

    async def omp(msg: nats.Msg) -> None:
        body = loads(msg.data)
        conversation_id = str(body.get("conversation_id") or "")
        state = conversations.setdefault(conversation_id, {})
        if body.get("type") == "result":
            await publish(f"{prefix}.chat", body)
            return
        if body.get("type") == "bounce":
            await publish(
                f"{prefix}.chat",
                {"type": "question", "text": body.get("text"), "conversation_id": conversation_id},
            )
            return
        text = str(body.get("text") or "").strip()
        if state.get("waiting") == "project" and text:
            state["project_name"] = text.removeprefix("Project:").strip()
            state["waiting"] = ""
            await publish(
                f"{prefix}.orchestrator",
                {
                    "type": "mission",
                    "conversation_id": conversation_id,
                    "goal": state.get("goal", ""),
                    "project_name": state["project_name"],
                },
            )
            return
        if len(text) < 12:
            await publish(
                f"{prefix}.chat",
                {"type": "question", "text": "What should the crew do?", "conversation_id": conversation_id},
            )
            return
        state["goal"] = text
        state["waiting"] = "project"
        await publish(
            f"{prefix}.chat",
            {
                "type": "question",
                "text": "What project name should this mission use?",
                "conversation_id": conversation_id,
            },
        )

    async def orchestrator(msg: nats.Msg) -> None:
        body = loads(msg.data)
        goal = str(body.get("goal") or "")
        gap = missing_requirements(goal)
        if gap:
            await publish(
                f"{prefix}.omp",
                {"type": "bounce", "text": gap, "conversation_id": body.get("conversation_id")},
            )
            return
        size = crew_size(goal)
        await publish(
            f"{prefix}.planner",
            {**body, "crew": size, "crew_label": crew_label(size)},
        )

    async def planner(msg: nats.Msg) -> None:
        body = loads(msg.data)
        gap = missing_requirements(str(body.get("goal") or ""))
        if gap:
            await publish(
                f"{prefix}.omp",
                {"type": "bounce", "text": gap, "conversation_id": body.get("conversation_id")},
            )
            return
        await publish(f"{prefix}.coder", body)

    async def coder(msg: nats.Msg) -> None:
        body = loads(msg.data)
        await publish(f"{prefix}.reviewer", {**body, "script": CALCULATOR_SCRIPT})

    async def reviewer(msg: nats.Msg) -> None:
        body = loads(msg.data)
        goal = str(body.get("goal") or "")
        script = str(body.get("script") or "")
        rejection = review_locally(goal, script)
        if rejection and revisions["count"] < 1:
            revisions["count"] += 1
            await publish(f"{prefix}.coder", {**body, "reviewer_notes": rejection})
            return
        if rejection:
            await publish(
                f"{prefix}.omp",
                {"type": "bounce", "text": rejection, "conversation_id": body.get("conversation_id")},
            )
            return
        await publish(
            f"{prefix}.omp",
            {
                "type": "result",
                "conversation_id": body.get("conversation_id"),
                "project_name": body.get("project_name"),
                "goal": goal,
                "crew_label": body.get("crew_label"),
                "text": "Reviewer accepted the script.",
                "script": script,
            },
        )

    async def chat_in(msg: nats.Msg) -> None:
        chat.put_nowait(loads(msg.data))

    subscriptions = []
    for subject, handler in (
        (f"{prefix}.omp", omp),
        (f"{prefix}.orchestrator", orchestrator),
        (f"{prefix}.planner", planner),
        (f"{prefix}.coder", coder),
        (f"{prefix}.reviewer", reviewer),
        (f"{prefix}.chat", chat_in),
    ):
        subscriptions.append(await nc.subscribe(subject, cb=handler))
    await nc.flush()

    conversation_id = uuid.uuid4().hex
    await publish(
        f"{prefix}.omp",
        {"type": "turn", "conversation_id": conversation_id, "text": CALCULATOR},
    )
    question = await wait_for(chat)
    await publish(
        f"{prefix}.omp",
        {"type": "turn", "conversation_id": conversation_id, "text": "Garden"},
    )
    answer = await wait_for(chat)
    for subscription in subscriptions:
        await subscription.unsubscribe()
    return [question, answer]


class WhiteboardNatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_follow_up_keeps_the_goal_and_returns_a_script(self) -> None:
        try:
            nc = await nats.connect(NATS_URL, connect_timeout=2)
        except Exception as exc:
            self.skipTest(f"local NATS is not reachable: {exc}")
        prefix = f"agent.crew.wb.{uuid.uuid4().hex}"
        try:
            question, answer = await run_whiteboard(nc, prefix)
        finally:
            await nc.close()

        self.assertEqual(question["type"], "question")
        self.assertEqual(question["text"], "What project name should this mission use?")
        self.assertEqual(answer["type"], "result")
        self.assertEqual(answer["project_name"], "Garden")
        self.assertIn("add, subtract", answer["goal"])
        self.assertEqual(answer["crew_label"], "small: 1 planner, 1 coder(s), 1 reviewer(s)")
        self.assertEqual(answer["text"], "Reviewer accepted the script.")
        self.assertIn("tkinter", answer["script"])
        self.assertIn("#d6eaf8", answer["script"])
        self.assertNotIn("agent.runs.created", prefix)


if __name__ == "__main__":
    unittest.main()
