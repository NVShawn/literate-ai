"""Locked provider-neutral Component command authority tests."""

from __future__ import annotations

import unittest

from literate_ai.contracts import (
    ComponentCommandPhase,
    ComponentCommandRole,
    ComponentLifecycleCommand,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

LIFECYCLE_COMMAND_SCHEMA = "urn:literate-ai:schema:v2:component-lifecycle-command"


def command(phase: ComponentCommandPhase) -> ComponentLifecycleCommand:
    if phase is ComponentCommandPhase.BUILD:
        argv = (
            "{tool}",
            "build",
            "{source_root}",
            "--object-root",
            "{object_root}",
            "--output",
            "{export_path}",
            "{provider_artifacts}",
        )
    else:
        argv = ("{tool}", phase.value, "{artifact_root}")
    return ComponentLifecycleCommand(phase, argv)


class ComponentCommandContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = SchemaCatalog()

    def test_shell_strings_and_inferred_executables_are_rejected(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            ComponentLifecycleCommand(ComponentCommandPhase.BUILD, "tool build source")
        with self.assertRaises((TypeError, ValueError)):
            ComponentLifecycleCommand(
                ComponentCommandPhase.BUILD,
                (
                    "python",
                    "build.py",
                    "{source_root}",
                    "{object_root}",
                    "{export_path}",
                ),
            )
        wire = command(ComponentCommandPhase.BUILD).to_dict()
        wire["argv"] = "sh -c 'build source'"
        with self.assertRaises((TypeError, ValueError)):
            ComponentLifecycleCommand.from_dict(wire)
        with self.assertRaises(AssertionError):
            self.catalog.validate(LIFECYCLE_COMMAND_SCHEMA, wire)

    def test_substitution_requires_exact_typed_roles_and_never_a_shell(self) -> None:
        build = command(ComponentCommandPhase.BUILD)
        bindings = {
            ComponentCommandRole.TOOL: ("bazel",),
            ComponentCommandRole.SOURCE_ROOT: ("/work/source",),
            ComponentCommandRole.OBJECT_ROOT: ("/work/object",),
            ComponentCommandRole.EXPORT_PATH: ("/work/artifact/app",),
            ComponentCommandRole.PROVIDER_ARTIFACTS: (
                "/providers/money",
                "/providers/reporting",
            ),
        }
        self.assertEqual(
            build.substitute(bindings),
            (
                "bazel",
                "build",
                "/work/source",
                "--object-root",
                "/work/object",
                "--output",
                "/work/artifact/app",
                "/providers/money",
                "/providers/reporting",
            ),
        )
        self.assertNotIn(
            "{provider_artifacts}",
            build.substitute({**bindings, ComponentCommandRole.PROVIDER_ARTIFACTS: ()}),
        )
        self.assertEqual(
            build.substitute(
                {
                    **bindings,
                    ComponentCommandRole.TOOL: ("python3", "-I"),
                }
            )[:3],
            ("python3", "-I", "build"),
        )
        invalid = (
            {
                key: item
                for key, item in bindings.items()
                if key is not ComponentCommandRole.TOOL
            },
            {**bindings, ComponentCommandRole.ARTIFACT_ROOT: ("/extra",)},
            {**bindings, ComponentCommandRole.TOOL: "bazel"},
            {**bindings, ComponentCommandRole.TOOL: ()},
            {**bindings, ComponentCommandRole.TOOL: ("bazel\x00sh",)},
        )
        for index, candidate in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaises((TypeError, ValueError)):
                    build.substitute(candidate)


if __name__ == "__main__":
    unittest.main()
