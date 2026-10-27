import unittest

from agent_manager.crew_rules import crew_size, missing_requirements


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


class PlannerGateTests(unittest.TestCase):
    def test_live_calculator_prompt_recognizes_operation_nouns_and_gerunds(self) -> None:
        goal = (
            "Could you generate a GUI of a calculator that computes differences, "
            "additions, subtractions, multiplication, and dividing?"
        )

        self.assertIsNone(missing_requirements(goal))

    def test_calculator_without_operations_requests_only_missing_detail(self) -> None:
        gap = missing_requirements("Build a GUI calculator with buttons.")

        self.assertEqual(
            gap,
            "The planner needs which operations the buttons should perform before a coder starts.",
        )


    def test_generic_gui_and_button_tasks_do_not_trigger_calculator_gate(self) -> None:
        goals = (
            "Build a contact page with a submit button that shows a confirmation message.",
            "Create a tabbed dashboard GUI with buttons for navigating between reports.",
        )
        for goal in goals:
            with self.subTest(goal=goal):
                self.assertIsNone(missing_requirements(goal))


if __name__ == "__main__":
    unittest.main()
