"""Discriminated Standard and external project lifecycle-driver contracts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from literate_ai.contracts import (
    ExternalProjectLifecycleDriver,
    ProjectDefinition,
    ProjectLifecycleDriver,
    StandardProjectLifecycleDriver,
    canonical_identity,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DRIVER_SCHEMA = "urn:literate-ai:schema:v2:project-lifecycle-driver"


def identity(label: str):
    return canonical_identity({"project-lifecycle-driver-test": label})


def project_document() -> dict[str, object]:
    return json.loads(
        (REPOSITORY_ROOT / "literate.project.json").read_text(encoding="utf-8")
    )


class ProjectLifecycleDriverBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = SchemaCatalog()

    def test_standard_binding_round_trips_and_validates(self) -> None:
        binding = StandardProjectLifecycleDriver(
            identity("installed-framework"), identity("standard-policy")
        )

        wire = binding.to_dict()
        self.assertEqual(wire["binding"], "standard")
        self.assertEqual(StandardProjectLifecycleDriver.from_dict(wire), binding)
        self.catalog.validate(DRIVER_SCHEMA, wire)

        project = project_document()
        project["lifecycle_driver"] = wire
        parsed = ProjectDefinition.from_dict(project)
        self.assertEqual(parsed.lifecycle_driver, binding)
        self.assertEqual(ProjectDefinition.from_dict(parsed.to_dict()), parsed)

    def test_existing_external_wire_remains_byte_shape_compatible(self) -> None:
        wire = project_document()["lifecycle_driver"]
        self.assertIsInstance(wire, dict)
        driver = ProjectLifecycleDriver.from_dict(wire)

        self.assertIsInstance(driver, ExternalProjectLifecycleDriver)
        self.assertEqual(driver.binding, "external")
        self.assertEqual(driver.to_dict(), wire)
        self.catalog.validate(DRIVER_SCHEMA, wire)

        explicit = {**wire, "binding": "external"}
        self.assertEqual(ProjectLifecycleDriver.from_dict(explicit), driver)
        self.catalog.validate(DRIVER_SCHEMA, explicit)

    def test_standard_binding_rejects_missing_mixed_and_unknown_fields(self) -> None:
        valid = StandardProjectLifecycleDriver(
            identity("framework"), identity("policy")
        ).to_dict()
        invalid = (
            {key: value for key, value in valid.items() if key != "policy_identity"},
            {**valid, "implementation_paths": ["scripts/driver.py"]},
            {**valid, "binding": "builtin"},
            {**valid, "framework_distribution_identity": "sha256:" + "a" * 64},
        )
        for index, wire in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaises((TypeError, ValueError)):
                    StandardProjectLifecycleDriver.from_dict(wire)
                with self.assertRaises(AssertionError):
                    self.catalog.validate(DRIVER_SCHEMA, wire)

    def test_project_dispatch_rejects_unknown_or_hybrid_bindings(self) -> None:
        standard = StandardProjectLifecycleDriver(
            identity("framework"), identity("policy")
        ).to_dict()
        external = project_document()["lifecycle_driver"]
        invalid = (
            {**standard, "binding": "unknown"},
            {**external, "binding": "standard"},
            {**external, "framework_distribution_identity": identity("x").to_dict()},
        )
        for index, driver in enumerate(invalid):
            project = project_document()
            project["lifecycle_driver"] = driver
            with self.subTest(case=index):
                with self.assertRaises((TypeError, ValueError)):
                    ProjectDefinition.from_dict(project)
                with self.assertRaises(AssertionError):
                    self.catalog.validate(DRIVER_SCHEMA, driver)

    def test_standard_binding_requires_typed_exact_identities(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            StandardProjectLifecycleDriver("sha256:" + "a" * 64, identity("policy"))
        with self.assertRaises((TypeError, ValueError)):
            StandardProjectLifecycleDriver(identity("framework"), None)


if __name__ == "__main__":
    unittest.main()
