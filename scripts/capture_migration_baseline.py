#!/usr/bin/env python3
"""Capture complete deterministic cache/SBOM/bundle/receipt wire observations.

This program is intentionally self-contained so it can be executed with ``PYTHONPATH``
pointing at a historical Literate AI source tree.  Its own bytes, interpreter identity,
arguments, and the selected Git commit/tree are bound by the characterization fixture.
It writes one canonical JSON document to stdout and never modifies the selected source.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import fields

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


def _identity(label: str):
    return canonical_identity({"fixture": label})


def capture_wire_observations() -> dict[str, object]:
    """Return complete canonical wires produced by the selected implementation."""

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
    accepted_arguments: dict[str, object] = {
        "cache_key": cache_key,
        "source_tree_identity": _identity("source-tree"),
        "managed_sbom_graph_identity": graph.identity,
        "source_sbom_identity": source_binding.bom_identity,
        "resolved_sbom_identity": resolved_binding.bom_identity,
        "source_sbom_binding": source_binding,
        "resolved_sbom_binding": resolved_binding,
        "generated_test_suite_identity": _identity("generated-tests"),
        "build_evidence_identity": _identity("build"),
        "test_evidence_identity": _identity("tests"),
        "acceptance_identity": _identity("acceptance"),
        "provenance_identity": _identity("provenance"),
    }
    # The historical replay imports the pre-FND-002 contract from a Git archive.
    # Keep the capture tool able to describe that old wire while the live wire
    # intentionally excludes source-intelligence observations from source identity.
    if "source_index_identity" in {
        field.name for field in fields(AcceptedSourceDerivation)
    }:
        accepted_arguments["source_index_identity"] = _identity("source-index")
    accepted = AcceptedSourceDerivation(**accepted_arguments)
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
        receipt.identity,
        receipt,
    )
    finalized = ProjectTestReceiptFinalizedCandidate(
        provisional.identity,
        lifecycle_request,
        lifecycle_command,
        source_cache_control,
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
        raise RuntimeError("CycloneDX builders did not emit canonical JSON bytes")

    documents = {
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
    return {
        name: {
            "wire_identity": (
                "sha256:" + hashlib.sha256(canonical_json_bytes(document)).hexdigest()
            ),
            "wire": document,
        }
        for name, document in sorted(documents.items())
    }


def main() -> int:
    sys.stdout.buffer.write(canonical_json_bytes(capture_wire_observations()) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
