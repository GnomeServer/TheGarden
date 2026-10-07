from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from agent_manager.auth import _decode, create_session
from agent_manager.dashboard import TaskCreate, compact_forgejo_payload, forgejo_summary


class SessionContractTests(unittest.TestCase):
    def test_tampered_session_is_rejected(self) -> None:
        token, csrf = create_session("user-1")
        self.assertEqual(_decode(token)["csrf"], csrf)
        replacement = "A" if token[0] != "A" else "B"
        self.assertIsNone(_decode(replacement + token[1:]))


class TaskContractTests(unittest.TestCase):
    def test_deadline_requires_timezone(self) -> None:
        with self.assertRaises(ValidationError):
            TaskCreate(title="naive deadline", due_at=datetime(2030, 1, 2, 15, 0))

        task = TaskCreate(
            title="absolute deadline",
            due_at=datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(task.due_at.utcoffset().total_seconds(), 0)


class ForgejoContractTests(unittest.TestCase):
    def test_push_normalization_is_bounded_and_excludes_unrelated_fields(self) -> None:
        payload = {
            "ref": "refs/heads/main",
            "authorization": "must-not-be-copied",
            "commits": [
                {"id": str(index), "message": f"commit {index}", "secret": "drop"}
                for index in range(105)
            ],
        }
        compact = compact_forgejo_payload("push", payload)
        self.assertNotIn("authorization", compact)
        self.assertEqual(len(compact["commits"]), 100)
        self.assertNotIn("secret", compact["commits"][0])
        self.assertEqual(
            forgejo_summary("push", payload, "Alice", "owner/project"),
            "Alice pushed 105 commits to owner/project:main",
        )


if __name__ == "__main__":
    unittest.main()
