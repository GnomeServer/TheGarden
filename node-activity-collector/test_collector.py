from __future__ import annotations

import importlib.util
import unittest
import hashlib
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("activity_collector", Path(__file__).with_name("collector.py"))
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class CollectorNormalizationTests(unittest.TestCase):
    def record(self, message: str, identifier: str = "sshd") -> dict[str, str]:
        return {
            "MESSAGE": message,
            "SYSLOG_IDENTIFIER": identifier,
            "__CURSOR": hashlib.sha256(message.encode()).hexdigest(),
            "__REALTIME_TIMESTAMP": "1760000000000000",
        }

    def test_successful_ssh_login_preserves_identity_and_remote(self) -> None:
        event = collector.normalize(
            self.record("Accepted publickey for bgurrol4 from 100.64.0.10 port 51234 ssh2")
        )
        self.assertEqual(event["event_type"], "login")
        self.assertEqual(event["identity"], "bgurrol4")
        self.assertEqual(event["identity_source"], "linux")
        self.assertEqual(event["remote_identity"], "100.64.0.10")

    def test_failed_login_is_security_evidence(self) -> None:
        event = collector.normalize(
            self.record("Failed password for invalid user root from 100.64.0.20 port 50000 ssh2")
        )
        self.assertEqual(event["event_type"], "auth_failed")
        self.assertEqual(event["identity"], "root")

    def test_sudo_event_does_not_forward_command(self) -> None:
        raw = "bgurrol4 : TTY=pts/0 ; PWD=/tmp ; COMMAND=/usr/bin/cat /secret"
        event = collector.normalize(self.record(raw, identifier="sudo"))
        self.assertEqual(event["event_type"], "privilege_use")
        self.assertNotIn("COMMAND", str(event))
        self.assertNotIn("/secret", str(event))


if __name__ == "__main__":
    unittest.main()
