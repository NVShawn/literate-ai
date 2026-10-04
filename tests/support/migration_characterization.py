"""Wire-characterization helpers kept for the migration refresh script.

The characterization test cases were removed from the suite; only the symbols the
``make migration-baseline-refresh`` script imports remain here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.artifacts import BundleKind, BundleManifest
from literate_ai.contracts import (
    AcceptedSourceDerivation,
    ComponentCoordinate,
    ComponentRevisionRef,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedEdge,
    CycloneDxManagedGraph,
    DependencyKind,
    ManagedComponentKind,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    VersionedContentRef,
    canonical_identity,
    canonical_json_bytes,
    component_bom_ref,
)
from literate_ai.storage import BlobRef

REPO_ROOT = Path(__file__).resolve().parents[2]
CURRENT_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "migration-characterization-post-lock-200.json"
)


def _identity(label: str):
    return canonical_identity({"fixture": label})


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
