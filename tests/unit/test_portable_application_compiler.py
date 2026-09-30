"""Portable application specification validation tests."""

from __future__ import annotations

import unittest

from literate_ai.adapters.models import (
    PORTABLE_APPLICATION_SCHEMA,
    PortableApplicationError,
    portable_source_entrypoint,
    validate_portable_application,
)
from literate_ai.contracts import CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR


def greeting_spec() -> dict[str, object]:
    return {
        "schema": PORTABLE_APPLICATION_SCHEMA,
        "application_id": "hello",
        "kind": "greeting-summary",
        "entrypoint": "run",
        "configuration": {
            "metrics": ["message_count", "word_count"],
            "recipient_id": "unicode-alphanumeric-kebab",
        },
    }


def dependency_planner_spec() -> dict[str, object]:
    return {
        "schema": PORTABLE_APPLICATION_SCHEMA,
        "application_id": "dependency-planner",
        "kind": "dependency-planner",
        "entrypoint": "run",
        "configuration": {
            "algorithm": "critical-path-method",
            "dependency_field": "depends_on",
            "duration_bounds": {
                "maximum": 1_000_000,
                "maximum_total": 2_147_483_647,
                "minimum": 1,
            },
            "duration_field": "duration_minutes",
            "maximum_tasks": 512,
            "ordering": "ascii-task-id-ascending",
            "parallelism": "unlimited",
        },
    }


def framework_readiness_spec() -> dict[str, object]:
    return {
        "schema": PORTABLE_APPLICATION_SCHEMA,
        "application_id": "self-hosting",
        "kind": "framework-compatibility-readiness-command",
        "entrypoint": "run",
        "configuration": {
            "operation": "framework-compatibility-readiness",
            "expected_framework_version": "0.2.0",
            "required_skill_ids": ["architecture", "security"],
        },
    }


class PortableApplicationSpecificationTests(unittest.TestCase):
    def test_language_neutral_specification_has_flavor_contributed_entrypoints(self):
        first = validate_portable_application(greeting_spec())
        second = validate_portable_application(greeting_spec())
        self.assertEqual(first, second)
        self.assertEqual(first["entrypoint"], "run")
        self.assertEqual(portable_source_entrypoint("python"), "source/main.py")
        self.assertEqual(portable_source_entrypoint("cpp"), "source/main.cpp")

    def test_unsupported_or_underspecified_configuration_fails_closed(self):
        value = greeting_spec()
        value["configuration"] = {"metrics": ["message_count"]}
        with self.assertRaisesRegex(PortableApplicationError, "configuration fields"):
            validate_portable_application(value)

    def test_model_selection_is_part_of_the_validated_specification(self):
        value = greeting_spec()
        value["models"] = {
            "codex": "gpt-example",
            "claude": "claude-example",
            "cursor-agent": "cursor-example",
            "opencode": "openai/gpt-example",
        }
        self.assertEqual(
            validate_portable_application(value)["models"], value["models"]
        )

    def test_reserved_default_selector_is_not_a_configured_model(self):
        value = greeting_spec()
        value["models"] = {"codex": CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR}

        with self.assertRaisesRegex(PortableApplicationError, "coding models"):
            validate_portable_application(value)

    def test_dependency_planner_has_its_own_exact_application_contract(self):
        value = dependency_planner_spec()
        self.assertEqual(validate_portable_application(value), value)

        value["configuration"]["parallelism"] = "serial"
        with self.assertRaisesRegex(PortableApplicationError, "unsupported"):
            validate_portable_application(value)

    def test_framework_readiness_is_not_a_self_generation_contract(self):
        value = framework_readiness_spec()
        self.assertEqual(validate_portable_application(value), value)

        value["kind"] = "self-host-command"
        with self.assertRaisesRegex(PortableApplicationError, "contract"):
            validate_portable_application(value)


if __name__ == "__main__":
    unittest.main()
