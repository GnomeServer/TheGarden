from __future__ import annotations

import unittest

from pydantic import ValidationError

from agent_manager.main import RunCreate


class RunNamingContractTests(unittest.TestCase):
    def test_names_are_optional_and_default_to_legacy_behavior(self) -> None:
        request = RunCreate(goal="Build a report", forgejo_repository="owner/repo")
        self.assertIsNone(request.project_name)
        self.assertIsNone(request.script_name)

    def test_project_name_is_trimmed_and_script_name_gets_python_extension(self) -> None:
        request = RunCreate(
            goal="Build a report",
            forgejo_repository="owner/repo",
            project_name="  Quarterly Reports  ",
            script_name="audit_report",
        )
        self.assertEqual(request.project_name, "Quarterly Reports")
        self.assertEqual(request.script_name, "audit_report.py")

    def test_script_name_rejects_paths_and_non_python_extensions(self) -> None:
        for script_name in ("../escape.py", r"folder\\escape.py", "report.txt"):
            with self.subTest(script_name=script_name):
                with self.assertRaises(ValidationError):
                    RunCreate(
                        goal="Build a report",
                        forgejo_repository="owner/repo",
                        script_name=script_name,
                    )

    def test_project_name_rejects_control_characters(self) -> None:
        with self.assertRaises(ValidationError):
            RunCreate(
                goal="Build a report",
                forgejo_repository="owner/repo",
                project_name="bad\nname",
            )


if __name__ == "__main__":
    unittest.main()
