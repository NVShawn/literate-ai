"""Machine-readable schema-catalog and current public wire conformance tests."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.application import (
    ComponentSourceEvidenceReadiness,
    FinalCutoverManifest,
    GenerationContextBinding,
    GenerationEvent,
    GenerationProvenance,
    ObservationDecision,
    ObservationDisposition,
    RollbackRehearsal,
    SeamCutover,
    SourceEvidenceReadiness,
)
from literate_ai.artifacts import BundleKind, BundleManifest
from literate_ai.contracts import (
    LEGACY_CYCLONEDX_BOM_BINDING_SCHEMA,
    LEGACY_CYCLONEDX_MANAGED_GRAPH_SCHEMA,
    AcceptedSourceDerivation,
    ComponentCoordinate,
    ComponentRevisionRef,
    ContentIdentity,
    ContentReference,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    HashAlgorithm,
    KnownTestFailureAnnotation,
    KnownTestFailureCause,
    KnownTestFailureOutcome,
    KnownTestFailurePolicy,
    KnownTestFailureReport,
    KnownTestFailureSummary,
    LifecycleSeam,
    ManagedComponentKind,
    ProjectDefinition,
    ProjectInitializationBaseline,
    ProjectInitializationBaselineFile,
    ProjectInitializationOrigin,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    ResolvedSpecificationToSourceSkill,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    SpecificationToSourceSkill,
    TargetProfile,
    TestOutcomeStatus,
    ToolchainConstraint,
    VersionedContentRef,
    canonical_identity,
    component_bom_ref,
)
from literate_ai.intelligence import IndexBinding
from literate_ai.models import (
    DataEgress,
    Locality,
    ModelEndpoint,
    ModelGroup,
    ModelRouter,
    StageModelPolicy,
)
from literate_ai.publication import (
    ImportAuthorization,
    ImportPolicy,
    ImportRequest,
    PublicationManifest,
    PublicationPolicy,
    PublicationRequest,
    TransferReceipt,
)
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildAuthorization,
    BuildRequest,
    BuildRequestDeclaration,
    FindingSeverity,
    ObservationExecutionAuthorization,
    ObservationRequest,
    OriginAttestation,
    RuleBasedSourceScanner,
    SecurityClassification,
    SecurityFinding,
    SecurityPolicy,
    SecurityProfile,
    SecurityScanReport,
    SourceModule,
    baseline_python_rules,
)
from literate_ai.settings import SettingScope, SettingsDocument
from literate_ai.source_to_specification import (
    CleanRegenerationEvidence,
    LocalHmacQualificationAttestor,
    LocalQualificationCase,
    LocalQualificationProfile,
    RegenerativeQualificationPolicy,
    RunMode,
    SourceToSpecificationRequest,
    canonical_value,
    qualify_regenerative_specification,
)
from literate_ai.storage import BlobRef
from literate_ai.workflows import StageDefinition, WorkflowDefinition

ROOT = Path(__file__).parents[2] / "schemas" / "v1"
V2_ROOT = Path(__file__).parents[2] / "schemas" / "v2"
DIVERGENCE_EVIDENCE = (
    Path(__file__).parents[2]
    / "tests"
    / "fixtures"
    / "schemas"
    / "unreleased-v1-drift.json"
)
DIGEST = "sha256:" + "a" * 64


def deferred_schema_change(path: str) -> dict:
    evidence = json.loads(DIVERGENCE_EVIDENCE.read_text(encoding="utf-8"))
    matches = [item for item in evidence["files"] if item["path"] == path]
    if len(matches) != 1:
        raise AssertionError(f"missing deferred schema evidence for {path}")
    return matches[0]


class SchemaCatalog:
    def __init__(self, root: Path = ROOT) -> None:
        self.catalog = json.loads((root / "index.json").read_text())
        self.documents = {
            path.name: json.loads(path.read_text())
            for path in sorted(root.glob("*.schema.json"))
        }
        self.resources: dict[str, dict] = {}
        for document in self.documents.values():
            self._collect(document)
        self.catalog_resources = frozenset(self.resources)
        if root == ROOT:
            for path in sorted(V2_ROOT.glob("*.schema.json")):
                self._collect(json.loads(path.read_text()))

    def _collect(self, value) -> None:
        if isinstance(value, dict):
            if "$id" in value:
                if value["$id"] in self.resources:
                    raise AssertionError(f"duplicate schema ID {value['$id']}")
                self.resources[value["$id"]] = value
            for child in value.values():
                self._collect(child)
        elif isinstance(value, list):
            for child in value:
                self._collect(child)

    def resolve(self, reference: str, base: str | None = None):
        if reference.startswith("#"):
            if base is None:
                raise AssertionError(f"relative reference without base: {reference}")
            resource_id, fragment = base, reference[1:]
        else:
            resource_id, marker, fragment = reference.partition("#")
            fragment = fragment if marker else ""
        try:
            value = self.resources[resource_id]
        except KeyError as error:
            raise AssertionError(f"unknown schema resource {resource_id}") from error
        if fragment:
            if not fragment.startswith("/"):
                raise AssertionError(f"unsupported non-pointer fragment {fragment}")
            for token in fragment[1:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                value = value[token]
        return value, resource_id

    def validate(self, resource_id: str, value) -> None:
        schema, base = self.resolve(resource_id)
        self._validate(schema, value, "$", base)

    def _validate(self, schema, value, path: str, base: str) -> None:
        if "$id" in schema:
            base = schema["$id"]
        if "$ref" in schema:
            target, target_base = self.resolve(schema["$ref"], base)
            self._validate(target, value, path, target_base)
            return
        if "oneOf" in schema:
            matches = 0
            for candidate in schema["oneOf"]:
                try:
                    self._validate(candidate, value, path, base)
                except AssertionError:
                    continue
                matches += 1
            if matches != 1:
                raise AssertionError(
                    f"{path}: expected exactly one schema match, got {matches}"
                )
            return
        if "const" in schema and value != schema["const"]:
            raise AssertionError(f"{path}: expected constant {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            raise AssertionError(f"{path}: {value!r} is outside enum")
        expected = schema.get("type")
        if expected is not None:
            allowed = expected if isinstance(expected, list) else [expected]
            if not any(self._is_type(value, item) for item in allowed):
                raise AssertionError(
                    f"{path}: expected {allowed}, got {type(value).__name__}"
                )
        if isinstance(value, dict):
            required = set(schema.get("required", ()))
            missing = required - value.keys()
            if missing:
                raise AssertionError(f"{path}: missing {sorted(missing)}")
            for key, dependencies in schema.get("dependentRequired", {}).items():
                if key in value:
                    missing_dependencies = set(dependencies) - value.keys()
                    if missing_dependencies:
                        raise AssertionError(
                            f"{path}: {key} requires {sorted(missing_dependencies)}"
                        )
            properties = schema.get("properties", {})
            for key, item in value.items():
                if key in properties:
                    self._validate(properties[key], item, f"{path}.{key}", base)
                else:
                    additional = schema.get("additionalProperties", True)
                    if additional is False:
                        raise AssertionError(f"{path}: unknown field {key}")
                    if isinstance(additional, dict):
                        self._validate(additional, item, f"{path}.{key}", base)
            if len(value) < schema.get("minProperties", 0):
                raise AssertionError(f"{path}: too few properties")
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
                "maxItems", 2**31
            ):
                raise AssertionError(f"{path}: invalid item count")
            if schema.get("uniqueItems") and len(
                {json.dumps(item, sort_keys=True) for item in value}
            ) != len(value):
                raise AssertionError(f"{path}: items are not unique")
            if "contains" in schema:
                matches = 0
                for index, item in enumerate(value):
                    try:
                        self._validate(
                            schema["contains"], item, f"{path}[{index}]", base
                        )
                    except AssertionError:
                        continue
                    matches += 1
                if matches < schema.get("minContains", 1) or matches > schema.get(
                    "maxContains", 2**31
                ):
                    raise AssertionError(f"{path}: contains match count is invalid")
            prefix = schema.get("prefixItems", ())
            for index, child in enumerate(prefix):
                if index < len(value):
                    self._validate(child, value[index], f"{path}[{index}]", base)
            items = schema.get("items")
            if isinstance(items, dict):
                for index, item in enumerate(value[len(prefix) :], start=len(prefix)):
                    self._validate(items, item, f"{path}[{index}]", base)
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                raise AssertionError(f"{path}: string is too short")
            if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
                raise AssertionError(f"{path}: string does not match pattern")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < schema.get("minimum", value):
                raise AssertionError(f"{path}: number is too small")
            if value > schema.get("maximum", value):
                raise AssertionError(f"{path}: number is too large")

    @staticmethod
    def _is_type(value, expected: str) -> bool:
        return {
            "null": value is None,
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "string": isinstance(value, str),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }[expected]


def wire(value):
    return json.loads(json.dumps(value, sort_keys=True))


class CatalogStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaCatalog()

    def test_index_is_exact_and_every_resource_is_versioned(self) -> None:
        catalog = self.schemas.catalog
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(
            catalog["dialect"], "https://json-schema.org/draft/2020-12/schema"
        )
        indexed_files = {item["file"] for item in catalog["schemas"]}
        self.assertEqual(indexed_files, set(self.schemas.documents))
        indexed_ids = {
            identifier
            for item in catalog["schemas"]
            for identifier in (item["root_id"], *item["public_ids"])
        }
        self.assertEqual(indexed_ids, set(self.schemas.catalog_resources))
        self.assertTrue(
            all(
                identifier.startswith("urn:literate-ai:schema:v1:")
                for identifier in indexed_ids
            )
        )
        for item in catalog["schemas"]:
            self.assertEqual(
                self.schemas.documents[item["file"]]["$id"], item["root_id"]
            )

    def test_every_reference_resolves_and_record_shapes_are_closed(self) -> None:
        def walk(value, base=None):
            if isinstance(value, dict):
                base = value.get("$id", base)
                if "$ref" in value:
                    self.schemas.resolve(value["$ref"], base)
                if value.get("type") == "object" and "properties" in value:
                    self.assertIn("additionalProperties", value)
                for child in value.values():
                    walk(child, base)
            elif isinstance(value, list):
                for child in value:
                    walk(child, base)

        for document in self.schemas.documents.values():
            self.assertEqual(
                document["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            walk(document)


class CurrentWireObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaCatalog()

    def test_project_initialization_evidence_matches_public_schemas(self) -> None:
        origin = ProjectInitializationOrigin(
            "ssh://git.example.test/operator/literate-ai.git",
            "a" * 40,
            "literate-ai",
            "0.2.0",
        )
        file = ProjectInitializationBaselineFile(
            "SKILL.md", 7, canonical_identity({"bytes": "fixture"})
        )
        baseline = ProjectInitializationBaseline(
            origin.identity, "literate-ai/project-template@1", (file,)
        )
        for contract in (origin, file, baseline):
            self.schemas.validate(contract.SCHEMA, contract.to_dict())
        self.assertEqual(
            ProjectInitializationOrigin.from_dict(origin.to_dict()), origin
        )
        self.assertEqual(
            ProjectInitializationBaselineFile.from_dict(file.to_dict()), file
        )
        self.assertEqual(
            ProjectInitializationBaseline.from_dict(baseline.to_dict()), baseline
        )

    def test_literate_specification_authoring_and_context_objects(self) -> None:
        schemas = SchemaCatalog(V2_ROOT)
        authoring = {
            "name": "Viewer backend",
            "summary": "Server-side scene responsibility",
            "kind": "part",
            "references": ["vfi.components.viewer.protocol"],
            "status": "review",
        }
        schemas.validate(
            "urn:literate-ai:schema:v2:specification-node-authoring", authoring
        )
        node = {
            "schema": "urn:literate-ai:schema:v2:specification-node",
            "id": "vfi.components.viewer.backend",
            "name": "Viewer backend",
            "summary": "Server-side scene responsibility",
            "kind": "part",
            "path": "spec/components/viewer/backend.md",
            "parent": "vfi.components.viewer",
            "references": ["vfi.components.viewer.protocol"],
            "status": "review",
            "content_identity": DIGEST,
        }
        context = {
            "schema": "urn:literate-ai:schema:v2:effective-specification-context",
            "root_id": "vfi.components.viewer.backend",
            "nodes": [node],
            "effective_documents": [
                {
                    "node_id": "vfi.components.viewer.backend",
                    "document_ids": ["vfi.components.viewer.backend"],
                }
            ],
        }
        schemas.validate(node["schema"], node)
        schemas.validate(context["schema"], context)

    def test_model_workflow_intelligence_and_security_objects(self) -> None:
        endpoint = ModelEndpoint(
            "local",
            "test",
            "model",
            "http://127.0.0.1:1",
            Locality.LOCAL,
            ("structured",),
            4096,
        )
        group = ModelGroup("group", "1.0.0", ("local",))
        policy = StageModelPolicy(
            "policy", "generate", "group", data_egress=DataEgress.NONE
        )
        decision = ModelRouter(endpoints=(endpoint,), groups=(group,)).select(policy)
        for resource, value in (
            (endpoint.SCHEMA, endpoint.to_dict()),
            (group.SCHEMA, group.to_dict()),
            (policy.SCHEMA, policy.to_dict()),
            (decision.SCHEMA, decision.to_dict()),
            (
                "urn:literate-ai:schema:v1:workflow-definition",
                asdict(
                    WorkflowDefinition(
                        "workflow:test",
                        "1.0.0",
                        (StageDefinition("generate", "model"),),
                    )
                ),
            ),
        ):
            self.schemas.validate(resource, wire(value))

        blob = BlobRef("b" * 64, 10, media_type="application/json")
        binding = IndexBinding.create(
            source_snapshot_id="snapshot",
            source_tree_id="tree",
            provider_id="provider",
            provider_version="1",
            configuration_id="config",
            engine_index_key="index",
            artifact_ref=blob,
            document_count=1,
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:intelligence-index-binding", binding.to_dict()
        )
        request = BuildRequest(
            DIGEST,
            DIGEST,
            "builder",
            DIGEST,
            "constrained",
            ("compiler",),
            ("artifact",),
        )
        attestation = OriginAttestation(DIGEST, "signer", "root", "signature", True)
        classification = SecurityPolicy(DIGEST).classify(
            effective_revision_digest=DIGEST, attestations=(attestation,), findings=()
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:build-request", wire(asdict(request))
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:security-classification",
            wire(asdict(classification)),
        )

    def test_unflavored_target_profile_is_deferred_from_published_v1(self) -> None:
        profile = TargetProfile(
            "portable-default",
            "1.0.0",
            "explicit",
            ContentIdentity.parse_uri(DIGEST),
            (),
        )
        self.schemas.validate(TargetProfile.SCHEMA, profile.to_dict())
        current_as_legacy = profile.to_dict()
        current_as_legacy["schema"] = "urn:literate-ai:schema:v1:target-profile"
        with self.assertRaisesRegex(AssertionError, "invalid item count"):
            self.schemas.validate(
                "urn:literate-ai:schema:v1:target-profile", current_as_legacy
            )
        historical_profile = dict(current_as_legacy)
        historical_profile["constraints"] = [
            {"axis": "platform.os", "value": "linux", "optional": False}
        ]
        self.schemas.validate(
            "urn:literate-ai:schema:v1:target-profile", historical_profile
        )
        evidence = deferred_schema_change("flavors.schema.json")
        self.assertIn(
            "urn:literate-ai:schema:v1:target-profile", evidence["reused_uris"]
        )
        self.assertIn("changed-cardinality", evidence["change_classes"])

    def test_unreleased_toolchain_contract_is_not_published_as_v1(self) -> None:
        constraint = ToolchainConstraint(
            "python",
            command=("python3", "-I"),
            minimum_version=(3, 11),
            required_version=(3,),
        )
        self.assertEqual(constraint.to_dict()["toolchain"], "python")
        self.schemas.validate(ToolchainConstraint.SCHEMA, constraint.to_dict())
        with self.assertRaisesRegex(AssertionError, "unknown schema resource"):
            self.schemas.resolve("urn:literate-ai:schema:v1:toolchain-constraint")
        evidence = deferred_schema_change("flavors.schema.json")
        self.assertIn(
            "urn:literate-ai:schema:v1:toolchain-constraint",
            evidence["unreleased_only_uris"],
        )

    def test_generation_cache_contracts_match_public_schemas(self) -> None:
        key = SourceDerivationCacheKey(
            recipe_identity=canonical_identity({"recipe": 1}),
            execution_plan_identity=canonical_identity({"execution": 1}),
            coding_cli_tool_binding_identity=canonical_identity({"cli": 1}),
            model_binding=SourceCacheModelBinding("test-provider", "model-selector"),
            request_identity=canonical_identity({"request": 1}),
        )
        composition = canonical_identity({"component-composition": 1})
        root_identity = canonical_identity({"component-revision": 1})
        root_ref = component_bom_ref(root_identity)
        managed_graph = CycloneDxManagedGraph(
            root_ref,
            (
                CycloneDxManagedComponent(
                    root_ref,
                    ManagedComponentKind.ROOT,
                    root_identity,
                    "component://schema/root",
                    "1.0.0",
                    (),
                ),
            ),
            (),
            composition,
        )
        managed_sbom = managed_graph.identity
        source_sbom = canonical_identity({"source-sbom": 1})
        resolved_sbom = canonical_identity({"resolved-sbom": 1})
        source_binding = CycloneDxBomBinding(
            CycloneDxLifecycle.SOURCE,
            source_sbom,
            canonical_identity({"source-graph": 1}),
            managed_sbom,
            composition,
            None,
            "urn:test:root",
            1,
            0,
        )
        resolved_binding = CycloneDxBomBinding(
            CycloneDxLifecycle.RESOLVED,
            resolved_sbom,
            canonical_identity({"resolved-graph": 1}),
            managed_sbom,
            composition,
            source_sbom,
            "urn:test:root",
            1,
            0,
        )
        record = AcceptedSourceDerivation(
            cache_key=key,
            component_lock_identity=composition,
            source_tree_identity=canonical_identity({"source": 1}),
            managed_sbom_graph_identity=managed_sbom,
            source_sbom_identity=source_sbom,
            resolved_sbom_identity=resolved_sbom,
            source_sbom_binding=source_binding,
            resolved_sbom_binding=resolved_binding,
            generated_test_suite_identity=canonical_identity({"generated-tests": 1}),
            build_evidence_identity=canonical_identity({"build": 1}),
            test_evidence_identity=canonical_identity({"tests": 1}),
            acceptance_identity=canonical_identity({"acceptance": 1}),
            provenance_identity=canonical_identity({"provenance": 1}),
        )
        self.schemas.validate(key.model_binding.SCHEMA, key.model_binding.to_dict())
        self.schemas.validate(
            key.accepted_source_lookup.SCHEMA,
            key.accepted_source_lookup.to_dict(),
        )
        self.schemas.validate(key.SCHEMA, key.to_dict())
        self.schemas.validate(managed_graph.SCHEMA, managed_graph.to_dict())
        self.schemas.validate(record.SCHEMA, record.to_dict())
        legacy_graph = managed_graph.to_dict()
        legacy_graph["schema"] = LEGACY_CYCLONEDX_MANAGED_GRAPH_SCHEMA
        legacy_graph["composition_identity"] = legacy_graph.pop(
            "resolved_graph_identity"
        )
        self.schemas.validate(LEGACY_CYCLONEDX_MANAGED_GRAPH_SCHEMA, legacy_graph)
        legacy_binding = source_binding.to_dict()
        legacy_binding["schema"] = LEGACY_CYCLONEDX_BOM_BINDING_SCHEMA
        legacy_binding["composition_identity"] = legacy_binding.pop(
            "resolved_graph_identity"
        )
        self.schemas.validate(LEGACY_CYCLONEDX_BOM_BINDING_SCHEMA, legacy_binding)

    def test_bazel_build_axis_is_explicitly_deferred_from_published_v1(self) -> None:
        root = ROOT.parents[1] / "flavors" / "build-bazel"
        path = root / "flavor.md"
        value = (
            parse_flavor_markdown(path.read_bytes(), source=path.as_posix())
            .resolve(lambda uri: root.joinpath(*Path(uri).parts).read_bytes())
            .to_dict()
        )
        self.schemas.validate("urn:literate-ai:schema:v2:flavor-definition", value)
        legacy_value = dict(value)
        legacy_value["schema"] = "urn:literate-ai:schema:v1:flavor-definition"
        with self.assertRaisesRegex(AssertionError, "outside enum"):
            self.schemas.validate(
                "urn:literate-ai:schema:v1:flavor-definition", legacy_value
            )
        self.assertEqual(value["primary_axis"], "build.system")
        self.assertEqual(value["supported_targets"], ["bazel"])
        evidence = deferred_schema_change("flavors.schema.json")
        self.assertIn(
            "urn:literate-ai:schema:v1:flavor-definition", evidence["reused_uris"]
        )
        self.assertIn("added-axis-and-slot-semantics", evidence["change_classes"])

    def test_project_and_specification_to_source_skills_match_public_schemas(
        self,
    ) -> None:
        repo_root = ROOT.parents[1]
        project_value = json.loads(
            (repo_root / "literate.project.json").read_text(encoding="utf-8")
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v2:project-definition", project_value
        )
        project = ProjectDefinition.from_dict(project_value)
        self.assertEqual(ProjectDefinition.from_dict(project.to_dict()), project)
        self.schemas.validate(
            "urn:literate-ai:schema:v1:project-source-intelligence-policy",
            project.source_intelligence.to_dict(),
        )
        minimal_project = dict(project_value)
        for field in (
            "component_roots",
            "flavor_roots",
            "skill_roots",
            "workflow_roots",
            "routing_roots",
        ):
            minimal_project[field] = []
        self.schemas.validate(
            "urn:literate-ai:schema:v2:project-definition", minimal_project
        )
        self.assertEqual(
            ProjectDefinition.from_dict(minimal_project).component_roots, ()
        )
        missing_documentation = dict(minimal_project, documentation_roots=[])
        with self.assertRaises(AssertionError):
            self.schemas.validate(
                "urn:literate-ai:schema:v2:project-definition",
                missing_documentation,
            )
        non_root_skill = dict(minimal_project, agent_skill="docs/SKILL.md")
        with self.assertRaisesRegex(AssertionError, "expected constant"):
            self.schemas.validate(
                "urn:literate-ai:schema:v2:project-definition", non_root_skill
            )

        legacy_project = dict(project_value)
        legacy_project.pop("source_intelligence")
        # v2-only optional envelopes; v1 additionalProperties is false.
        legacy_project.pop("institutional_channels", None)
        legacy_project.pop("mcp_roots", None)
        legacy_project.pop("repository_policy", None)
        legacy_project["schema"] = "urn:literate-ai:schema:v1:project-definition"
        # v1 required a named source_index. Provider `none` exists only on the
        # v2 source_intelligence policy and cannot be projected onto that shape.
        legacy_project["source_index"] = {
            "schema": "urn:literate-ai:schema:v1:project-source-index-requirement",
            "provider_id": "codegraph-cli",
            "command": "codegraph",
            "minimum_version": "1.1.1",
            "index_path": ".codegraph/codegraph.db",
        }
        legacy_project["lifecycle_driver"] = {
            **legacy_project["lifecycle_driver"],
            "schema": "urn:literate-ai:schema:v1:project-lifecycle-driver",
            "phases": [
                "index-source" if phase == "derive-source-intelligence" else phase
                for phase in legacy_project["lifecycle_driver"]["phases"]
            ],
        }
        del legacy_project["test_receipt"]
        legacy_project.pop("test_receipt_policy", None)
        self.schemas.validate(
            "urn:literate-ai:schema:v1:project-definition", legacy_project
        )
        self.assertIsNone(ProjectDefinition.from_dict(legacy_project).test_receipt)

        policy_without_path = dict(project_value)
        del policy_without_path["test_receipt"]
        with self.assertRaisesRegex(AssertionError, "requires"):
            self.schemas.validate(
                "urn:literate-ai:schema:v2:project-definition",
                policy_without_path,
            )

        manifests = sorted(
            (repo_root / "skills" / "specification-to-source").rglob("SKILL.md")
        )
        self.assertTrue(manifests)
        for path in manifests:
            content = path.read_bytes()
            reference = ContentReference(
                "specification-to-source-skill",
                path.relative_to(repo_root).as_posix(),
                ContentIdentity(
                    HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()
                ),
            )
            skill = ResolvedSpecificationToSourceSkill.from_reference(
                reference, content, source="schema test"
            )
            manifest = skill.manifest
            self.schemas.validate(
                "urn:literate-ai:schema:v1:specification-to-source-skill",
                manifest.to_dict(),
            )
            self.assertEqual(
                SpecificationToSourceSkill.from_dict(manifest.to_dict()), manifest
            )
            for dependency in skill.dependencies:
                self.schemas.validate(
                    "urn:literate-ai:schema:v1:skill-reference",
                    dependency.to_dict(),
                )

    def test_project_test_receipt_matches_public_wire_schema(self) -> None:
        lifecycle_request_identity = canonical_identity({"request": "rebuild"})
        lifecycle_command_identity = canonical_identity({"command": "driver"})
        source_cache_decision_identity = canonical_identity({"cache": "decision"})
        source_cache_lifecycle_identity = canonical_identity({"cache": "lifecycle"})
        receipt = ProjectTestReceipt(
            project_id="schema-project",
            project_revision_identity=canonical_identity({"project": "authority"}),
            subject_identity=canonical_identity({"subject": "closure"}),
            suite=VersionedContentRef(
                "test-suite",
                "portable-e2e",
                "1.0.0",
                canonical_identity({"suite": "portable-e2e"}),
            ),
            outcome="passed",
            summary=ProjectTestSummary(2, 2, 0, 0),
            result_identity=canonical_identity({"result": "passed"}),
            evidence=(
                ProjectTestEvidence(
                    "build-result", canonical_identity({"build": "passed"})
                ),
                ProjectTestEvidence("lifecycle-command", lifecycle_command_identity),
                ProjectTestEvidence("lifecycle-request", lifecycle_request_identity),
                ProjectTestEvidence(
                    "source-cache-decision", source_cache_decision_identity
                ),
                ProjectTestEvidence(
                    "source-cache-lifecycle", source_cache_lifecycle_identity
                ),
            ),
        )

        document = receipt.to_dict()
        self.schemas.validate(
            "urn:literate-ai:schema:v1:project-test-receipt", document
        )
        self.schemas.validate("urn:literate-ai:schema:v1:testing-contracts", document)
        self.assertEqual(ProjectTestReceipt.from_dict(document), receipt)

        provisional = ProjectTestReceiptProvisional(
            lifecycle_request_identity=lifecycle_request_identity,
            lifecycle_command_identity=lifecycle_command_identity,
            source_cache_control_identity=canonical_identity({"cache": "control"}),
            component_lock_identities=(
                canonical_identity({"fixture": "component-lock"}),
            ),
            receipt_identity=receipt.identity,
            receipt=receipt,
        )
        provisional_document = provisional.to_dict()
        self.schemas.validate(
            "urn:literate-ai:schema:v1:project-test-receipt-provisional",
            provisional_document,
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:testing-contracts", provisional_document
        )
        self.assertEqual(
            ProjectTestReceiptProvisional.from_dict(provisional_document), provisional
        )
        finalized = ProjectTestReceiptFinalizedCandidate.finalize(
            provisional,
            source_cache_decision_identity=source_cache_decision_identity,
            source_cache_lifecycle_identity=source_cache_lifecycle_identity,
        )
        finalized_document = finalized.to_dict()
        self.schemas.validate(
            "urn:literate-ai:schema:v1:project-test-receipt-finalized-candidate",
            finalized_document,
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:testing-contracts", finalized_document
        )

        invalid = wire(document)
        invalid["tests"] = 0
        with self.assertRaises(AssertionError):
            self.schemas.validate(
                "urn:literate-ai:schema:v1:project-test-receipt", invalid
            )

    def test_known_failure_annotation_and_report_match_public_wire_schema(self) -> None:
        pin = canonical_identity({"pin": "fixture"})
        context = canonical_identity({"worker": "fixture"})
        failure = canonical_identity({"failure": "authentication"})
        annotation = KnownTestFailureAnnotation(
            pin_identity=pin,
            test_key="fixture.Cases.test_authentication",
            cause=KnownTestFailureCause.AUTHENTICATION_REQUIRED,
            context_identity=context,
            universal_authority=None,
            failure_identity=failure,
            evidence_run_identity=canonical_identity({"run": "fixture"}),
            evidence_node_id="n0001",
            reason="External content requires interactive authentication",
            policy=KnownTestFailurePolicy.MANUAL_REVALIDATION,
            tracker="https://example.test/issues/1",
        )
        report = KnownTestFailureReport(
            suite="fixture",
            pin_identity=pin,
            context_identity=context,
            release_evidence=False,
            summary=KnownTestFailureSummary(1, 0, 0, 0, 1, 0),
            outcomes=(
                KnownTestFailureOutcome(
                    test_key=annotation.test_key,
                    status=TestOutcomeStatus.KNOWN_FAILURE,
                    failure_identity=failure,
                    annotation_identity=annotation.identity,
                ),
            ),
        )
        for schema, document in (
            (annotation.SCHEMA, annotation.to_dict()),
            (report.SCHEMA, report.to_dict()),
        ):
            self.schemas.validate(schema, document)
            self.schemas.validate(
                "urn:literate-ai:schema:v1:testing-contracts", document
            )

    def test_project_test_receipt_policy_matches_public_wire_schema(self) -> None:
        policy = ProjectTestReceiptPolicy(
            suite_id="portable-e2e",
            suite_version="1.0.0",
            runner_identity=canonical_identity({"runner": "trusted"}),
            required_evidence_kinds=("test-report", "test-runner"),
            minimum_test_count=20,
        )

        document = policy.to_dict()
        self.schemas.validate(
            "urn:literate-ai:schema:v1:project-test-receipt-policy", document
        )
        self.schemas.validate("urn:literate-ai:schema:v1:testing-contracts", document)
        self.assertEqual(ProjectTestReceiptPolicy.from_dict(document), policy)

        invalid = wire(document)
        invalid["required_evidence_kinds"] = ["test-report"]
        with self.assertRaises(AssertionError):
            self.schemas.validate(
                "urn:literate-ai:schema:v1:project-test-receipt-policy", invalid
            )

    def test_generated_test_suite_matches_public_wire_schema(self) -> None:
        suite = {
            "schema": "urn:literate-ai:schema:v1:generated-test-suite",
            "recipe_identity": "sha256:" + "d" * 64,
            "generation_mode": "major-rebuild",
            "cases": [
                {
                    "case_id": f"case-{category}",
                    "category": category,
                    "specification_refs": ["openspec/spec.md"],
                    "arguments": [{"value": index}],
                    "expected_result": {"value": index * 2},
                }
                for index, category in enumerate(
                    ("example", "boundary", "invariant"), 1
                )
            ],
        }

        self.schemas.validate("urn:literate-ai:schema:v1:generated-test-suite", suite)
        self.schemas.validate("urn:literate-ai:schema:v1:testing-contracts", suite)

    def test_security_runtime_contracts_match_public_wire_schema(self) -> None:
        now = datetime(2026, 8, 3, tzinfo=UTC)
        attestation = OriginAttestation(
            DIGEST,
            "schema-signer",
            "schema-trust-root",
            "schema-signature",
            True,
        )
        finding = SecurityFinding(
            "finding:schema",
            DIGEST,
            "schema",
            FindingSeverity.LOW,
            "scanner:schema@1",
            "schema finding",
        )
        policy = SecurityPolicy(DIGEST)
        classification = policy.classify(
            effective_revision_digest=DIGEST,
            attestations=(attestation,),
            findings=(finding,),
        )
        build_request = BuildRequest(
            DIGEST,
            DIGEST,
            "builder:schema@1",
            DIGEST,
            "constrained",
            ("compiler",),
            ("artifact",),
        )
        build_request_declaration = BuildRequestDeclaration(
            DIGEST,
            "builder:schema@1",
            DIGEST,
            "constrained",
            ("compiler",),
            ("artifact",),
        )
        build_authorization = policy.authorize_build(
            classification,
            build_request,
            actor="schema-authorizer",
            reason="schema conformance",
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        observation_request = ObservationRequest(
            DIGEST,
            (DIGEST,),
            "runner:schema@1",
            DIGEST,
            "constrained",
            ("processes",),
            ("trace",),
        )
        observation_authorization = policy.authorize_observation(
            classification,
            observation_request,
            actor="schema-authorizer",
            reason="schema conformance",
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        report = RuleBasedSourceScanner(
            "scanner:schema@1", baseline_python_rules()
        ).scan((SourceModule.create("main.py", b"value = 1\n"),))
        revocations = AuthorizationRevocationSet().revoke(
            build_authorization.authorization_id,
            actor="schema-security",
            reason="schema conformance",
        )

        contracts = (
            (attestation.SCHEMA, attestation, OriginAttestation),
            (finding.SCHEMA, finding, SecurityFinding),
            (classification.SCHEMA, classification, SecurityClassification),
            (
                build_request_declaration.SCHEMA,
                build_request_declaration,
                BuildRequestDeclaration,
            ),
            (build_request.SCHEMA, build_request, BuildRequest),
            (observation_request.SCHEMA, observation_request, ObservationRequest),
            (build_authorization.SCHEMA, build_authorization, BuildAuthorization),
            (
                observation_authorization.SCHEMA,
                observation_authorization,
                ObservationExecutionAuthorization,
            ),
            (report.SCHEMA, report, SecurityScanReport),
            (revocations.SCHEMA, revocations, AuthorizationRevocationSet),
        )
        for schema_id, contract, contract_type in contracts:
            with self.subTest(schema_id=schema_id):
                document = contract.to_dict()
                self.assertEqual(contract_type.from_dict(document), contract)
                if schema_id == ObservationExecutionAuthorization.SCHEMA:
                    legacy_schema = (
                        "urn:literate-ai:schema:v1:observation-execution-authorization"
                    )
                    historical_document = {
                        key: value
                        for key, value in document.items()
                        if key not in {"schema", "effective_revision_digest", "profile"}
                    }
                    self.schemas.validate(schema_id, document)
                    self.schemas.validate(legacy_schema, historical_document)
                    with self.assertRaisesRegex(AssertionError, "unknown field"):
                        self.schemas.validate(legacy_schema, document)
                    evidence = deferred_schema_change("security.schema.json")
                    self.assertIn(legacy_schema, evidence["reused_uris"])
                    self.assertIn(
                        "changed-observation-authorization-shape",
                        evidence["change_classes"],
                    )
                else:
                    self.schemas.validate(schema_id, document)

    def test_bundle_publication_settings_application_and_inverse_authoring(
        self,
    ) -> None:
        blob = BlobRef("c" * 64, 4, media_type="text/plain")
        bundle = BundleManifest(
            BundleKind.SOURCE,
            DIGEST,
            DIGEST,
            None,
            {"source": blob},
            (),
            (blob,),
            component_ref=ComponentRevisionRef(
                ComponentCoordinate("test", "component"),
                "1.0.0",
                ContentIdentity.parse_uri(DIGEST),
            ),
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:bundle-manifest", bundle.to_dict()
        )
        self.assertEqual(BundleManifest.from_dict(bundle.to_dict()), bundle)
        publication_component = ComponentRevisionRef(
            ComponentCoordinate("test", "component"),
            "1.0.0",
            ContentIdentity.parse_uri(DIGEST),
        )
        publication_policy = PublicationPolicy(
            DIGEST,
            allowed_targets=("schema-target",),
        )
        publication_request = PublicationRequest.create(
            component_ref=publication_component,
            component_lock_identity=canonical_identity(
                {"component-lock": "schema-publication"}
            ),
            effective_revision_digest=DIGEST,
            source_bundle=blob,
            roots={"source": blob, "provenance": blob},
            blobs=(blob,),
            provenance=(blob,),
            security_classification_digest=DIGEST,
            security_profile=SecurityProfile.CONSTRAINED,
            target_id="schema-target",
            target_identity_digest=DIGEST,
            policy_digest=publication_policy.policy_digest,
            actor="schema-test",
        )
        publication_time = datetime(2026, 8, 2, tzinfo=UTC)
        publication = PublicationManifest.create(
            request=publication_request,
            authorization=publication_policy.authorize(
                publication_request,
                reason="schema conformance",
                issued_at=publication_time,
                expires_at=publication_time + timedelta(minutes=5),
            ),
            created_at=publication_time,
        )
        self.assertEqual(
            PublicationManifest.from_dict(publication.to_dict()), publication
        )
        import_policy = ImportPolicy(
            DIGEST,
            allowed_source_targets=(publication.request.target_id,),
            allowed_classification_digests=(
                publication.request.security_classification_digest,
            ),
            trusted_publication_policy_digests=(publication.request.policy_digest,),
        )
        import_request = ImportRequest.create(
            manifest=publication,
            publication_manifest=blob,
            destination_identity_digest=DIGEST,
            policy_digest=import_policy.policy_digest,
            actor="schema-importer",
        )
        import_authorization = import_policy.authorize(
            import_request,
            reason="schema conformance",
            issued_at=publication_time,
            expires_at=publication_time + timedelta(minutes=5),
        )
        self.assertEqual(
            ImportRequest.from_dict(import_request.to_dict()), import_request
        )
        self.assertEqual(
            ImportAuthorization.from_dict(import_authorization.to_dict()),
            import_authorization,
        )
        receipt = TransferReceipt(
            operation_id="publish_" + "a" * 24,
            direction="publish",
            target_id=publication.request.target_id,
            target_identity_digest=publication.request.target_identity_digest,
            publication_manifest=blob,
            component_id=publication.component_id,
            revision=publication.revision,
            component_ref=publication.component_ref,
            component_lock_identity=publication.component_lock_identity,
            effective_revision_digest=publication.request.effective_revision_digest,
            source_bundle=publication.request.source_bundle,
            provenance=publication.request.provenance,
            security_classification_digest=(
                publication.request.security_classification_digest
            ),
            security_profile=publication.request.security_profile,
            publication_request_digest=publication.request.digest,
            publication_authorization_id=(publication.authorization.authorization_id),
            publication_policy_digest=publication.request.policy_digest,
            import_request_digest=None,
            import_authorization_id=None,
            import_policy_digest=None,
            blob_count=1,
            transferred_count=1,
            reused_count=0,
            started_at=publication_time.isoformat(),
            completed_at=publication_time.isoformat(),
        )
        self.assertEqual(TransferReceipt.from_dict(receipt.to_dict()), receipt)
        historical_manifest = {
            "schema_version": 2,
            "component_id": publication.component_id,
            "revision": publication.revision,
            "component_ref": publication.component_ref.to_dict(),
            "roots": {
                key: value.to_dict()
                for key, value in sorted(publication.request.roots.items())
            },
            "blobs": [value.to_dict() for value in publication.request.blobs],
            "created_at": publication.created_at,
        }
        historical_receipt = {
            "schema_version": 2,
            "operation_id": receipt.operation_id,
            "direction": receipt.direction,
            "target_id": receipt.target_id,
            "publication_manifest": receipt.publication_manifest.to_dict(),
            "component_id": receipt.component_id,
            "revision": receipt.revision,
            "component_ref": receipt.component_ref.to_dict(),
            "blob_count": receipt.blob_count,
            "transferred_count": receipt.transferred_count,
            "reused_count": receipt.reused_count,
            "started_at": receipt.started_at,
            "completed_at": receipt.completed_at,
        }
        for schema_id, document in (
            ("urn:literate-ai:schema:v1:publication-manifest", historical_manifest),
            ("urn:literate-ai:schema:v1:transfer-receipt", historical_receipt),
        ):
            with self.subTest(publication_contract=tuple(document)):
                self.schemas.validate(schema_id, document)
                self.schemas.validate(
                    "urn:literate-ai:schema:v1:publication-contracts", document
                )

        publication_evidence = deferred_schema_change("publication.schema.json")
        for schema_id in (
            "urn:literate-ai:schema:v1:publication-request",
            "urn:literate-ai:schema:v1:publication-authorization",
            "urn:literate-ai:schema:v1:import-request",
            "urn:literate-ai:schema:v1:import-authorization",
        ):
            with self.subTest(unreleased_publication_contract=schema_id):
                with self.assertRaisesRegex(AssertionError, "unknown schema resource"):
                    self.schemas.resolve(schema_id)
                self.assertIn(schema_id, publication_evidence["unreleased_only_uris"])
        for legacy_schema, document in (
            ("urn:literate-ai:schema:v1:publication-manifest", publication.to_dict()),
            ("urn:literate-ai:schema:v1:transfer-receipt", receipt.to_dict()),
        ):
            with self.subTest(deferred_publication_shape=legacy_schema):
                self.schemas.validate(document["schema"], document)
                with self.assertRaises(AssertionError):
                    self.schemas.validate(legacy_schema, document)
                self.assertIn(legacy_schema, publication_evidence["reused_uris"])

        invalid_manifest = wire(historical_manifest)
        invalid_manifest["schema_version"] = True
        invalid_receipt = wire(historical_receipt)
        invalid_receipt["publication_manifest"]["size"] = True
        for schema_id, document in (
            (
                "urn:literate-ai:schema:v1:publication-manifest",
                invalid_manifest,
            ),
            ("urn:literate-ai:schema:v1:transfer-receipt", invalid_receipt),
        ):
            with self.subTest(invalid_publication_contract=schema_id):
                with self.assertRaises(AssertionError):
                    self.schemas.validate(schema_id, document)
        settings = SettingsDocument(1, SettingScope.USER, {"models.group": "local"})
        self.schemas.validate(
            "urn:literate-ai:schema:v1:settings-document", settings.to_dict()
        )
        self.assertEqual(SettingsDocument.from_dict(settings.to_dict()), settings)

        content = canonical_identity({"value": "fixture"})
        readiness = SourceEvidenceReadiness(
            (content,), (DIGEST,), (DIGEST,), True, True
        )
        component_readiness = ComponentSourceEvidenceReadiness(
            content, (DIGEST,), (DIGEST,), True, True
        )
        prompt = "bounded prompt"
        context = GenerationContextBinding(
            content,
            content,
            content,
            content,
            content,
            ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            ),
            content,
            content,
            "workspace",
            prompt,
        )
        event = GenerationEvent.create("run", 1, "started", None, {"identity": DIGEST})
        provenance = GenerationProvenance(
            content,
            content,
            content,
            content,
            content,
            content,
            content,
            context.prompt_identity,
            content,
            content,
            content,
            content,
            component_readiness.identity,
            (DIGEST,),
            (content,),
            (content,),
            content,
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:source-evidence-readiness", readiness.to_dict()
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:generation-event", event.to_dict()
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v2:component-source-evidence-readiness",
            component_readiness.to_dict(),
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v3:generation-context-binding", context.to_dict()
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v4:generation-provenance", provenance.to_dict()
        )

        inverse = SourceToSpecificationRequest(
            "request",
            "snapshot",
            DIGEST,
            "attestation",
            RunMode.BOOTSTRAP,
            "openspec",
            "skills",
            "routing",
            "redaction",
            "egress",
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:source-to-specification-request",
            canonical_value(inverse),
        )

    def test_final_cutover_manifest_matches_public_wire_schema(self) -> None:
        identity = canonical_identity
        manifest = FinalCutoverManifest(
            framework_release_identity=identity({"release": "0.1.1"}),
            compatible_release_identities=(
                identity({"release": "0.1.0a1"}),
                identity({"release": "0.1.1a1"}),
            ),
            seams=tuple(
                SeamCutover(seam, identity({"comparison": seam.value}))
                for seam in LifecycleSeam
            ),
            compatibility_reader_identity=identity({"reader": "ova@1"}),
            compatibility_reader_support_term="until explicit major removal",
            dependency_audit_identity=identity({"audit": "downstream"}),
            duplicate_general_lifecycle_implementation_removed=True,
            rollback=RollbackRehearsal(
                identity({"state": "baseline"}),
                identity({"state": "copy"}),
                identity({"evidence": "rollback"}),
                True,
                True,
                True,
            ),
            observation=ObservationDecision(
                ObservationDisposition.USER_DIRECTED_WITHOUT_ELAPSED_WINDOW,
                "repository-owner",
                "complete Phase 2 now",
                directive_reference="user-directive:2026-08-02",
            ),
        )

        self.schemas.validate(
            "urn:literate-ai:schema:v1:final-cutover-manifest",
            manifest.to_dict(),
        )

    def test_regenerative_qualification_records_match_public_wire_schemas(self) -> None:
        qualification_schemas = SchemaCatalog(V2_ROOT)

        def identity(label: str) -> str:
            return canonical_identity({"fixture": label}).uri

        target = identity("target")
        policy = RegenerativeQualificationPolicy(
            policy_id="source-promotion@1",
            minimum_clean_runs=2,
            required_target_profile_ids=(target,),
            required_surface_ids=("api",),
        )
        evidence = CleanRegenerationEvidence(
            run_id=identity("run"),
            run_attestation_id=identity("attestation"),
            source_snapshot_id=identity("source"),
            specification_set_id=identity("specification"),
            target_profile_id=target,
            flavor_lock_id=identity("flavors"),
            generation_recipe_id=identity("recipe"),
            generated_tree_id=identity("tree"),
            build_result_id=identity("build"),
            generated_test_result_id=identity("generated-tests"),
            independent_parity_result_id=identity("parity"),
            covered_surface_ids=("api",),
            source_excluded_from_generation=True,
            empty_workspace=True,
            generated_source_cache_hit=False,
            build_passed=True,
            generated_tests_passed=True,
            independent_parity_passed=True,
            generated_tests_total=1,
            generated_tests_succeeded=1,
            generated_tests_failed=0,
            generated_tests_skipped=0,
        )
        second_value = evidence.to_dict()
        second_value["run_id"] = identity("run-2")
        second_value["run_attestation_id"] = identity("attestation-2")
        second_evidence = CleanRegenerationEvidence.from_dict(second_value)
        decision = qualify_regenerative_specification(
            source_snapshot_id=evidence.source_snapshot_id,
            specification_set_id=evidence.specification_set_id,
            policy=policy,
            evidence=(evidence, second_evidence),
        )
        for schema_id, record in (
            (policy.SCHEMA, policy),
            (evidence.SCHEMA, evidence),
            (decision.SCHEMA, decision),
        ):
            with self.subTest(schema_id=schema_id):
                qualification_schemas.validate(schema_id, record.to_dict())
                qualification_schemas.validate(
                    "urn:literate-ai:schema:v2:source-qualification-contracts",
                    record.to_dict(),
                )

        for field in ("required_target_profile_ids", "required_surface_ids"):
            with self.subTest(empty_policy_field=field):
                invalid_policy = policy.to_dict()
                invalid_policy[field] = []
                with self.assertRaisesRegex(AssertionError, "invalid item count"):
                    qualification_schemas.validate(policy.SCHEMA, invalid_policy)

    def test_model_translation_journal_and_operational_attestation_schemas(
        self,
    ) -> None:
        model_output = {
            "schema": "urn:literate-ai:schema:v1:source-to-specification-model-output",
            "language": "python",
            "observations": [
                {
                    "skill_id": "language-python",
                    "facet": "language-binding",
                    "claim_kind": "observed-current-behavior",
                    "requirement": "The application accepts signed integers.",
                    "capability": "portable total",
                    "scenario": {
                        "name": "Sum integers",
                        "when": "integer arguments are supplied",
                        "then": "the application emits their sum",
                    },
                    "evidence_ids": ["evidence:python"],
                    "confidence_basis_points": 9000,
                    "scope": "base",
                    "component_coordinate": "local/application",
                }
            ],
            "component_graph": {
                "root_coordinate": "local/application",
                "nodes": [
                    {
                        "coordinate": "local/application",
                        "title": "Application",
                        "provided_capabilities": ["portable total"],
                        "source_paths": ["app.py"],
                        "observation_indexes": [0],
                        "evidence_ids": ["evidence:python"],
                    }
                ],
                "edges": [],
            },
        }
        evidence = {
            "reference": {
                "evidence_id": "evidence:python",
                "source_snapshot_id": DIGEST,
                "content_digest": DIGEST,
                "path": "app.py",
                "symbol": "main",
            },
            "language": "python",
            "kind": "source-file",
            "start_line": 1,
            "end_line": 1,
            "call_path": ["main"],
            "content": "def main(): pass\n",
        }
        intelligence = {
            "schema": "urn:literate-ai:schema:v1:source-to-specification-intelligence",
            "source_snapshot_id": DIGEST,
            "provider_id": "codegraph-cli",
            "provider_version": "structured-symbol-query-v1",
            "runtime_version": "1.1.6",
            "executable_identity": DIGEST,
            "database_identity": DIGEST,
            "built_with_version": "1.1.6",
            "extraction_version": 24,
            "document_count": 1,
            "node_count": 1,
            "edge_count": 0,
            "languages": ["python"],
            "evidence": [evidence],
            "queries": [
                {
                    "query_id": DIGEST,
                    "language": "python",
                    "text": "main",
                    "evidence_ids": ["evidence:python"],
                }
            ],
        }
        journal = {
            "schema": (
                "urn:literate-ai:schema:v1:source-to-specification-model-call-journal"
            ),
            "call_id": DIGEST,
            "language": "python",
            "coding_cli": "codex",
            "executable": "/opt/tools/codex",
            "executable_identity": DIGEST,
            "model": "test-model",
            "command": ["/opt/tools/codex", "exec"],
            "prompt": "translate evidence",
            "response": model_output,
            "response_text": json.dumps(model_output),
            "stdout": "",
            "stderr": "",
            "request_identity": DIGEST,
            "response_identity": DIGEST,
            "command_identity": DIGEST,
            "selection_identity": DIGEST,
            "tool_binding_identity": DIGEST,
            "isolation": {
                "profile": "workspace",
                "clean_configuration": True,
                "filesystem_boundary": "workspace-write",
                "hermetic": False,
                "limitations": ["provider internals are not attested"],
            },
            "environment_keys": ["PATH"],
            "skill_refs": [
                {
                    "skill_id": "language-python",
                    "version": "1.0.0",
                    "content_digest": DIGEST,
                }
            ],
            "intelligence_identity": DIGEST,
            "egress_policy_id": "explicit-egress@1",
        }
        translation = {
            "schema": (
                "urn:literate-ai:schema:v1:source-to-specification-translation-run"
            ),
            "mode": "coding-cli",
            "intelligence": intelligence,
            "journals": [journal],
        }
        for schema_id, value in (
            (model_output["schema"], model_output),
            (intelligence["schema"], intelligence),
            (journal["schema"], journal),
            (translation["schema"], translation),
        ):
            with self.subTest(schema_id=schema_id):
                self.schemas.validate(schema_id, value)
                self.schemas.validate(
                    "urn:literate-ai:schema:v1:source-translation-contracts", value
                )

        qualification_schemas = SchemaCatalog(V2_ROOT)
        operational = {
            "schema": (
                "urn:literate-ai:schema:v2:operational-qualification-attestation"
            ),
            "plan": {
                "run_id": DIGEST,
                "specification_set_id": DIGEST,
                "target_profile_id": DIGEST,
                "flavor_lock_id": DIGEST,
                "generation_recipe_id": DIGEST,
                "covered_surface_ids": ["cli"],
            },
            "source_snapshot_id": DIGEST,
            "source_exclusion_identity": DIGEST,
            "regenerator_identity": DIGEST,
            "regeneration": {
                "generation_input_ids": [DIGEST],
                "generated_tree_id": DIGEST,
                "build_result_id": DIGEST,
                "generated_test_result_id": DIGEST,
                "generated_source_cache_hit": False,
                "build_passed": True,
                "generated_tests_total": 1,
                "generated_tests_succeeded": 1,
                "generated_tests_failed": 0,
                "generated_tests_skipped": 0,
            },
            "parity_verifier_identity": DIGEST,
            "parity_request": {
                "run_id": DIGEST,
                "source_snapshot_id": DIGEST,
                "specification_set_id": DIGEST,
                "target_profile_id": DIGEST,
                "generated_tree_id": DIGEST,
                "covered_surface_ids": ["cli"],
            },
            "parity": {
                "result_id": DIGEST,
                "passed": True,
                "covered_surface_ids": ["cli"],
            },
            "attestor_identity": DIGEST,
            "empty_workspace": True,
            "exact_generation_inputs": True,
        }
        qualification_schemas.validate(operational["schema"], operational)
        qualification_schemas.validate(
            "urn:literate-ai:schema:v2:source-qualification-contracts", operational
        )

        profile = LocalQualificationProfile(
            profile_id="schema-fixture@1",
            build_command=("bazel", "build", "//:run"),
            test_commands=(("bazel", "test", "//..."),),
            source_command=("source-run",),
            generated_command=("generated-run",),
            cases=(LocalQualificationCase("value-one", ({"value": 1},), {"value": 1}),),
            covered_surface_ids=("cli",),
        )
        qualification_schemas.validate(
            "urn:literate-ai:schema:v2:local-regenerative-qualification-profile",
            profile.to_dict(),
        )
        attestor = LocalHmacQualificationAttestor(
            signer="schema-fixture", key=b"s" * 32
        )
        attestor.attest(operational)
        qualification_schemas.validate(
            "urn:literate-ai:schema:v2:local-qualification-signature",
            attestor.envelopes[0],
        )

    def test_v3_source_intelligence_schema_preserves_typed_relationships(self):
        evidence = {
            "reference": {
                "evidence_id": "evidence:python",
                "source_snapshot_id": DIGEST,
                "content_digest": DIGEST,
                "path": "app.py",
                "symbol": "main",
            },
            "language": "python",
            "kind": "source-file",
            "start_line": 1,
            "end_line": 1,
            "call_path": ["main"],
            "content": "def main(): pass\n",
        }
        intelligence = {
            "schema": (
                "urn:literate-ai:schema:v3:source-to-specification-intelligence"
            ),
            "authority_source_snapshot_id": DIGEST,
            "indexed_source_snapshot_id": DIGEST,
            "indexed_source_tree_id": DIGEST,
            "indexed_source_files": [
                {"path": "app.py", "size": 17, "identity": DIGEST}
            ],
            "source_content_identity": DIGEST,
            "source_material_identity": DIGEST,
            "intelligence_evidence_identity": DIGEST,
            "provider_id": "fixture-intelligence",
            "provider_version": "structured-symbol-query-v1",
            "runtime_version": "1.1.6",
            "executable_identity": DIGEST,
            "artifact_identity": DIGEST,
            "artifact_media_type": "application/vnd.sqlite3",
            "capabilities": ["call-graph", "references"],
            "provider_properties": ["extraction-version=24"],
            "document_count": 1,
            "symbol_count": 2,
            "relationship_count": 1,
            "unresolved_relationship_count": 1,
            "warning_count": 0,
            "languages": ["python"],
            "evidence": [evidence],
            "relationships": [
                {
                    "relationship_id": DIGEST,
                    "kind": "calls",
                    "source_symbol": "main",
                    "source_path": "app.py",
                    "source_language": "python",
                    "target_symbol": "helper",
                    "target_path": "app.py",
                    "target_language": "python",
                    "line": 1,
                    "column": 0,
                    "provenance": None,
                    "resolution_method": "exact-match",
                    "confidence_basis_points": 9000,
                }
            ],
            "unresolved_relationships": [
                {
                    "unresolved_id": DIGEST,
                    "source_symbol": "main",
                    "source_path": "app.py",
                    "source_language": "python",
                    "reference_name": "external",
                    "reference_kind": "calls",
                    "line": 1,
                    "column": 0,
                    "candidates": [],
                }
            ],
            "queries": [
                {
                    "query_id": DIGEST,
                    "language": "python",
                    "text": "main",
                    "evidence_ids": ["evidence:python"],
                }
            ],
        }
        translation = {
            "schema": (
                "urn:literate-ai:schema:v3:source-to-specification-translation-run"
            ),
            "mode": "source-intelligence-coding-cli",
            "intelligence": intelligence,
            "journals": [],
        }
        schemas = SchemaCatalog(V2_ROOT)
        schemas.validate(intelligence["schema"], intelligence)
        schemas.validate(translation["schema"], translation)


if __name__ == "__main__":
    unittest.main()
