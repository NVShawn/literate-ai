"""Authored Component intent stays readable and free of implicit resolver pins."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.capabilities import CapabilityRequirement, DependencyKind
from literate_ai.contracts.component_locking import (
    AuthoredProvidedCapability,
    AuthoredRepositorySourceDependency,
    ComponentAssetSelector,
    ComponentAuthoring,
    ComponentContentSelector,
)
from literate_ai.contracts.components import Entrypoint
from literate_ai.contracts.flavors import FlavorAxis, FlavorCardinality, FlavorSlot
from literate_ai.contracts.identity import ComponentCoordinate, canonical_identity
from literate_ai.contracts.repositories import (
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas/v2/component-authoring-locks.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def schema_catalog() -> SchemaCatalog:
    catalog = SchemaCatalog(ROOT / "schemas/v2")
    for path in sorted((ROOT / "schemas/v1").glob("*.schema.json")):
        catalog._collect(json.loads(path.read_text(encoding="utf-8")))
    return catalog


def official_validator(resource_id: str) -> Draft202012Validator:
    registry = Registry()
    for directory in (ROOT / "schemas/v1", ROOT / "schemas/v2"):
        for path in sorted(directory.glob("*.schema.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            registry = registry.with_resource(
                document["$id"], Resource.from_contents(document)
            )
    return Draft202012Validator({"$ref": resource_id}, registry=registry.crawl())


def selector(kind: str, uri: str, *, pinned: bool = False) -> ComponentContentSelector:
    return ComponentContentSelector(
        kind,
        uri,
        canonical_identity({"pin": uri}) if pinned else None,
    )


def authored_component() -> ComponentAuthoring:
    return ComponentAuthoring(
        coordinate=ComponentCoordinate("samples", "invoice-cli"),
        version="1.0.0",
        display_name="Invoice CLI",
        description="A portable invoice application.",
        profiles=("application", "portable", "sample"),
        sample=True,
        provides=(
            AuthoredProvidedCapability(
                "invoice-api",
                "1.0.0",
                selector("public-interface-contract", "interfaces/invoice.json"),
            ),
        ),
        requires=(
            CapabilityRequirement(
                "money",
                "money-api",
                ">=1,<2",
                DependencyKind.GENERATION,
            ),
        ),
        specification_provider="literate-markdown",
        specification_roots=("specs/spec.md", "specs/contracts.json"),
        authoring_inputs=(
            selector("specification-to-source-skill", "skills/implement.json"),
            selector("specification-to-source-skill", "skills/plan.json", pinned=True),
        ),
        workflow_definition=selector("workflow", "workflows/host.json"),
        routing_policy=selector("routing-policy", "routing/default.json"),
        flavor_slots=(
            FlavorSlot(
                "language",
                FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                FlavorCardinality.EXACTLY_ONE,
                "invoice-api",
            ),
            FlavorSlot(
                "os",
                FlavorAxis.PLATFORM_OS,
                FlavorCardinality.EXACTLY_ONE,
                "invoice-api",
            ),
        ),
        entrypoints=(Entrypoint("run", "portable-application", "run"),),
        acceptance_contracts=(
            selector("acceptance-contract", "acceptance/execution.json"),
        ),
        source_dependencies=(
            AuthoredRepositorySourceDependency(
                "sqlite",
                "https://github.com/sqlite/sqlite.git",
                RepositoryRevisionSelector(RepositoryRevisionKind.BRANCH, "trunk"),
                DependencyKind.BUILD,
                integration_contract=selector(
                    "integration-contract", "dependencies/sqlite.md"
                ),
            ),
        ),
        assets=(
            ComponentAssetSelector(
                "currency-table",
                "assets/currencies.csv",
                "source/data/currencies.csv",
                "runtime-data",
                "text/csv",
            ),
        ),
    )


class ComponentAuthoringContractTests(unittest.TestCase):
    def test_schema_is_valid_and_authoring_round_trips(self) -> None:
        Draft202012Validator.check_schema(SCHEMA)
        value = authored_component()
        self.assertEqual(ComponentAuthoring.from_dict(value.to_dict()), value)
        schema_catalog().validate(ComponentAuthoring.SCHEMA, value.to_dict())
        schema_catalog().validate(
            ComponentContentSelector.SCHEMA,
            value.authoring_inputs[0].to_dict(),
        )
        schema_catalog().validate(
            ComponentAssetSelector.SCHEMA,
            value.assets[0].to_dict(),
        )

    def test_asset_sources_support_local_shared_and_external_authority(self) -> None:
        local = ComponentAssetSelector(
            "local", "assets/table.csv", "source/data/table.csv", "runtime-data"
        )
        shared = replace(local, asset_id="shared", source="project:///shared/table.csv")
        remote = replace(
            local,
            asset_id="remote",
            source="https://assets.example.test/models/model.bin?version=3",
        )
        self.assertEqual(local.source, "assets/table.csv")
        self.assertTrue(shared.source.startswith("project:///"))
        self.assertTrue(remote.source.startswith("https://"))
        for unsafe in (
            "../secret",
            "/absolute/path",
            "file:///tmp/secret",
            "https://user:token@example.test/asset",
            "project://host/shared/file",
        ):
            with (
                self.subTest(source=unsafe),
                self.assertRaises(ContractValidationError),
            ):
                replace(local, source=unsafe)

    def test_only_explicit_pin_can_place_an_identity_in_authored_selector(self) -> None:
        unpinned = selector("workflow", "workflows/host.json")
        pinned = selector("workflow", "workflows/host.json", pinned=True)
        self.assertIsNone(unpinned.pin)
        self.assertIsNotNone(pinned.pin)

        disguised = unpinned.to_dict()
        disguised["identity"] = canonical_identity({"forged": True}).to_dict()
        with self.assertRaisesRegex(
            ContractValidationError, "unknown fields: identity"
        ):
            ComponentContentSelector.from_dict(disguised)

        missing_explicit_null = unpinned.to_dict()
        del missing_explicit_null["pin"]
        with self.assertRaisesRegex(ContractValidationError, "missing required fields"):
            ComponentContentSelector.from_dict(missing_explicit_null)

    def test_authored_requirements_use_lock_representable_identifiers(self) -> None:
        value = authored_component().to_dict()
        value["requires"][0]["requirement_id"] = "needs pricing"
        value["requires"][0]["capability"] = "pricing api"

        self.assertTrue(
            tuple(official_validator(ComponentAuthoring.SCHEMA).iter_errors(value))
        )
        with self.assertRaisesRegex(
            ContractValidationError, "portable lower-case identifier"
        ):
            ComponentAuthoring.from_dict(value)

    def test_set_like_authoring_fields_require_canonical_semantic_order(self) -> None:
        component = authored_component()
        with self.assertRaisesRegex(ContractValidationError, "profile order"):
            replace(component, profiles=tuple(reversed(component.profiles)))
        with self.assertRaisesRegex(ContractValidationError, "Flavor slot IDs"):
            replace(component, flavor_slots=tuple(reversed(component.flavor_slots)))
        with self.assertRaisesRegex(ContractValidationError, "authoring selectors"):
            replace(
                component,
                authoring_inputs=tuple(reversed(component.authoring_inputs)),
            )

    def test_runtime_and_resolution_evidence_are_not_authoring_fields(self) -> None:
        forbidden = (
            "source_snapshot",
            "parent_runs",
            "selected_flavors",
            "catalog_candidates",
            "generation_prompt",
            "test_receipt",
        )
        for field in forbidden:
            with self.subTest(field=field):
                value = authored_component().to_dict()
                value[field] = canonical_identity({"forbidden": field}).to_dict()
                with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
                    ComponentAuthoring.from_dict(value)

    def test_frozen_welded_v2_component_schema_is_unchanged(self) -> None:
        digest = hashlib.sha256(
            (ROOT / "schemas/v2/components.schema.json").read_bytes()
        ).hexdigest()
        self.assertEqual(
            digest,
            "122c260d25413924cdfa1b42156954aa881a892540c4bda7618643a9917aa7a7",
        )

    def test_schema_and_runtime_reject_the_same_authoring_boundary_attacks(
        self,
    ) -> None:
        validator = official_validator(ComponentAuthoring.SCHEMA)
        mutations = []

        long_name = authored_component().to_dict()
        long_name["display_name"] = "x" * 4097
        mutations.append(long_name)

        unsafe_repository = authored_component().to_dict()
        unsafe_repository["source_dependencies"][0]["repository_url"] = (
            "file:///tmp/sqlite"
        )
        mutations.append(unsafe_repository)

        traversal = authored_component().to_dict()
        traversal["workflow_definition"]["uri"] = "../workflow.json"
        mutations.append(traversal)

        wrong_kind = authored_component().to_dict()
        wrong_kind["routing_policy"]["kind"] = "workflow"
        mutations.append(wrong_kind)

        for value in mutations:
            with self.subTest(value=value):
                self.assertTrue(tuple(validator.iter_errors(value)))
                with self.assertRaises(ContractValidationError):
                    ComponentAuthoring.from_dict(value)


if __name__ == "__main__":
    unittest.main()
