"""Ordinary build/test commands preserve explicit retained authority boundaries."""

import unittest
from unittest.mock import patch

from literate_ai.cli.build_run import build_from_args, test_from_args
from literate_ai.cli.dispatch import (
    _human_build_text,
    _human_rebuild_text,
    _human_test_text,
    _parser,
)
from literate_ai.cli.errors import CliFailure


class RetainedBuildFrontendTests(unittest.TestCase):
    def test_orphaned_or_empty_retained_options_cannot_enter_generated_execution(self):
        options = (
            ["--retained-project-profile", "profile.json"],
            ["--retained-project-plan"],
            ["--authorize-retained-project", "sha256:" + "a" * 64],
            ["--retained-project", ""],
        )
        for command, handler in (("build", build_from_args), ("test", test_from_args)):
            for option in options:
                with (
                    self.subTest(command=command, option=option),
                    patch(
                        "literate_ai.cli.rebuild._project",
                        side_effect=AssertionError(
                            "must refuse before project execution"
                        ),
                    ),
                ):
                    args = _parser().parse_args([command, *option])
                    with self.assertRaises(CliFailure) as failure:
                        handler(args)
                    self.assertEqual(
                        failure.exception.code, "retained_project.manifest_required"
                    )

    def test_execution_requires_host_authorization_even_with_a_reviewed_plan(self):
        for command, handler in (("build", build_from_args), ("test", test_from_args)):
            with self.subTest(command=command):
                args = _parser().parse_args(
                    [
                        command,
                        "--retained-project",
                        "manifest.json",
                        "--retained-project-profile",
                        "profile.json",
                        "--authorize-retained-project",
                        "sha256:" + "a" * 64,
                    ]
                )
                with self.assertRaises(CliFailure) as failure:
                    handler(args)
                self.assertEqual(
                    failure.exception.code, "rebuild.host_execution_not_acknowledged"
                )

    def test_operator_output_distinguishes_planning_admission_and_package_authority(
        self,
    ):
        plan = {
            "authority_mode": "retained-project",
            "admitted": False,
            "authorization_identity": "sha256:" + "a" * 64,
        }
        admitted = {
            "authority_mode": "retained-project",
            "admitted": True,
            "plan_identity": plan["authorization_identity"],
            "retained_receipt_identity": "sha256:" + "b" * 64,
            "custody_root": "retained-cas",
        }
        for renderer in (_human_build_text, _human_test_text, _human_rebuild_text):
            with self.subTest(renderer=renderer.__name__):
                output = renderer(plan)
                self.assertIn("planned (read-only)", output)
                self.assertNotIn("FAILED", output)
                self.assertNotIn("passed", output)
                self.assertNotIn("Artifact:", output)
                output = renderer(admitted)
                self.assertIn("retained project admitted", output)
                self.assertIn(admitted["retained_receipt_identity"], output)
                self.assertIn(
                    "Package and release qualification remain separate", output
                )
                self.assertNotIn("litai run", output)


if __name__ == "__main__":
    unittest.main()
