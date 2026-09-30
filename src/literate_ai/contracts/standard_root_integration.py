"""Exact evidence binding the Standard root integration and package boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail
from .executable_components import (
    ArtifactBuildGraph,
    ExactLinkPlan,
    PackagePlan,
    PackageResult,
)
from .identity import ContentIdentity, contract_identity

STANDARD_ROOT_INTEGRATION_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v1:standard-root-integration-evidence"
)


def _identity(value: object, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


@dataclass(frozen=True, slots=True)
class StandardRootIntegrationEvidence:
    """One exact root graph, package, execution, and independent acceptance chain."""

    component_lock_identity: ContentIdentity
    execution_plan_identity: ContentIdentity
    project_build_plan_identity: ContentIdentity
    artifact_graph: ArtifactBuildGraph
    link_plan: ExactLinkPlan
    package_plan: PackagePlan
    package_result: PackageResult
    root_generated_integration_test_identity: ContentIdentity
    packaged_execution_identity: ContentIdentity
    independent_acceptance_identity: ContentIdentity

    SCHEMA: ClassVar[str] = STANDARD_ROOT_INTEGRATION_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_lock_identity",
            "execution_plan_identity",
            "project_build_plan_identity",
            "root_generated_integration_test_identity",
            "packaged_execution_identity",
            "independent_acceptance_identity",
        ):
            _identity(getattr(self, name), f"StandardRootIntegrationEvidence.{name}")
        stage_identities = (
            self.root_generated_integration_test_identity,
            self.packaged_execution_identity,
            self.independent_acceptance_identity,
        )
        if len(set(stage_identities)) != len(stage_identities):
            fail(
                "StandardRootIntegrationEvidence",
                "integration test, packaged execution, and independent acceptance "
                "identities must be distinct",
            )
        for name, expected_type in (
            ("artifact_graph", ArtifactBuildGraph),
            ("link_plan", ExactLinkPlan),
            ("package_plan", PackagePlan),
            ("package_result", PackageResult),
        ):
            if not isinstance(getattr(self, name), expected_type):
                fail(
                    f"StandardRootIntegrationEvidence.{name}",
                    f"must be a {expected_type.__name__}",
                )

        graph_links = {item.identity: item for item in self.artifact_graph.link_plans}
        if graph_links.get(self.link_plan.identity) != self.link_plan:
            fail(
                "StandardRootIntegrationEvidence.link_plan",
                "must be an exact link plan in the artifact graph",
            )
        plan = self.package_plan
        if (
            plan.component_lock_identity != self.component_lock_identity
            or plan.artifact_graph_identity != self.artifact_graph.identity
            or plan.link_plan_identity != self.link_plan.identity
            or plan.root_artifact_identity != self.link_plan.root_artifact_identity
        ):
            fail(
                "StandardRootIntegrationEvidence.package_plan",
                "must bind the exact lock, artifact graph, link plan, and "
                "root artifact",
            )
        if plan.root_component_revision not in {
            item.component_revision for item in self.artifact_graph.manifests
        }:
            fail(
                "StandardRootIntegrationEvidence.package_plan",
                "root Component revision is absent from the artifact graph",
            )

        result = self.package_result
        if (
            result.package_plan_identity != plan.identity
            or result.root_component_revision != plan.root_component_revision
            or result.component_lock_identity != plan.component_lock_identity
            or result.target_identity != plan.target_identity
            or result.artifact_graph_identity != plan.artifact_graph_identity
            or result.package_kind is not plan.package_kind
            or result.packager_identity != plan.packager_identity
            or result.entrypoints != plan.entrypoints
            or result.runtime_requirements != plan.runtime_requirements
            or result.native_library_root != plan.native_library_root
            or result.native_library_layout != plan.native_library_layout
        ):
            fail(
                "StandardRootIntegrationEvidence.package_result",
                "must be the exact result of the retained package plan",
            )
        planned_files = tuple(
            (
                item.path,
                item.role,
                item.kind,
                item.source_identity,
                item.target_identity,
                item.blob,
            )
            for item in plan.inputs
        )
        result_files = tuple(
            (
                item.path,
                item.role,
                item.kind,
                item.source_identity,
                item.target_identity,
                item.blob,
            )
            for item in result.files
        )
        if result_files != planned_files:
            fail(
                "StandardRootIntegrationEvidence.package_result.files",
                "must contain every and only exact planned package input",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "project_build_plan_identity": self.project_build_plan_identity.to_dict(),
            "artifact_graph": self.artifact_graph.to_dict(),
            "link_plan": self.link_plan.to_dict(),
            "package_plan": self.package_plan.to_dict(),
            "package_result": self.package_result.to_dict(),
            "root_generated_integration_test_identity": (
                self.root_generated_integration_test_identity.to_dict()
            ),
            "packaged_execution_identity": (self.packaged_execution_identity.to_dict()),
            "independent_acceptance_identity": (
                self.independent_acceptance_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardRootIntegrationEvidence"
    ) -> StandardRootIntegrationEvidence:
        identity_names = (
            "component_lock_identity",
            "execution_plan_identity",
            "project_build_plan_identity",
            "root_generated_integration_test_identity",
            "packaged_execution_identity",
            "independent_acceptance_identity",
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    *identity_names,
                    "artifact_graph",
                    "link_plan",
                    "package_plan",
                    "package_result",
                }
            ),
        )
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identity_names
            },
            artifact_graph=ArtifactBuildGraph.from_dict(
                data["artifact_graph"], path=f"{path}.artifact_graph"
            ),
            link_plan=ExactLinkPlan.from_dict(
                data["link_plan"], path=f"{path}.link_plan"
            ),
            package_plan=PackagePlan.from_dict(
                data["package_plan"], path=f"{path}.package_plan"
            ),
            package_result=PackageResult.from_dict(
                data["package_result"], path=f"{path}.package_result"
            ),
        )


__all__ = [
    "STANDARD_ROOT_INTEGRATION_EVIDENCE_SCHEMA",
    "StandardRootIntegrationEvidence",
]
