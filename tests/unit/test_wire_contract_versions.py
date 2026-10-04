from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.cli._wire import result_from_wire, result_to_wire
from literate_ai.contracts import (
    GENERATED_SOURCE_CANDIDATE_SCHEMA,
    SOURCE_GENERATION_CHECKPOINT_SCHEMA,
    SOURCE_GENERATION_PROVENANCE_SCHEMA,
    SOURCE_GENERATION_RUN_OUTPUT_SCHEMA,
    ComponentDefinition,
    ComponentGenerationRuntimeObservation,
    ContractValidationError,
    FlavorDefinition,
    GeneratedSourceCandidate,
    ProjectDefinition,
    SourceCacheTarget,
    SourceGenerationCheckpoint,
    SourceGenerationCheckpointStatus,
    SourceGenerationProvenance,
    SourceGenerationRunOutput,
)
from literate_ai.schema_catalog import SchemaCatalogError, verify_schema_catalog
from literate_ai.source_to_specification import (
    V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA,
    V2_COMPONENT_GRAPH_DRAFT_SCHEMA,
    V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA,
    V3_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA,
    RegenerativeAuthority,
    RegenerativeQualificationDecision,
    SourceToSpecificationError,
    WireMigrationError,
    adapt_unreleased_post_v011_qualification_document,
    adapt_unreleased_post_v011_source_to_specification_result,
    migrate_source_to_specification_result_files,
    migrate_v1_source_to_specification_result,
    normalize_component_authority_projection,
    normalize_source_to_specification_result,
)
from literate_ai.version import DISTRIBUTION_VERSION
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.support.fixtures_test_source_generation_boundary_contracts import (
    _candidate,
    _identity,
    _provenance,
)

REPOSITORY = Path(__file__).resolve().parents[2]
V1_SCHEMAS = REPOSITORY / "schemas" / "v1"
V2_SCHEMAS = REPOSITORY / "schemas" / "v2"
HISTORICAL_RESULT = (
    REPOSITORY
    / "tests"
    / "fixtures"
    / "schemas"
    / "v0.1.1"
    / "source-to-specification-result.json"
)


def _v2_schemas() -> SchemaCatalog:
    schemas = SchemaCatalog(V2_SCHEMAS)
    for path in sorted(V1_SCHEMAS.glob("*.schema.json")):
        schemas._collect(json.loads(path.read_text(encoding="utf-8")))
    return schemas


class VersionedWireContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.historical = json.loads(HISTORICAL_RESULT.read_text(encoding="utf-8"))
        self.schemas = _v2_schemas()

    def test_both_packaged_catalogs_have_exact_versioned_indexes(self) -> None:
        self.assertEqual(
            verify_schema_catalog("v1", V1_SCHEMAS),
            {
                "catalog_version": "v1",
                "catalog_id": "urn:literate-ai:schema-catalog:v1",
                "schema_file_count": 17,
                "resource_count": 84,
                "release": None,
            },
        )
        current = verify_schema_catalog("v2", V2_SCHEMAS)
        self.assertEqual(current["catalog_version"], "v2")
        self.assertEqual(current["catalog_id"], "urn:literate-ai:schema-catalog:v2")
        self.assertEqual(current["release"], DISTRIBUTION_VERSION)
        self.assertGreaterEqual(current["schema_file_count"], 1)
        self.assertGreaterEqual(current["resource_count"], current["schema_file_count"])

    def test_source_generation_boundary_versions_are_exact_and_cataloged(self) -> None:
        candidate = _candidate()
        provenance = _provenance(candidate)
        output = SourceGenerationRunOutput(
            candidate,
            candidate.identity,
            provenance,
            provenance.identity,
            ComponentGenerationRuntimeObservation(1, 200, 300, None),
        )
        checkpoint = SourceGenerationCheckpoint(
            "source-generation:wire-version",
            candidate.source_generation_request_identity,
            SourceGenerationCheckpointStatus.CANDIDATE_READY,
            _identity("wire-version-events"),
            ("plan", "generate"),
            provenance.model_stage_output_identities,
            candidate.identity,
            provenance.identity,
        )
        contracts = (
            (
                GENERATED_SOURCE_CANDIDATE_SCHEMA,
                candidate,
                GeneratedSourceCandidate.from_dict,
            ),
            (
                SOURCE_GENERATION_PROVENANCE_SCHEMA,
                provenance,
                SourceGenerationProvenance.from_dict,
            ),
            (
                SOURCE_GENERATION_RUN_OUTPUT_SCHEMA,
                output,
                SourceGenerationRunOutput.from_dict,
            ),
            (
                SOURCE_GENERATION_CHECKPOINT_SCHEMA,
                checkpoint,
                SourceGenerationCheckpoint.from_dict,
            ),
        )
        for schema_uri, value, parser in contracts:
            with self.subTest(schema=schema_uri):
                document = value.to_dict()
                self.schemas.validate(schema_uri, document)
                self.assertEqual(parser(document), value)

                future = dict(document)
                version, _, name = schema_uri.rpartition(":")
                generation = int(version.rsplit("v", 1)[1])
                future["schema"] = f"urn:literate-ai:schema:v{generation + 1}:{name}"
                with self.assertRaises(ContractValidationError):
                    parser(future)

    def test_v2_compatibility_matrix_is_fail_closed_catalog_authority(self) -> None:
        mutations = (
            lambda matrix: matrix.__setitem__("unknown_versions", "accept"),
            lambda matrix: matrix["write_contracts"].clear(),
            lambda matrix: matrix["write_contracts"].pop(),
            lambda matrix: matrix["write_contracts"].__setitem__(
                0, "urn:literate-ai:schema:v1:component-definition"
            ),
            lambda matrix: matrix["read_paths"][0].__setitem__(
                "adapter", "arbitrary-string-is-not-an-adapter"
            ),
            lambda matrix: matrix["read_paths"][0].__setitem__(
                "status", "published-and-probably-fine"
            ),
            lambda matrix: matrix["read_paths"].pop(),
            lambda matrix: matrix["incompatible_inputs"][0].__setitem__(
                "migration", "invent-missing-authority"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate.__code__.co_firstlineno):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "v2"
                    shutil.copytree(V2_SCHEMAS, root)
                    path = root / "compatibility.json"
                    matrix = json.loads(path.read_text(encoding="utf-8"))
                    mutate(matrix)
                    path.write_text(json.dumps(matrix), encoding="utf-8")
                    with self.assertRaises(SchemaCatalogError) as raised:
                        verify_schema_catalog("v2", root)
                self.assertEqual(
                    raised.exception.code,
                    "schema.catalog_compatibility_invalid",
                )

    def test_v2_compatibility_matrix_admits_entrypoint_evidence_contracts(self) -> None:
        matrix = json.loads(
            (V2_SCHEMAS / "compatibility.json").read_text(encoding="utf-8")
        )
        self.assertIn(
            "urn:literate-ai:schema:v2:standard-entrypoint-generated-test-evidence",
            matrix["write_contracts"],
        )
        self.assertIn(
            "urn:literate-ai:schema:v2:standard-entrypoint-execution-evidence",
            matrix["write_contracts"],
        )
        self.assertEqual(
            verify_schema_catalog("v2", V2_SCHEMAS)["release"], DISTRIBUTION_VERSION
        )

    def test_v2_catalog_rejects_invalid_schema_syntax_and_unresolved_refs(self) -> None:
        mutations = (
            (
                "component-authority-projection.schema.json",
                lambda document: document.__setitem__("type", "not-a-schema-type"),
                "schema.catalog_file_invalid",
            ),
            (
                "component-authority-projection.schema.json",
                lambda document: document["$defs"]["projection"]["properties"][
                    "component_revision_identity"
                ].__setitem__("$ref", "urn:literate-ai:schema:v999:missing"),
                "schema.catalog_reference_unresolved",
            ),
        )
        for filename, mutate, code in mutations:
            with self.subTest(filename=filename, code=code):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "v2"
                    shutil.copytree(V2_SCHEMAS, root)
                    path = root / filename
                    document = json.loads(path.read_bytes())
                    mutate(document)
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(SchemaCatalogError) as raised:
                        verify_schema_catalog("v2", root)
                self.assertEqual(raised.exception.code, code)

    def test_v2_authority_projection_is_exact_and_has_no_lossy_legacy_adapter(
        self,
    ) -> None:
        identity = "sha256:" + "a" * 64
        projection = {
            "schema": V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA,
            "component_coordinate": "component://sample/legacy-service",
            "component_revision_identity": identity,
            "state": "derived-source-retained",
            "transition": "human-acceptance",
            "source_snapshot_identity": identity,
            "specification_set_identity": identity,
            "target_lock_identity": None,
            "generation_closure": None,
            "verifier_identity": None,
            "policy_identity": None,
            "evidence_identities": [identity],
            "provenance_reference_identity": identity,
            "prior_projection_identity": identity,
        }
        self.schemas.validate(V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA, projection)
        self.assertEqual(
            normalize_component_authority_projection(projection), projection
        )

        invalid_transition = dict(projection, transition="regenerative-qualification")
        with self.assertRaises(WireMigrationError) as raised:
            normalize_component_authority_projection(invalid_transition)
        self.assertEqual(raised.exception.code, "wire.authority_projection_invalid")
        for legacy in (
            {"schema": "literate-ai/accepted-specification-set@1"},
            {"schema": ("urn:literate-ai:schema:v3:component-authority-projection")},
        ):
            with self.subTest(schema=legacy["schema"]):
                with self.assertRaises(WireMigrationError) as raised:
                    normalize_component_authority_projection(legacy)
                self.assertEqual(
                    raised.exception.code,
                    "wire.authority_projection_version_unsupported",
                )

        compatibility = json.loads(
            (V2_SCHEMAS / "compatibility.json").read_text(encoding="utf-8")
        )
        self.assertIn(
            V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA,
            compatibility["write_contracts"],
        )
        incompatible = {
            item["input"]: item for item in compatibility["incompatible_inputs"]
        }
        self.assertEqual(
            incompatible["literate-ai/accepted-specification-set@1"]["migration"],
            "fail-closed-until-AUTH-100-evidence-is-supplied",
        )

    def test_v2_authority_projection_state_shapes_use_null_not_placeholders(
        self,
    ) -> None:
        identity = "sha256:" + "b" * 64
        closure = {
            "flavor_set_identity": identity,
            "skill_set_identity": identity,
            "workflow_identity": identity,
            "routing_policy_identity": identity,
            "promotion_input_audit_identity": identity,
            "promotion_tree_identity": identity,
            "qualification_evidence_identity": identity,
        }

        def projection(
            state: str,
            transition: str,
            *,
            component_revision: str | None,
            specification: str | None,
            target_lock: str | None,
            generation_closure: dict[str, str] | None,
            prior: str | None,
        ) -> dict[str, object]:
            return {
                "schema": V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA,
                "component_coordinate": "component://sample/authority-ladder",
                "component_revision_identity": component_revision,
                "state": state,
                "transition": transition,
                "source_snapshot_identity": identity,
                "specification_set_identity": specification,
                "target_lock_identity": target_lock,
                "generation_closure": generation_closure,
                "verifier_identity": (
                    identity if state == "regeneratively-qualified-fungible" else None
                ),
                "policy_identity": (
                    identity if state == "regeneratively-qualified-fungible" else None
                ),
                "evidence_identities": [identity],
                "provenance_reference_identity": identity,
                "prior_projection_identity": prior,
            }

        valid = {
            "source-authoritative": projection(
                "source-authoritative",
                "source-inventory",
                component_revision=None,
                specification=None,
                target_lock=None,
                generation_closure=None,
                prior=None,
            ),
            "spec-assisted": projection(
                "spec-assisted",
                "spec-derivation",
                component_revision=identity,
                specification=identity,
                target_lock=None,
                generation_closure=None,
                prior=identity,
            ),
            "derived-source-retained": projection(
                "derived-source-retained",
                "human-acceptance",
                component_revision=identity,
                specification=identity,
                target_lock=None,
                generation_closure=None,
                prior=identity,
            ),
            "regeneratively-qualified-fungible": projection(
                "regeneratively-qualified-fungible",
                "regenerative-qualification",
                component_revision=identity,
                specification=identity,
                target_lock=identity,
                generation_closure=closure,
                prior=identity,
            ),
        }
        for state, document in valid.items():
            with self.subTest(state=state):
                self.schemas.validate(
                    V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA, document
                )
                self.assertEqual(
                    normalize_component_authority_projection(document), document
                )

        invalid = {
            "source-with-component-revision": dict(
                valid["source-authoritative"],
                component_revision_identity=identity,
            ),
            "source-with-target-lock": dict(
                valid["source-authoritative"], target_lock_identity=identity
            ),
            "source-with-generation-closure": dict(
                valid["source-authoritative"], generation_closure=closure
            ),
            "spec-assisted-without-component-revision": dict(
                valid["spec-assisted"], component_revision_identity=None
            ),
            "derived-without-component-revision": dict(
                valid["derived-source-retained"], component_revision_identity=None
            ),
            "qualified-without-target-lock": dict(
                valid["regeneratively-qualified-fungible"],
                target_lock_identity=None,
            ),
            "qualified-without-generation-closure": dict(
                valid["regeneratively-qualified-fungible"],
                generation_closure=None,
            ),
        }
        for case, document in invalid.items():
            with self.subTest(case=case):
                with self.assertRaises(WireMigrationError) as raised:
                    normalize_component_authority_projection(document)
                self.assertEqual(
                    raised.exception.code, "wire.authority_projection_invalid"
                )

    def test_component_and_flavor_writers_emit_v2_and_read_known_v1_envelopes(
        self,
    ) -> None:
        reference_schema = "urn:literate-ai:schema:v1:content-reference"
        current_component = {
            "schema": ComponentDefinition.SCHEMA,
            "coordinate": {"namespace": "tests", "name": "wire-component"},
            "version": "1.0.0",
            "display_name": "Wire Component",
            "description": "Versioned contract fixture",
            "profiles": [],
            "sample": False,
            "provides": [],
            "requires": [],
            "specification_provider": "literate-markdown",
            "specification_roots": ["component.md"],
            "authoring_inputs": [],
            "workflow_definition": {
                "schema": reference_schema,
                "kind": "workflow",
                "uri": "workflows/test.json",
                "identity": _identity("workflow").to_dict(),
            },
            "routing_policy": {
                "schema": reference_schema,
                "kind": "routing-policy",
                "uri": "routing/test.json",
                "identity": _identity("routing").to_dict(),
            },
            "flavor_slots": [],
            "entrypoints": [],
            "acceptance_contracts": [],
        }
        documents = (
            (
                ComponentDefinition,
                current_component,
                "urn:literate-ai:schema:v2:component-definition",
                "urn:literate-ai:schema:v1:component-definition",
            ),
            (
                FlavorDefinition,
                parse_flavor_markdown(
                    (REPOSITORY / "flavors" / "build-bazel" / "flavor.md").read_bytes(),
                    source=(
                        REPOSITORY / "flavors" / "build-bazel" / "flavor.md"
                    ).as_posix(),
                )
                .resolve(
                    lambda uri: (
                        (REPOSITORY / "flavors" / "build-bazel")
                        .joinpath(*Path(uri).parts)
                        .read_bytes()
                    )
                )
                .to_dict(),
                "urn:literate-ai:schema:v2:flavor-definition",
                "urn:literate-ai:schema:v1:flavor-definition",
            ),
        )
        for contract, current, current_schema, legacy_schema in documents:
            with self.subTest(contract=contract.__name__):
                self.assertEqual(current["schema"], current_schema)
                self.schemas.validate(current_schema, current)
                self.assertEqual(contract.from_dict(current).to_dict(), current)
                legacy = dict(current, schema=legacy_schema)
                self.assertEqual(
                    contract.from_dict(legacy).to_dict()["schema"], current_schema
                )
                with self.assertRaises(ValueError):
                    contract.from_dict(
                        dict(current, schema=current_schema.replace(":v2:", ":v3:"))
                    )

    def test_changed_project_and_cache_wires_have_explicit_v1_readers(self) -> None:
        current = json.loads(
            (REPOSITORY / "literate.project.json").read_text(encoding="utf-8")
        )
        current.pop("source_intelligence")
        current.pop("institutional_channels", None)
        current.pop("mcp_roots", None)
        current.pop("repository_policy", None)
        current.pop("component_flavor_selectors", None)
        current["schema"] = "urn:literate-ai:schema:v1:project-definition"
        current["source_index"] = {
            "schema": "urn:literate-ai:schema:v1:project-source-index-requirement",
            "provider_id": "codegraph-cli",
            "command": "codegraph",
            "minimum_version": "1.1.1",
            "index_path": ".codegraph/codegraph.db",
        }
        driver = current["lifecycle_driver"]
        driver["schema"] = "urn:literate-ai:schema:v1:project-lifecycle-driver"
        driver["phases"] = [
            "index-source" if phase == "derive-source-intelligence" else phase
            for phase in driver["phases"]
        ]
        self.schemas.validate("urn:literate-ai:schema:v1:project-definition", current)
        migrated = ProjectDefinition.from_dict(current).to_dict()
        self.assertEqual(
            migrated["schema"], "urn:literate-ai:schema:v2:project-definition"
        )
        self.assertIn("source_intelligence", migrated)
        self.assertNotIn("source_index", migrated)

        target = SourceCacheTarget.from_dict(
            {
                "schema": "urn:literate-ai:schema:v1:source-cache-target",
                "target_id": "local",
                "format": "filesystem-v1",
                "root_kind": "project-relative",
                "root_reference": ".literate-ai/source-cache",
            }
        )
        self.assertEqual(target.SCHEMA, "urn:literate-ai:schema:v2:source-cache-target")
        self.assertEqual(target.format, "filesystem-v1")

    def test_published_v1_result_migrates_without_inventing_a_graph(self) -> None:
        migrated = migrate_v1_source_to_specification_result(self.historical)
        self.assertEqual(migrated["schema"], V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA)
        self.assertIsNone(migrated["component_graph_draft"])
        migration_items = [
            item
            for item in migrated["uncertainty"]["items"]
            if item["uncertainty_id"].startswith("migration:missing-component-graph:")
        ]
        self.assertEqual(len(migration_items), 1)
        self.assertTrue(migration_items[0]["blocking"])
        self.assertEqual(migration_items[0]["kind"], "missing-evidence")
        self.schemas.validate(V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA, migrated)
        self.assertEqual(normalize_source_to_specification_result(migrated), migrated)
        current = result_to_wire(result_from_wire(self.historical))
        self.assertEqual(current["schema"], V3_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA)
        self.schemas.validate(V3_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA, current)

    def test_known_unreleased_result_shape_has_one_narrow_adapter(self) -> None:
        unreleased = dict(self.historical)
        unreleased["component_graph_draft"] = {
            "source_snapshot_id": "snapshot-v011",
            "root_coordinate": "sample/deterministic-greeting",
            "nodes": [
                {
                    "coordinate": "sample/deterministic-greeting",
                    "title": "Deterministic greeting",
                    "provided_capabilities": ["deterministic-greeting"],
                    "source_paths": ["src/main.py"],
                    "observation_ids": ["observation-v011"],
                    "evidence_ids": ["evidence-v011"],
                }
            ],
            "edges": [],
        }
        adapted = adapt_unreleased_post_v011_source_to_specification_result(unreleased)
        self.assertEqual(
            adapted["component_graph_draft"]["schema"],
            V2_COMPONENT_GRAPH_DRAFT_SCHEMA,
        )
        self.assertFalse(
            any(
                item["uncertainty_id"].startswith("migration:missing-component-graph:")
                for item in adapted["uncertainty"]["items"]
            )
        )
        self.schemas.validate(V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA, adapted)
        self.assertEqual(result_to_wire(result_from_wire(unreleased)), adapted)

    def test_unknown_and_ambiguous_result_versions_fail_closed(self) -> None:
        future = {"schema": "urn:literate-ai:schema:v4:source-to-specification-result"}
        with self.assertRaises(WireMigrationError) as raised:
            normalize_source_to_specification_result(future)
        self.assertEqual(raised.exception.code, "wire.version_unsupported")
        ambiguous = dict(self.historical, unexpected=True)
        with self.assertRaises(WireMigrationError) as raised:
            normalize_source_to_specification_result(ambiguous)
        self.assertEqual(raised.exception.code, "wire.result_shape_ambiguous")

    def test_multi_file_migration_restores_exact_bytes_after_commit_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(self.historical, separators=(",", ":")))
            second.write_text(json.dumps(self.historical, indent=4) + "\n\n")
            before = {path: path.read_bytes() for path in (first, second)}
            calls = 0

            def fail_second(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second replacement failure")
                os.replace(source, target)

            with patch(
                "literate_ai.source_to_specification.wire_migrations._replace_for_commit",
                side_effect=fail_second,
            ):
                with self.assertRaises(WireMigrationError) as raised:
                    migrate_source_to_specification_result_files([first, second])
            self.assertEqual(raised.exception.code, "wire.file_migration_commit_failed")
            self.assertEqual(
                {path: path.read_bytes() for path in (first, second)}, before
            )

    def test_nested_invalid_v1_result_never_reaches_staging_or_commit(self) -> None:
        malformed = dict(self.historical, observations="not-an-array")
        with self.assertRaises(WireMigrationError) as raised:
            migrate_v1_source_to_specification_result(malformed)
        self.assertEqual(raised.exception.code, "wire.v1_result_invalid")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.json"
            path.write_text(json.dumps(malformed))
            before = path.read_bytes()
            with patch(
                "literate_ai.source_to_specification.wire_migrations._replace_for_commit"
            ) as replacement:
                with self.assertRaises(WireMigrationError) as raised:
                    migrate_source_to_specification_result_files([path])
            self.assertEqual(raised.exception.code, "wire.v1_result_invalid")
            replacement.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_unreleased_qualification_uri_is_rebound_to_v2_only_explicitly(
        self,
    ) -> None:
        document = {
            "schema": "urn:literate-ai:schema:v1:regenerative-qualification-policy",
            "policy_id": "strict",
            "minimum_clean_runs": 2,
            "required_target_profile_ids": ["sha256:" + "a" * 64],
            "required_surface_ids": ["cli"],
            "allow_skipped_tests": False,
        }
        migrated = adapt_unreleased_post_v011_qualification_document(document)
        schema = "urn:literate-ai:schema:v2:regenerative-qualification-policy"
        self.assertEqual(migrated["schema"], schema)
        self.schemas.validate(schema, migrated)
        with self.assertRaises(WireMigrationError) as raised:
            adapt_unreleased_post_v011_qualification_document(
                dict(document, unexpected=True)
            )
        self.assertEqual(raised.exception.code, "wire.qualification_record_invalid")
        invalid_semantics = dict(document, minimum_clean_runs="garbage")
        with self.assertRaises(WireMigrationError) as raised:
            adapt_unreleased_post_v011_qualification_document(invalid_semantics)
        self.assertEqual(raised.exception.code, "wire.qualification_record_invalid")
        with self.assertRaises(WireMigrationError) as raised:
            adapt_unreleased_post_v011_qualification_document(
                {"schema": "urn:literate-ai:schema:v3:qualification"}
            )
        self.assertEqual(
            raised.exception.code, "wire.qualification_version_unsupported"
        )

    def test_historical_specification_claim_cannot_become_current_authority(
        self,
    ) -> None:
        """Regression: adapting and parsing a v1 claim must not authorize it."""

        identities = tuple("sha256:" + character * 64 for character in "abcde")
        historical = {
            "schema": ("urn:literate-ai:schema:v1:regenerative-qualification-decision"),
            "source_snapshot_id": identities[0],
            "specification_set_id": identities[1],
            "policy_id": "historical-claim",
            "policy_identity": identities[2],
            "evidence_ids": [identities[3], identities[4]],
            "authority": "specification",
            "blockers": [],
        }

        adapted = adapt_unreleased_post_v011_qualification_document(historical)
        decision = RegenerativeQualificationDecision.from_dict(adapted)

        self.assertEqual(
            adapted["schema"],
            "urn:literate-ai:schema:v2:regenerative-qualification-decision",
        )
        self.assertNotIn("authority", adapted)
        self.assertEqual(adapted["claimed_authority"], "specification")
        self.assertEqual(adapted["effective_authority"], "source-baseline")
        self.assertIs(decision.claimed_authority, RegenerativeAuthority.SPECIFICATION)
        self.assertTrue(decision.claimed_qualified)
        self.assertFalse(decision.qualified)
        self.assertIs(
            decision.effective_authority, RegenerativeAuthority.SOURCE_BASELINE
        )
        with self.assertRaises(AttributeError):
            _ = decision.authority
        self.assertEqual(decision.to_dict(), adapted)

    def test_qualification_claim_versions_are_explicit_and_fail_closed(self) -> None:
        identity = "sha256:" + "a" * 64
        future = {
            "schema": ("urn:literate-ai:schema:v3:regenerative-qualification-decision"),
            "source_snapshot_id": identity,
            "specification_set_id": identity,
            "policy_id": "future",
            "policy_identity": identity,
            "evidence_ids": [identity, "sha256:" + "b" * 64],
            "claimed_authority": "specification",
            "effective_authority": "source-baseline",
            "blockers": [],
        }
        with self.assertRaises(SourceToSpecificationError) as raised:
            RegenerativeQualificationDecision.from_dict(future)
        self.assertEqual(raised.exception.code, "qualification.schema_unsupported")

        ambiguous = dict(future)
        ambiguous.pop("schema")
        with self.assertRaises(WireMigrationError) as raised:
            adapt_unreleased_post_v011_qualification_document(ambiguous)
        self.assertEqual(
            raised.exception.code, "wire.qualification_version_unsupported"
        )


if __name__ == "__main__":
    unittest.main()
