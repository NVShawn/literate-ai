"""Historical Wave-0 evidence plus live lock/lifecycle contract characterization.

Historical fixtures remain immutable replay evidence. Current samples are deliberately
not compared with obsolete ``component.json`` snapshots; focused migration fixtures own
that compatibility boundary.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.artifacts import BundleKind, BundleManifest
from literate_ai.contracts import (
    AcceptedSourceDerivation,
    CandidateStatus,
    ComponentCoordinate,
    ComponentRevisionRef,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedEdge,
    CycloneDxManagedGraph,
    DependencyKind,
    EffectiveRevision,
    EffectiveSpecificationSet,
    FlavorCandidateDecision,
    FlavorDefinition,
    FlavorRevision,
    FlavorSetLock,
    ManagedComponentKind,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    TargetProfile,
    VersionedContentRef,
    canonical_identity,
    canonical_json_bytes,
    component_bom_ref,
)
from literate_ai.storage import BlobRef
from tests.conformance.support import sample_runner as runner

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPO_ROOT / "samples"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "migration-characterization-v1.json"
CURRENT_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "migration-characterization-post-lock-200.json"
)
HISTORICAL_WIRE_FIXTURE_PATH = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "migration-characterization-pre-fnd-002-wires.json"
)

CAPTURE_SOURCE_REVISION = "8e4ebdb806ae3aa53287853e4dbed7453fbd9cb4"
CAPTURE_SOURCE_TREE = "99dd662d6cfbae8967b39885e51f71784594aae7"
CAPTURE_TOOL_IDENTITY = (
    "sha256:286baa72bf49ec25c1cab56f1f97b87edb0ed2d37bbae3958c0f4a90c64eb319"
)
PRE_FND_002_OBSERVATIONS_IDENTITY = (
    "sha256:08fcf606292b40b975584ee1d1bbb6180df833e7817d8d5a51cecaa68d399c4f"
)
HISTORICAL_WIRE_FIXTURE_IDENTITY = (
    "sha256:76978c3783fd77391484ea77dbcf117776fcde1a7978c5275b8ea8dcfd14614c"
)
CAPTURE_TOOL_DESCRIPTOR = {
    "schema": "literate-ai/migration-characterization-capture-tool@1",
    "name": "wave-0-migration-characterization",
    "version": "1.0.0",
    "protocol": "canonical-json-observations-v1",
    "surfaces": [
        "sample-component-manifests",
        "representative-component-composition",
        "sample-generation-recipes",
        "cache-sbom-receipt-wires",
        "sample-runner-ast",
    ],
}


def _historical_capture_tree() -> str | None:
    """Return the source tree only when this checkout retains the old Git object."""

    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{CAPTURE_SOURCE_REVISION}^{{tree}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _identity(label: str):
    return canonical_identity({"fixture": label})


def _fixture(path: Path = FIXTURE_PATH) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _wire_shape_observation(
    mutate: Callable[[dict[str, dict[str, object]]], None] | None = None,
) -> dict[str, object]:
    """Capture complete canonical wires, rather than field-name silhouettes.

    The optional mutator is a test-only fault-injection seam.  It operates after every
    typed contract has emitted its wire document and before identities are computed, so
    a same-shape semantic mutation must be visible in the resulting observation.
    """

    composition_identity = _identity("composition")
    component_revision = _identity("component-revision")
    dependency_revision = _identity("dependency-revision")
    root_ref = component_bom_ref(component_revision)
    dependency_ref = component_bom_ref(dependency_revision)
    graph = CycloneDxManagedGraph(
        root_ref,
        tuple(
            sorted(
                (
                    CycloneDxManagedComponent(
                        root_ref,
                        ManagedComponentKind.ROOT,
                        component_revision,
                        "component://fixture/root",
                        "1.0.0",
                        (),
                    ),
                    CycloneDxManagedComponent(
                        dependency_ref,
                        ManagedComponentKind.COMPONENT,
                        dependency_revision,
                        "component://fixture/dependency",
                        "2.0.0",
                        ("runtime",),
                    ),
                ),
                key=lambda item: item.bom_ref,
            )
        ),
        (
            CycloneDxManagedEdge(
                root_ref,
                dependency_ref,
                DependencyKind.RUNTIME,
                False,
            ),
        ),
        composition_identity,
    )
    source_bom, source_binding = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=graph,
    )
    resolved_bom, resolved_binding = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.RESOLVED,
        managed_graph=graph,
        source_bom=source_bom,
    )
    cache_key = SourceDerivationCacheKey(
        recipe_identity=_identity("recipe"),
        execution_plan_identity=_identity("execution-plan"),
        coding_cli_tool_binding_identity=_identity("coding-cli-tool-binding"),
        model_binding=SourceCacheModelBinding("fixture-provider", "fixture-model"),
        request_identity=_identity("request"),
    )
    accepted = AcceptedSourceDerivation(
        cache_key=cache_key,
        component_lock_identity=composition_identity,
        source_tree_identity=_identity("source-tree"),
        managed_sbom_graph_identity=graph.identity,
        source_sbom_identity=source_binding.bom_identity,
        resolved_sbom_identity=resolved_binding.bom_identity,
        source_sbom_binding=source_binding,
        resolved_sbom_binding=resolved_binding,
        generated_test_suite_identity=_identity("generated-tests"),
        build_evidence_identity=_identity("build"),
        test_evidence_identity=_identity("tests"),
        acceptance_identity=_identity("acceptance"),
        provenance_identity=_identity("provenance"),
    )
    component_ref = ComponentRevisionRef(
        ComponentCoordinate("fixture", "root"), "1.0.0", component_revision
    )
    blob = BlobRef("b" * 64, 4, media_type="text/plain")
    bundle = BundleManifest(
        BundleKind.SOURCE,
        component_revision.uri,
        _identity("effective-revision").uri,
        None,
        {"source": blob},
        (),
        (blob,),
        component_ref=component_ref,
    )

    lifecycle_request = _identity("lifecycle-request")
    lifecycle_command = _identity("lifecycle-command")
    source_cache_control = _identity("source-cache-control")
    source_cache_decision = _identity("source-cache-decision")
    source_cache_lifecycle = _identity("source-cache-lifecycle")
    component_locks = (_identity("component-lock"),)
    receipt = ProjectTestReceipt(
        project_id="fixture-project",
        project_revision_identity=_identity("project-revision"),
        subject_identity=_identity("subject"),
        suite=VersionedContentRef(
            "test-suite", "portable-e2e", "1.0.0", _identity("suite")
        ),
        outcome="passed",
        summary=ProjectTestSummary(5, 5, 0, 0),
        result_identity=_identity("result"),
        evidence=tuple(
            ProjectTestEvidence(kind, _identity(kind))
            for kind in (
                "lifecycle-command",
                "lifecycle-request",
                "source-cache-decision",
                "source-cache-lifecycle",
                "test-report",
            )
        ),
    )
    provisional = ProjectTestReceiptProvisional(
        lifecycle_request,
        lifecycle_command,
        source_cache_control,
        component_locks,
        receipt.identity,
        receipt,
    )
    finalized = ProjectTestReceiptFinalizedCandidate(
        provisional.identity,
        lifecycle_request,
        lifecycle_command,
        source_cache_control,
        component_locks,
        source_cache_decision,
        source_cache_lifecycle,
        receipt.identity,
        receipt,
    )
    source_document = json.loads(source_bom)
    resolved_document = json.loads(resolved_bom)
    if source_bom != canonical_json_bytes(source_document) or (
        resolved_bom != canonical_json_bytes(resolved_document)
    ):
        raise AssertionError("CycloneDX builders no longer emit canonical JSON bytes")

    wire_documents: dict[str, dict[str, object]] = {
        "accepted_source_derivation": accepted.to_dict(),
        "bundle_manifest": bundle.to_dict(),
        "cyclonedx_resolved_bom": resolved_document,
        "cyclonedx_resolved_bom_binding": resolved_binding.to_dict(),
        "cyclonedx_source_bom": source_document,
        "cyclonedx_source_bom_binding": source_binding.to_dict(),
        "managed_sbom_graph": graph.to_dict(),
        "project_test_receipt": receipt.to_dict(),
        "project_test_receipt_finalized_candidate": finalized.to_dict(),
        "project_test_receipt_provisional": provisional.to_dict(),
        "source_derivation_cache_key": cache_key.to_dict(),
    }
    if mutate is not None:
        mutate(wire_documents)

    def addressed(document: dict[str, object]) -> dict[str, object]:
        canonical_bytes = canonical_json_bytes(document)
        return {
            "wire_identity": ("sha256:" + hashlib.sha256(canonical_bytes).hexdigest()),
            "wire": document,
        }

    return {
        name: addressed(document) for name, document in sorted(wire_documents.items())
    }


def _identity_impact_model() -> dict[str, object]:
    """Return the bounded Wave-0 identity graph for observed sample planning.

    This is deliberately not labeled a global framework closure.  It follows the exact
    resolved identities that the sample recipe binds, plus their cache/SBOM/receipt
    consumers.  Unobserved packaging, publication, and deployment identities are outside
    this characterization.
    """

    changed_roots = (
        "component-definition-wire-layout",
        "component-revision-wire-layout",
        "effective-revision-wire-layout",
        "effective-specification-set-wire-layout",
        "flavor-definition-wire-layout",
        "flavor-revision-wire-layout",
        "flavor-set-lock-wire-layout",
        "lifecycle-driver-contract",
        "target-profile-wire-layout",
        "toolchain-constraint-wire-layout",
    )
    stable_inputs = (
        "acceptance-contract-content-identity",
        "capability-requirement-semantics",
        "component-coordinate-and-version",
        "flavor-resolution-policy-identity",
        "flavor-specification-fragment-identities",
        "generation-recipe-document-content",
        "resolver-contribution-identities",
        "specification-artifact-content-identity",
        "specification-set-identity",
    )
    dependency_graph = {
        "accepted-source-derivation-identity": [
            "managed-sbom-graph-identity",
            "resolved-sbom-identity",
            "source-derivation-cache-key-identity",
            "source-sbom-identity",
        ],
        "bundle-manifest-bytes": [
            "component-revision-identity",
            "effective-revision-identity",
            "flavor-set-lock-identity",
        ],
        "component-composition-identity": [
            "component-revision-identity",
            "requirement-resolution-decision-identity",
        ],
        "component-definition-identity": ["component-definition-wire-layout"],
        "component-revision-identity": [
            "component-definition-identity",
            "component-revision-wire-layout",
            "specification-set-identity",
        ],
        "effective-revision-identity": [
            "component-revision-identity",
            "effective-revision-wire-layout",
            "effective-specification-set-identity",
            "flavor-resolution-policy-identity",
            "flavor-set-lock-identity",
            "resolver-contribution-identities",
            "target-profile-identity",
        ],
        "effective-specification-set-identity": [
            "effective-specification-set-wire-layout",
            "flavor-set-lock-identity",
            "flavor-specification-fragment-identities",
            "specification-set-identity",
        ],
        "flavor-definition-identity": [
            "flavor-definition-wire-layout",
            "toolchain-constraint-identity",
        ],
        "flavor-revision-identity": [
            "flavor-definition-identity",
            "flavor-revision-wire-layout",
        ],
        "flavor-set-lock-identity": [
            "component-revision-identity",
            "flavor-resolution-policy-identity",
            "flavor-revision-identity",
            "flavor-set-lock-wire-layout",
            "target-profile-identity",
        ],
        "generation-recipe-identity": [
            "component-composition-identity",
            "component-revision-identity",
            "effective-revision-identity",
            "effective-specification-set-identity",
            "flavor-set-lock-identity",
            "generation-recipe-document-content",
            "target-profile-identity",
        ],
        "managed-sbom-graph-identity": ["component-composition-identity"],
        "project-test-receipt-wire-and-identity": [
            "lifecycle-driver-contract",
            "sample-test-runner-identity",
            "source-derivation-cache-key-identity",
        ],
        "requirement-resolution-decision-identity": [
            "component-revision-identity",
            "capability-requirement-semantics",
        ],
        "resolved-sbom-identity": ["source-sbom-identity"],
        "sample-test-runner-identity": ["lifecycle-driver-contract"],
        "source-derivation-cache-key-identity": ["generation-recipe-identity"],
        "source-sbom-identity": ["managed-sbom-graph-identity"],
        "target-profile-identity": ["target-profile-wire-layout"],
        "toolchain-constraint-identity": ["toolchain-constraint-wire-layout"],
    }

    impacted = set(changed_roots)
    changed = True
    while changed:
        changed = False
        for identity, dependencies in dependency_graph.items():
            if identity not in impacted and impacted.intersection(dependencies):
                impacted.add(identity)
                changed = True

    return {
        "scope": "wave-0-observed-sample-planning-cache-sbom-receipt",
        "completeness": "complete-for-declared-scope-not-global-framework-closure",
        "excluded_surfaces": [
            "deployment-identities",
            "packaging-identities",
            "publication-identities",
            "unobserved-component-compositions",
        ],
        "changed_roots": list(changed_roots),
        "stable_inputs": list(stable_inputs),
        "dependency_graph": dependency_graph,
        "cannot_preserve_exactly": sorted(impacted - set(changed_roots)),
    }


def _flavor_identity_probe(
    template_recipe,
    definition: FlavorDefinition,
    target_profile: TargetProfile,
    resolution_policy,
) -> dict[str, str]:
    """Exercise real Flavor wire identities through the six recipe input bindings."""

    component_revision = _identity("impact-component-revision")
    component_composition = _identity("impact-component-composition")
    flavor_revision = FlavorRevision(definition, None, ())
    component_ref = ComponentRevisionRef(
        ComponentCoordinate("fixture", "impact-root"),
        "1.0.0",
        component_revision,
    )
    lock = FlavorSetLock(
        component_revision,
        target_profile,
        resolution_policy,
        (
            FlavorCandidateDecision(
                flavor_revision.identity,
                CandidateStatus.SELECTED,
                (),
            ),
        ),
        (flavor_revision.identity,),
        component_ref,
        (flavor_revision.ref,),
    )
    effective_specifications = EffectiveSpecificationSet(
        _identity("impact-specification-set"),
        tuple(item.identity for item in definition.specification_fragments),
        lock.identity,
    )
    effective_revision = EffectiveRevision(
        component_revision,
        target_profile.identity,
        lock.identity,
        effective_specifications.identity,
        definition.contributions,
        resolution_policy,
    )
    recipe = replace(
        template_recipe,
        resolved_inputs=(
            ("component_revision", component_revision.uri),
            ("component_composition", component_composition.uri),
            ("target_profile", target_profile.identity.uri),
            ("flavor_set_lock", lock.identity.uri),
            (
                "effective_specification_set",
                effective_specifications.identity.uri,
            ),
            ("effective_revision", effective_revision.identity.uri),
        ),
    )
    return {
        "component_composition-identity": component_composition.uri,
        "component-revision-identity": component_revision.uri,
        "effective-revision-identity": effective_revision.identity.uri,
        "effective-specification-set-identity": effective_specifications.identity.uri,
        "flavor-definition-identity": definition.identity.uri,
        "flavor-resolution-policy-identity": resolution_policy.uri,
        "flavor-revision-identity": flavor_revision.identity.uri,
        "flavor-set-lock-identity": lock.identity.uri,
        "generation-recipe-identity": recipe.identity,
        "target-profile-identity": target_profile.identity.uri,
    }


class MigrationCharacterizationBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _fixture()
        self.observations = self.fixture["observations"]
        self.post_fnd_002_fixture = _fixture(CURRENT_FIXTURE_PATH)
        self.post_fnd_002_observations = self.post_fnd_002_fixture["observations"]

    def test_fixture_is_versioned_content_addressed_observation(self) -> None:
        self.assertEqual(
            self.fixture["schema"], "literate-ai/test-migration-characterization@1"
        )
        self.assertEqual(
            self.fixture["status"],
            "observed-current-behavior-not-endorsed-architecture",
        )
        self.assertEqual(
            self.fixture["observations_identity"],
            canonical_identity(self.observations).uri,
        )
        self.assertEqual(
            self.fixture["observations_identity"],
            PRE_FND_002_OBSERVATIONS_IDENTITY,
        )
        self.assertEqual(
            self.post_fnd_002_fixture["schema"],
            "literate-ai/test-migration-characterization@3",
        )
        self.assertEqual(
            self.post_fnd_002_fixture["status"],
            "observed-post-lock-200-current-contracts",
        )
        self.assertEqual(
            self.post_fnd_002_fixture["baseline_observations_identity"],
            "sha256:a485b4cf246cc4abfe1c044c7be0b91c414a6ee50d21e1545ebcf5382a9af840",
        )
        self.assertEqual(
            self.post_fnd_002_fixture["observations_identity"],
            canonical_identity(self.post_fnd_002_observations).uri,
        )

    def test_pre_fnd_002_fixture_records_content_addressed_capture(self) -> None:
        capture = self.fixture["capture"]
        self.assertEqual(capture["source_revision"], CAPTURE_SOURCE_REVISION)
        self.assertEqual(capture["source_tree"], CAPTURE_SOURCE_TREE)
        tool = capture["tool"]
        self.assertEqual(tool["descriptor"], CAPTURE_TOOL_DESCRIPTOR)
        self.assertEqual(tool["identity"], CAPTURE_TOOL_IDENTITY)
        self.assertEqual(
            CAPTURE_TOOL_IDENTITY, canonical_identity(CAPTURE_TOOL_DESCRIPTOR).uri
        )

    def test_pre_fnd_002_capture_tree_matches_retained_history(self) -> None:
        tree = _historical_capture_tree()
        if tree is None:
            self.skipTest("sanitized checkout does not retain historical Git objects")
        self.assertEqual(tree, CAPTURE_SOURCE_TREE)

    def test_complete_pre_fnd_002_wire_fixture_has_current_integrity(
        self,
    ) -> None:
        fixture = _fixture(HISTORICAL_WIRE_FIXTURE_PATH)
        fixture_material = dict(fixture)
        fixture_identity = fixture_material.pop("fixture_identity")
        self.assertEqual(fixture_identity, HISTORICAL_WIRE_FIXTURE_IDENTITY)
        self.assertEqual(canonical_identity(fixture_material).uri, fixture_identity)
        self.assertEqual(
            canonical_identity(fixture["capture"]).uri,
            fixture["capture_identity"],
        )
        self.assertEqual(
            canonical_identity(fixture["wires"]).uri,
            fixture["wires_identity"],
        )
        tool_path = REPO_ROOT / fixture["capture"]["tool"]["path"]
        self.assertEqual(
            "sha256:" + hashlib.sha256(tool_path.read_bytes()).hexdigest(),
            fixture["capture"]["tool"]["content_identity"],
        )
        reference = self.fixture["complete_wire_fixture"]
        self.assertEqual(
            reference["path"],
            tool_path.parent.parent.joinpath(
                *HISTORICAL_WIRE_FIXTURE_PATH.relative_to(REPO_ROOT).parts
            )
            .relative_to(REPO_ROOT)
            .as_posix(),
        )
        self.assertEqual(reference["identity"], fixture_identity)
        characterization_material = {
            "capture": self.fixture["capture"],
            "observations_identity": self.fixture["observations_identity"],
            "complete_wire_fixture": reference,
        }
        self.assertEqual(
            canonical_identity(characterization_material).uri,
            self.fixture["characterization_identity"],
        )
        substituted_capture = json.loads(json.dumps(characterization_material))
        substituted_capture["capture"]["tool"]["descriptor"]["version"] = "forged"
        self.assertNotEqual(
            canonical_identity(substituted_capture).uri,
            self.fixture["characterization_identity"],
        )
        self.assertEqual(
            _wire_shape_observation(), self.post_fnd_002_observations["wire_shapes"]
        )

    def test_complete_pre_fnd_002_wires_replay_from_exact_historical_source(
        self,
    ) -> None:
        tree = _historical_capture_tree()
        if tree is None:
            self.skipTest("sanitized checkout does not retain historical Git objects")
        self.assertEqual(tree, CAPTURE_SOURCE_TREE)
        fixture = _fixture(HISTORICAL_WIRE_FIXTURE_PATH)
        tool_path = REPO_ROOT / fixture["capture"]["tool"]["path"]

        archive = subprocess.check_output(
            ["git", "archive", CAPTURE_SOURCE_REVISION, "src"],
            cwd=REPO_ROOT,
        )
        with tempfile.TemporaryDirectory() as temporary:
            archive_root = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
                for member in source.getmembers():
                    relative = PurePosixPath(member.name)
                    self.assertFalse(relative.is_absolute())
                    self.assertNotIn("..", relative.parts)
                    self.assertEqual(relative.parts[0], "src")
                    target = archive_root.joinpath(*relative.parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    self.assertTrue(member.isreg())
                    extracted = source.extractfile(member)
                    self.assertIsNotNone(extracted)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(extracted.read())
            environment = dict(os.environ)
            environment.update(
                {
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONPATH": str(archive_root / "src"),
                }
            )
            replay = subprocess.run(
                [sys.executable, str(tool_path)],
                cwd=archive_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                timeout=30,
            )
        self.assertEqual(json.loads(replay.stdout), fixture["wires"])

    def test_live_contract_wires_match_the_post_fnd_002_observation(self) -> None:
        current = self.post_fnd_002_observations
        self.assertEqual(_wire_shape_observation(), current["wire_shapes"])
        self.assertEqual(_identity_impact_model(), current["identity_impact_model"])

    def test_content_addressed_wires_detect_same_shape_semantic_mutations(self) -> None:
        baseline = _wire_shape_observation()

        def accepted_source(documents: dict[str, dict[str, object]]) -> None:
            documents["accepted_source_derivation"]["source_tree_identity"] = _identity(
                "mutated-source-tree"
            ).to_dict()

        def cache_model(documents: dict[str, dict[str, object]]) -> None:
            model = documents["source_derivation_cache_key"]["model_binding"]
            assert isinstance(model, dict)
            model["model"] = "mutated-model"

        def managed_graph(documents: dict[str, dict[str, object]]) -> None:
            graph = documents["managed_sbom_graph"]
            components = graph["components"]
            edges = graph["edges"]
            assert isinstance(components, list) and isinstance(edges, list)
            components[1]["version"] = "9.9.9"
            edges[0]["optional"] = True

        def source_bom(documents: dict[str, dict[str, object]]) -> None:
            bom = documents["cyclonedx_source_bom"]
            components = bom["components"]
            assert isinstance(components, list)
            components[0]["version"] = "9.9.9"

        def receipt(documents: dict[str, dict[str, object]]) -> None:
            documents["project_test_receipt"]["tests"] = 999
            for name in (
                "project_test_receipt_provisional",
                "project_test_receipt_finalized_candidate",
            ):
                nested = documents[name]["receipt"]
                assert isinstance(nested, dict)
                nested["tests"] = 999

        def bundle_blob(documents: dict[str, dict[str, object]]) -> None:
            roots = documents["bundle_manifest"]["roots"]
            assert isinstance(roots, dict)
            source = roots["source"]
            assert isinstance(source, dict)
            source["size"] = 999
            source["media_type"] = "application/octet-stream"

        cases = (
            ("accepted_source_derivation", accepted_source),
            ("source_derivation_cache_key", cache_model),
            ("managed_sbom_graph", managed_graph),
            ("cyclonedx_source_bom", source_bom),
            (
                (
                    "project_test_receipt",
                    "project_test_receipt_provisional",
                    "project_test_receipt_finalized_candidate",
                ),
                receipt,
            ),
            ("bundle_manifest", bundle_blob),
        )
        for affected, mutator in cases:
            names = (affected,) if isinstance(affected, str) else affected
            with self.subTest(wires=names):
                mutated = _wire_shape_observation(mutator)
                for name in names:
                    self.assertEqual(
                        sorted(baseline[name]["wire"]),
                        sorted(mutated[name]["wire"]),
                    )
                    self.assertNotEqual(
                        baseline[name]["wire_identity"],
                        mutated[name]["wire_identity"],
                    )

    def test_identity_impact_model_has_the_actual_flavor_and_recipe_edges(self) -> None:
        model = self.post_fnd_002_observations["identity_impact_model"]
        self.assertEqual(
            model["completeness"],
            "complete-for-declared-scope-not-global-framework-closure",
        )
        graph = model["dependency_graph"]
        self.assertEqual(
            set(graph["flavor-definition-identity"]),
            {"flavor-definition-wire-layout", "toolchain-constraint-identity"},
        )
        self.assertEqual(
            set(graph["flavor-revision-identity"]),
            {"flavor-definition-identity", "flavor-revision-wire-layout"},
        )
        self.assertEqual(
            set(graph["flavor-set-lock-identity"]),
            {
                "component-revision-identity",
                "flavor-resolution-policy-identity",
                "flavor-revision-identity",
                "flavor-set-lock-wire-layout",
                "target-profile-identity",
            },
        )
        self.assertEqual(
            set(graph["generation-recipe-identity"])
            - {"generation-recipe-document-content"},
            {
                "component-composition-identity",
                "component-revision-identity",
                "effective-revision-identity",
                "effective-specification-set-identity",
                "flavor-set-lock-identity",
                "target-profile-identity",
            },
        )
        known = {
            *model["changed_roots"],
            *model["stable_inputs"],
            *graph,
        }
        self.assertFalse(
            {
                dependency
                for dependencies in graph.values()
                for dependency in dependencies
            }
            - known
        )
        self.assertTrue(
            set(model["changed_roots"])
            <= {
                dependency
                for dependencies in graph.values()
                for dependency in dependencies
            }
        )

    def test_real_flavor_mutations_follow_the_declared_identity_edges(self) -> None:
        # One portable recipe carries these identity mutations. Discovering every
        # sample would make this pure characterization depend on live CUDA tools.
        selected = runner.discover(SAMPLES, ("hello-component",), platform="linux")
        with (
            patch.object(runner, "_host_os", return_value="linux"),
            patch.object(runner, "discover", return_value=selected),
        ):
            template_recipe = runner.plan_recipes(SAMPLES)[0]["recipe"]
        root = REPO_ROOT / "flavors" / "lang-python"
        path = root / "flavor.md"
        definition = parse_flavor_markdown(
            path.read_bytes(), source=path.as_posix()
        ).resolve(lambda uri: root.joinpath(*Path(uri).parts).read_bytes())
        profile = TargetProfile(
            "impact-profile",
            "1.0.0",
            "explicit",
            _identity("impact-target-provider"),
            (),
        )
        policy = _identity("impact-resolution-policy")
        baseline = _flavor_identity_probe(template_recipe, definition, profile, policy)

        cases = (
            (
                _flavor_identity_probe(
                    template_recipe,
                    replace(
                        definition,
                        display_name=definition.display_name + " changed",
                    ),
                    profile,
                    policy,
                ),
                {
                    "flavor-definition-identity",
                    "flavor-revision-identity",
                    "flavor-set-lock-identity",
                    "effective-specification-set-identity",
                    "effective-revision-identity",
                    "generation-recipe-identity",
                },
            ),
            (
                _flavor_identity_probe(
                    template_recipe,
                    definition,
                    replace(
                        profile,
                        provider_configuration=_identity(
                            "changed-impact-target-provider"
                        ),
                    ),
                    policy,
                ),
                {
                    "target-profile-identity",
                    "flavor-set-lock-identity",
                    "effective-specification-set-identity",
                    "effective-revision-identity",
                    "generation-recipe-identity",
                },
            ),
            (
                _flavor_identity_probe(
                    template_recipe,
                    definition,
                    profile,
                    _identity("changed-impact-resolution-policy"),
                ),
                {
                    "flavor-resolution-policy-identity",
                    "flavor-set-lock-identity",
                    "effective-specification-set-identity",
                    "effective-revision-identity",
                    "generation-recipe-identity",
                },
            ),
        )
        for changed, expected_changed in cases:
            with self.subTest(expected=sorted(expected_changed)):
                actual_changed = {
                    name
                    for name, identity in changed.items()
                    if baseline[name] != identity
                }
                self.assertEqual(actual_changed, expected_changed)


if __name__ == "__main__":
    unittest.main()
