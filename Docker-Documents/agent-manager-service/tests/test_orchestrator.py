import unittest

from agent_manager.orchestrator import (
    container_create_body,
    crew_size,
    missing_requirements,
    review_locally,
)


class CrewSizeTests(unittest.TestCase):
    def test_a_short_request_gets_one_of_each_role(self) -> None:
        size = crew_size("Write a short status note for the lab dashboard.")
        self.assertEqual(size["size"], "small")
        self.assertEqual(size["coders"], 1)
        self.assertEqual(size["reviewers"], 1)
        self.assertNotIn("vm", size)

    def test_a_long_request_adds_coders_and_reviewers(self) -> None:
        size = crew_size("detail " * 500)
        self.assertEqual(size["size"], "large")
        self.assertEqual(size["coders"], 3)
        self.assertEqual(size["reviewers"], 2)


class PlannerTests(unittest.TestCase):
    def test_a_complete_calculator_request_can_start(self) -> None:
        goal = (
            "i would like a GUI calculator that can add, subtract, divide, and multiply. "
            "input should be in the form of buttons for the user to click on. "
            "make the background color a pale blue and the buttons just white with black text"
        )
        self.assertIsNone(missing_requirements(goal))

    def test_a_gui_without_operations_bounces(self) -> None:
        gap = missing_requirements("Build a GUI calculator with buttons.")
        self.assertIn("operations", gap or "")


class ReviewerTests(unittest.TestCase):
    def test_a_script_that_leaves_the_crew_is_rejected(self) -> None:
        reason = review_locally("Write a note.", "import subprocess\nsubprocess.run(['id'])\n")
        self.assertIn("leave the crew", reason or "")

    def test_container_body_has_no_socket_and_no_host_network(self) -> None:
        body = container_create_body("crew-coder", "crew-net", "python:3.12-slim", "coder")
        host = body["HostConfig"]
        self.assertEqual(host["NetworkMode"], "crew-net")
        self.assertNotEqual(host["NetworkMode"], "host")
        self.assertFalse(host["Privileged"])
        self.assertNotIn("Binds", host)
        self.assertNotIn("docker.sock", str(body))
        self.assertNotIn("/var/run/docker.sock", body["Env"])


if __name__ == "__main__":
    unittest.main()
