"""Derive release closure only from an accepted Standard lifecycle."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from literate_ai.contracts import (
    BlobRef,
    ComponentLock,
    ComponentRevisionRef,
    ContentIdentity,
)
from literate_ai.contracts._validation import (
    contract_fields,
    enum_value,
    fail,
    list_value,
    parse_tuple,
    string_value,
)
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components import (
    ArtifactAssemblyDependency,
    ArtifactBuildGraph,
    ComponentExecutionPlan,
    PackageFileKind,
    PackagePlan,
    PackageResult,
    ReleaseArtifactSet,
    RuntimeRequirement,
)
from literate_ai.contracts.executable_components.packages import (
    PackageEntrypoint,
    PackageInput,
    PackageKind,
    SourceBundleClosure,
    release_evidence_manifest_bytes,
)

from .artifact_graph import (
    create_artifact_build_graph,
    create_package_plan,
    realize_manifest,
)
from .packaging import PackageBlobReader, verify_package_result
from .standard_project_lifecycle import (
    StandardNodeLifecycleResult,
    StandardProjectLifecycleResult,
)


class ReleaseArtifactAssemblyError(ValueError):
    """Accepted lifecycle, artifact graph, and package evidence disagree."""


@dataclass(frozen=True, slots=True)
class StandardReleaseDeclaration:
    """Explicit pre-package authority bound to one exact planned root and lock."""

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:standard-release-declaration"

    execution_plan_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    root_component_revision: ContentIdentity
    target_identity: ContentIdentity
    root_artifact_identity: ContentIdentity
    package_kind: PackageKind
    packager_identity: ContentIdentity
    destinations: tuple[tuple[str, str], ...]
    entrypoints: tuple[PackageEntrypoint, ...]
    resource_inputs: tuple[PackageInput, ...] = ()
    runtime_requirements: tuple[RuntimeRequirement, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.execution_plan_identity,
            self.component_lock_identity,
            self.root_component_revision,
            self.target_identity,
            self.root_artifact_identity,
            self.packager_identity,
        ):
            if not isinstance(value, ContentIdentity):
                raise TypeError("release declaration identities must be typed")
        if not isinstance(self.package_kind, PackageKind):
            raise TypeError("release declaration package kind must be typed")
        if (
            not isinstance(self.destinations, tuple)
            or not self.destinations
            or len(self.destinations) > 16384
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(
                    isinstance(value, str) and 0 < len(value) <= 4096 for value in item
                )
                for item in self.destinations
            )
        ):
            raise ReleaseArtifactAssemblyError(
                "release destinations must be unique canonical identity/path pairs"
            )
        destination_keys = tuple(item[0] for item in self.destinations)
        destination_paths = tuple(item[1] for item in self.destinations)
        if destination_keys != tuple(sorted(set(destination_keys))) or len(
            set(destination_paths)
        ) != len(destination_paths):
            raise ReleaseArtifactAssemblyError(
                "release destinations must be unique canonical identity/path pairs"
            )
        canonical_values = (
            (
                self.entrypoints,
                PackageEntrypoint,
                lambda item: item.name,
                256,
                True,
                "entrypoints",
            ),
            (
                self.resource_inputs,
                PackageInput,
                lambda item: item.path,
                16384,
                False,
                "resources",
            ),
            (
                self.runtime_requirements,
                RuntimeRequirement,
                lambda item: item.requirement_id,
                1024,
                False,
                "runtime requirements",
            ),
        )
        for values, expected_type, key, maximum, required, label in canonical_values:
            if (
                not isinstance(values, tuple)
                or (required and not values)
                or len(values) > maximum
                or any(not isinstance(item, expected_type) for item in values)
                or tuple(key(item) for item in values)
                != tuple(sorted({key(item) for item in values}))
            ):
                raise ReleaseArtifactAssemblyError(
                    f"release declaration {label} must be typed, unique, and sorted"
                )

    @property
    def identity(self) -> ContentIdentity:
        from literate_ai.contracts import canonical_identity

        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "root_component_revision": self.root_component_revision.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "root_artifact_identity": self.root_artifact_identity.to_dict(),
            "package_kind": self.package_kind.value,
            "packager_identity": self.packager_identity.to_dict(),
            "destinations": [list(item) for item in self.destinations],
            "entrypoints": [item.to_dict() for item in self.entrypoints],
            "resource_inputs": [item.to_dict() for item in self.resource_inputs],
            "runtime_requirements": [
                item.to_dict() for item in self.runtime_requirements
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardReleaseDeclaration"
    ) -> StandardReleaseDeclaration:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "execution_plan_identity",
                    "component_lock_identity",
                    "root_component_revision",
                    "target_identity",
                    "root_artifact_identity",
                    "package_kind",
                    "packager_identity",
                    "destinations",
                    "entrypoints",
                    "resource_inputs",
                    "runtime_requirements",
                }
            ),
        )

        def destination(item: Any, *, path: str) -> tuple[str, str]:
            pair = list_value(item, path)
            if len(pair) != 2:
                fail(path, "must contain exactly two strings")
            return (
                string_value(pair[0], f"{path}[0]"),
                string_value(pair[1], f"{path}[1]"),
            )

        return cls(
            execution_plan_identity=ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            component_lock_identity=ContentIdentity.from_dict(
                data["component_lock_identity"],
                path=f"{path}.component_lock_identity",
            ),
            root_component_revision=ContentIdentity.from_dict(
                data["root_component_revision"],
                path=f"{path}.root_component_revision",
            ),
            target_identity=ContentIdentity.from_dict(
                data["target_identity"], path=f"{path}.target_identity"
            ),
            root_artifact_identity=ContentIdentity.from_dict(
                data["root_artifact_identity"],
                path=f"{path}.root_artifact_identity",
            ),
            package_kind=enum_value(
                PackageKind, data["package_kind"], f"{path}.package_kind"
            ),
            packager_identity=ContentIdentity.from_dict(
                data["packager_identity"], path=f"{path}.packager_identity"
            ),
            destinations=tuple(
                destination(item, path=f"{path}.destinations[{index}]")
                for index, item in enumerate(
                    list_value(data["destinations"], f"{path}.destinations")
                )
            ),
            entrypoints=parse_tuple(
                data["entrypoints"],
                f"{path}.entrypoints",
                PackageEntrypoint.from_dict,
            ),
            resource_inputs=parse_tuple(
                data["resource_inputs"],
                f"{path}.resource_inputs",
                PackageInput.from_dict,
            ),
            runtime_requirements=parse_tuple(
                data["runtime_requirements"],
                f"{path}.runtime_requirements",
                RuntimeRequirement.from_dict,
            ),
        )


def standard_release_evidence_manifest_bytes(
    evidence: Iterable[ContentIdentity],
) -> bytes:
    """Canonical compact blob that makes semantic lifecycle evidence publishable."""

    try:
        return release_evidence_manifest_bytes(evidence)
    except ValueError as exc:
        raise ReleaseArtifactAssemblyError(str(exc)) from exc


def standard_release_evidence_identities(
    lifecycle: StandardProjectLifecycleResult,
) -> tuple[ContentIdentity, ...]:
    """Derive the complete semantic evidence index for one accepted lifecycle."""

    if not isinstance(lifecycle, StandardProjectLifecycleResult):
        raise TypeError("lifecycle must be a StandardProjectLifecycleResult")
    if (
        not lifecycle.successful
        or lifecycle.admission_identity is None
        or lifecycle.aggregate_receipt is None
        or lifecycle.receipt_identity is None
    ):
        raise ReleaseArtifactAssemblyError(
            "release evidence requires a successful accepted Standard lifecycle"
        )
    evidence = {
        lifecycle.identity,
        lifecycle.validation_identity,
        lifecycle.generation_schedule.identity,
        lifecycle.lifecycle_membership.identity,
        lifecycle.admission_identity,
        lifecycle.aggregate_receipt.identity,
        lifecycle.receipt_identity,
    }
    if lifecycle.project_build_plan_identity is not None:
        evidence.add(lifecycle.project_build_plan_identity)
    for result in lifecycle.node_results:
        if (
            result.failure_evidence is not None
            or result.source_output is None
            or result.build_plan_identity is None
            or result.index_identity is None
            or result.authorization_identity is None
            or result.build_identity is None
            or result.test_identity is None
            or result.execution_identity is None
            or result.acceptance_identity is None
            or result.source_cache_membership is None
        ):
            raise ReleaseArtifactAssemblyError(
                "every released Component requires complete accepted lifecycle evidence"
            )
        evidence.update(
            {
                result.identity,
                result.source_generation.identity,
                result.source_output.identity,
                result.build_plan_identity,
                result.index_identity,
                result.authorization_identity,
                result.build_identity,
                result.test_identity,
                result.execution_identity,
                result.acceptance_identity,
                result.source_cache_membership.identity,
            }
        )
        if result.source_cache_publication_identity is not None:
            evidence.add(result.source_cache_publication_identity)
    return tuple(sorted(evidence, key=lambda item: item.uri))


def plan_standard_assembly_dependencies(
    execution_plan: ComponentExecutionPlan,
    results: tuple[StandardNodeLifecycleResult, ...],
) -> tuple[ArtifactAssemblyDependency, ...]:
    """Bind late link inputs to locked edges and exact accepted provider results."""
    if not isinstance(execution_plan, ComponentExecutionPlan) or any(
        not isinstance(item, StandardNodeLifecycleResult) for item in results
    ):
        raise ReleaseArtifactAssemblyError("assembly requires typed plan and results")
    accepted = {item.component_revision: item for item in results}
    expected = {item.component_revision for item in execution_plan.generation_plans}
    if (
        len(accepted) != len(results)
        or set(accepted) != expected
        or any(
            item.failure_code is not None
            or item.acceptance_identity is None
            or not item.exports
            for item in results
        )
    ):
        raise ReleaseArtifactAssemblyError(
            "assembly requires every exact accepted Component"
        )
    edges = {
        edge.identity.uri: edge
        for action in execution_plan.action_plans
        for edge in action.dependency_edges
        if edge.kind in {DependencyKind.RUNTIME, DependencyKind.PACKAGING}
    }
    bindings = []
    for edge in edges.values():
        consumer = accepted[edge.consumer_revision]
        provider = accepted[edge.provider_revision]
        if len(bindings) + len(consumer.exports) * len(provider.exports) > 16384:
            raise ReleaseArtifactAssemblyError(
                "assembly dependencies exceed 16384 bindings"
            )
        for output in consumer.exports:
            for supplied in provider.exports:
                bindings.append(
                    ArtifactAssemblyDependency(
                        output.identity,
                        supplied.identity,
                        edge.kind,
                        edge.identity,
                        provider.acceptance_identity,
                    )
                )
    return tuple(sorted(bindings, key=lambda item: item.identity.uri))


def create_standard_artifact_build_graph(
    execution_plan: ComponentExecutionPlan,
    lifecycle: StandardProjectLifecycleResult,
) -> ArtifactBuildGraph:
    """Derive the only release graph authorized by retained accepted build plans."""

    if not isinstance(execution_plan, ComponentExecutionPlan):
        raise TypeError("execution_plan must be a ComponentExecutionPlan")
    if not isinstance(lifecycle, StandardProjectLifecycleResult):
        raise TypeError("lifecycle must be a StandardProjectLifecycleResult")
    if lifecycle.execution_plan_identity != execution_plan.identity:
        raise ReleaseArtifactAssemblyError(
            "lifecycle result belongs to another Component execution plan"
        )
    project_plan = lifecycle.project_build_plan
    if project_plan is None:
        raise ReleaseArtifactAssemblyError(
            "release graph requires the retained typed project build plan"
        )
    results = {item.component_revision: item for item in lifecycle.node_results}
    manifests = []
    for plan in project_plan.components:
        result = results.get(plan.component_revision)
        if result is None or result.build_plan_identity != plan.identity:
            raise ReleaseArtifactAssemblyError(
                "accepted node results differ from the retained project build plan"
            )
        manifests.append(realize_manifest(plan.manifest, result.exports))
    realized = tuple(sorted(manifests, key=lambda item: item.component_revision.uri))
    drivers = {item.build_system_driver_identity for item in realized}
    if len(drivers) != 1:
        raise ReleaseArtifactAssemblyError(
            "one release graph requires one exact build-system driver"
        )
    root_exports = tuple(
        export.identity
        for manifest in realized
        if manifest.component_revision == execution_plan.root_revision
        for export in manifest.exports
    )
    if not root_exports:
        raise ReleaseArtifactAssemblyError(
            "release graph requires at least one root Component export"
        )
    try:
        return create_artifact_build_graph(
            build_system_driver_identity=next(iter(drivers)),
            manifests=realized,
            link_roots=root_exports,
            assembly_dependencies=plan_standard_assembly_dependencies(
                execution_plan, lifecycle.node_results
            ),
        )
    except Exception as exc:
        raise ReleaseArtifactAssemblyError(
            "accepted build plans cannot form one exact artifact graph"
        ) from exc


def create_standard_package_plan(
    execution_plan: ComponentExecutionPlan,
    artifact_graph: ArtifactBuildGraph,
    declaration: StandardReleaseDeclaration,
) -> PackagePlan:
    """Derive one package plan solely from explicit authority and accepted artifacts."""

    if not isinstance(declaration, StandardReleaseDeclaration):
        raise TypeError("declaration must be a StandardReleaseDeclaration")
    if (
        declaration.execution_plan_identity != execution_plan.identity
        or declaration.component_lock_identity != execution_plan.component_lock_identity
        or declaration.root_component_revision != execution_plan.root_revision
    ):
        raise ReleaseArtifactAssemblyError(
            "release declaration differs from the exact execution authority"
        )
    try:
        return create_package_plan(
            artifact_graph,
            root_component_revision=execution_plan.root_revision,
            component_lock_identity=execution_plan.component_lock_identity,
            target_identity=declaration.target_identity,
            root_artifact_identity=declaration.root_artifact_identity,
            package_kind=declaration.package_kind,
            packager_identity=declaration.packager_identity,
            destinations=dict(declaration.destinations),
            entrypoints=declaration.entrypoints,
            resource_inputs=declaration.resource_inputs,
            runtime_requirements=declaration.runtime_requirements,
        )
    except ReleaseArtifactAssemblyError:
        raise
    except Exception as exc:
        raise ReleaseArtifactAssemblyError(
            "release declaration cannot form an exact package plan"
        ) from exc


def create_standard_release_artifact_set(
    execution_plan: ComponentExecutionPlan,
    lifecycle: StandardProjectLifecycleResult,
    component_lock: ComponentLock,
    artifact_graph: ArtifactBuildGraph,
    packages: Iterable[tuple[StandardReleaseDeclaration, PackageResult]],
    *,
    root_source_bundle: SourceBundleClosure,
    evidence_manifest: BlobRef,
    read_blob: PackageBlobReader,
) -> ReleaseArtifactSet:
    """Bind byte-verified package results to one exact accepted lock and node closure.

    The typed lock is the only source of the root Component ref. The source closure and
    evidence BlobRef must name retrievable bytes bound to the accepted lifecycle.
    Package plans are reconstructed from explicit pre-package declarations and the
    lifecycle-derived artifact graph before their results are byte-verified.
    """

    if not isinstance(execution_plan, ComponentExecutionPlan):
        raise TypeError("execution_plan must be a ComponentExecutionPlan")
    if not isinstance(lifecycle, StandardProjectLifecycleResult):
        raise TypeError("lifecycle must be a StandardProjectLifecycleResult")
    if not isinstance(component_lock, ComponentLock):
        raise TypeError("component_lock must be a ComponentLock")
    if not isinstance(artifact_graph, ArtifactBuildGraph):
        raise TypeError("artifact_graph must be an ArtifactBuildGraph")
    if not lifecycle.successful or lifecycle.receipt_identity is None:
        raise ReleaseArtifactAssemblyError(
            "release artifacts require a successful accepted Standard lifecycle"
        )
    if lifecycle.execution_plan_identity != execution_plan.identity:
        raise ReleaseArtifactAssemblyError(
            "lifecycle result belongs to another Component execution plan"
        )
    expected_artifact_graph = create_standard_artifact_build_graph(
        execution_plan, lifecycle
    )
    if artifact_graph != expected_artifact_graph:
        raise ReleaseArtifactAssemblyError(
            "artifact graph differs from retained accepted build-plan authority"
        )
    if (
        component_lock.identity != execution_plan.component_lock_identity
        or component_lock.root_revision != execution_plan.root_revision
    ):
        raise ReleaseArtifactAssemblyError(
            "release Component lock differs from the exact execution plan"
        )

    planned_revisions = tuple(
        item.component_revision for item in execution_plan.generation_plans
    )
    result_revisions = tuple(item.component_revision for item in lifecycle.node_results)
    graph_revisions = tuple(
        item.component_revision for item in artifact_graph.manifests
    )
    if result_revisions != planned_revisions or graph_revisions != planned_revisions:
        raise ReleaseArtifactAssemblyError(
            "artifact graph and lifecycle must cover every and only planned Component"
        )

    accepted_workspaces = []
    expected_evidence = standard_release_evidence_identities(lifecycle)
    results_by_revision = {
        item.component_revision.uri: item for item in lifecycle.node_results
    }
    for manifest in artifact_graph.manifests:
        result = results_by_revision[manifest.component_revision.uri]
        if manifest.exports != result.exports:
            raise ReleaseArtifactAssemblyError(
                "artifact graph exports differ from the accepted lifecycle outputs"
            )
        assert result.source_output is not None
        accepted_workspaces.append(
            result.source_output.candidate.workspace_allocation_identity
        )

    package_executions = tuple(packages)
    if not package_executions or any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not isinstance(item[0], StandardReleaseDeclaration)
        or not isinstance(item[1], PackageResult)
        for item in package_executions
    ):
        raise ReleaseArtifactAssemblyError(
            "release artifacts require typed release declaration/result pairs"
        )
    package_executions = tuple(
        sorted(package_executions, key=lambda item: item[0].identity.uri)
    )
    package_values: list[PackageResult] = []
    for declaration, result in package_executions:
        try:
            plan = create_standard_package_plan(
                execution_plan, artifact_graph, declaration
            )
            package_values.append(
                verify_package_result(plan, result, read_blob=read_blob)
            )
        except ReleaseArtifactAssemblyError:
            raise
        except Exception as exc:
            raise ReleaseArtifactAssemblyError(
                "package plan or result failed exact graph and byte verification"
            ) from exc
    package_values_tuple = tuple(
        sorted(package_values, key=lambda item: item.package_plan_identity.uri)
    )
    if len({item.package_plan_identity for item in package_values}) != len(
        package_values
    ):
        raise ReleaseArtifactAssemblyError(
            "release package-plan identities must be unique"
        )
    targets = {item.target_identity for item in package_values_tuple}
    if len(targets) != 1:
        raise ReleaseArtifactAssemblyError(
            "one release artifact set cannot mix target identities"
        )
    target_identity = next(iter(targets))
    root_exports = tuple(
        export
        for manifest in artifact_graph.manifests
        for export in manifest.exports
        if export.component_revision == execution_plan.root_revision
    )
    if not root_exports or any(
        item.target_identity != target_identity for item in root_exports
    ):
        raise ReleaseArtifactAssemblyError(
            "release packages do not match the root artifact target"
        )
    root_result = results_by_revision[execution_plan.root_revision.uri]
    assert root_result.source_output is not None
    root_candidate = root_result.source_output.candidate
    if (
        not isinstance(root_source_bundle, SourceBundleClosure)
        or root_source_bundle.root.identity != root_candidate.source_bundle_identity.uri
        or root_source_bundle.tree_identity != root_candidate.tree_identity
    ):
        raise ReleaseArtifactAssemblyError(
            "root source bundle does not match the accepted root source"
        )

    if not isinstance(evidence_manifest, BlobRef):
        raise TypeError("evidence_manifest must be a BlobRef")
    expected_evidence_bytes = standard_release_evidence_manifest_bytes(
        expected_evidence
    )
    if evidence_manifest.digest != hashlib.sha256(
        expected_evidence_bytes
    ).hexdigest() or evidence_manifest.size != len(expected_evidence_bytes):
        raise ReleaseArtifactAssemblyError(
            "release evidence manifest differs from the accepted lifecycle closure"
        )
    for reference in (
        root_source_bundle.root,
        *(item.blob for item in root_source_bundle.files),
        evidence_manifest,
    ):
        content = read_blob(reference)
        if (
            not isinstance(content, bytes)
            or len(content) != reference.size
            or hashlib.sha256(content).hexdigest() != reference.digest
        ):
            raise ReleaseArtifactAssemblyError(
                "release source or evidence blob failed byte verification"
            )

    root_node = next(
        item
        for item in component_lock.nodes
        if item.revision.identity == component_lock.root_revision
    )
    root_component_ref = ComponentRevisionRef(
        root_node.revision.coordinate,
        root_node.revision.version,
        root_node.revision.identity,
    )

    resources = tuple(
        sorted(
            {
                file.source_identity
                for package in package_values_tuple
                for file in package.files
                if file.kind is PackageFileKind.RESOURCE
            },
            key=lambda item: item.uri,
        )
    )
    return ReleaseArtifactSet(
        root_component_ref=root_component_ref,
        root_component_revision=execution_plan.root_revision,
        root_source_bundle=root_source_bundle,
        component_lock_identity=execution_plan.component_lock_identity,
        target_identity=target_identity,
        artifact_graph_identity=artifact_graph.identity,
        accepted_workspace_identities=tuple(
            sorted(accepted_workspaces, key=lambda item: item.uri)
        ),
        release_declaration_identities=tuple(
            sorted(
                (declaration.identity for declaration, _result in package_executions),
                key=lambda item: item.uri,
            )
        ),
        packages=package_values_tuple,
        resource_identities=resources,
        evidence_identities=expected_evidence,
        evidence_manifest=evidence_manifest,
    )


__all__ = [
    "ReleaseArtifactAssemblyError",
    "StandardReleaseDeclaration",
    "plan_standard_assembly_dependencies",
    "create_standard_artifact_build_graph",
    "create_standard_package_plan",
    "create_standard_release_artifact_set",
    "standard_release_evidence_identities",
    "standard_release_evidence_manifest_bytes",
]
