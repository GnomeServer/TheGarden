from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from agent_manager.tracking import AccessEventIn, grafana_node_url, key_hash, vector_by_node


class AccessEventContractTests(unittest.TestCase):
    def test_event_requires_absolute_nonfuture_time(self) -> None:
        with self.assertRaises(ValidationError):
            AccessEventIn(
                event_id="event-1",
                source="sshd",
                service="ssh",
                event_type="login",
                occurred_at=datetime(2030, 1, 1),
                summary="login",
            )
        with self.assertRaises(ValidationError):
            AccessEventIn(
                event_id="event-2",
                source="sshd",
                service="ssh",
                event_type="login",
                occurred_at=datetime.now(timezone.utc) + timedelta(minutes=6),
                summary="future login",
            )

    def test_node_key_hash_is_keyed_and_not_plaintext(self) -> None:
        token = "dfn_credential.secret"
        digest = key_hash(token)
        self.assertNotIn("secret", digest)
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, key_hash(token + "-different"))


class PrometheusContractTests(unittest.TestCase):
    def test_vector_prefers_stable_worker_identity(self) -> None:
        vector = [
            {
                "metric": {
                    "instance": "100.94.145.104:9100",
                    "host": "donatello",
                    "worker_id": "worker-03",
                    "node_id": "worker-03",
                },
                "value": [1_000, "37.55"],
            }
        ]
        self.assertEqual(vector_by_node(vector), {"worker-03": 37.5})

    def test_grafana_host_cannot_inject_query_parameters(self) -> None:
        host = "donatello &var-host=another-node"
        query = parse_qs(urlsplit(grafana_node_url(host)).query)
        self.assertEqual(query["var-host"], [host])
        self.assertNotIn("var-node", query)


if __name__ == "__main__":
    unittest.main()
