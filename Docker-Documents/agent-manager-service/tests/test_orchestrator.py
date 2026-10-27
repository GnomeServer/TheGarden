import unittest

from agent_manager.crew_rules import crew_size, is_chat_turn


class CrewSizeTests(unittest.TestCase):
    def test_small_missions_use_one_of_each_role(self) -> None:
        size = crew_size("Build a standalone calculator GUI.")
        self.assertEqual(
            size,
            {"size": "small", "planners": 1, "coders": 1, "reviewers": 1},
        )

    def test_large_missions_scale_coders_and_reviewers(self) -> None:
        size = crew_size("large project " * 250)
        self.assertEqual(size["size"], "large")
        self.assertEqual(size["coders"], 3)
        self.assertEqual(size["reviewers"], 2)


class ConversationRoutingTests(unittest.TestCase):
    def test_general_task_intents_route_to_a_worker(self) -> None:
        goals = (
            "Could you generate a GUI of a calculator?",
            "Build a contact page with a submit button that shows a confirmation message.",
            "Create a tabbed dashboard GUI with buttons for navigating between reports.",
        )
        for goal in goals:
            with self.subTest(goal=goal):
                self.assertFalse(is_chat_turn(goal))

    def test_question_intents_remain_chat_turns(self) -> None:
        self.assertTrue(is_chat_turn("How do I add a button to a web page?"))

if __name__ == "__main__":
    unittest.main()
