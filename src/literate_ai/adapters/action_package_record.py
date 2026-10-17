"""Bounded PACKAGE intent binds the exact plan and every Component LINK result."""

import json
from dataclasses import dataclass

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.application.action_dag_planning import plan_lifecycle_action_dag
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.application.artifact_graph import create_package_plan
from literate_ai.contracts import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.executable_components import (
    ArtifactBuildGraph,
    ComponentExecutionPlan,
    PackageFileKind,
    PackagePlan,
)


def _invalid():
    raise ActionWireError("action_package.input_invalid", "PACKAGE intent refused")


@dataclass(frozen=True)
class PackageWorkerInput:
    execution_plan: ComponentExecutionPlan
    artifact_graph: ArtifactBuildGraph
    plan: PackagePlan
    link_results: tuple[tuple[str, ContentIdentity], ...]

    def to_bytes(self):
        return canonical_json_bytes(
            {
                "schema": "literate-ai/package-worker-input@1",
                "execution_plan": self.execution_plan.to_dict(),
                "artifact_graph": self.artifact_graph.to_dict(),
                "plan": self.plan.to_dict(),
                "link_results": [
                    {"action_id": action, "result_identity": identity.uri}
                    for action, identity in self.link_results
                ],
            }
        )

    @classmethod
    def admit(cls, content, identity, deadline):
        """Admit descriptors only; referenced LINK proof must still be reopened."""
        deadline.remaining()
        try:
            if (
                not isinstance(content, bytes)
                or len(content) > MAX_ACTION_RECORD_BYTES
                or not isinstance(identity, ContentIdentity)
                or record_identity(content) != identity
            ):
                _invalid()
            doc = json.loads(content)
            if (
                not isinstance(doc, dict)
                or set(doc)
                != {
                    "schema",
                    "execution_plan",
                    "artifact_graph",
                    "plan",
                    "link_results",
                }
                or doc["schema"] != "literate-ai/package-worker-input@1"
                or canonical_json_bytes(doc) != content
                or not isinstance(doc["link_results"], list)
                or not 1 <= len(doc["link_results"]) <= 4096
            ):
                _invalid()
            links = []
            for item in doc["link_results"]:
                if not isinstance(item, dict) or set(item) != {
                    "action_id",
                    "result_identity",
                }:
                    _invalid()
                links.append(
                    (
                        item["action_id"],
                        ContentIdentity.parse_uri(item["result_identity"]),
                    )
                )
            value = cls(
                ComponentExecutionPlan.from_dict(doc["execution_plan"]),
                ArtifactBuildGraph.from_dict(doc["artifact_graph"]),
                PackagePlan.from_dict(doc["plan"]),
                tuple(links),
            )
            execution, graph, plan = (
                value.execution_plan,
                value.artifact_graph,
                value.plan,
            )
            package = next(
                node
                for node in plan_lifecycle_action_dag(
                    execution, worker_ids=("package",)
                )
                if node.kind is LifecycleActionKind.PACKAGE
            )
            revisions = tuple(
                item.component_revision for item in execution.generation_plans
            )
            if (
                tuple(action for action, _ in links) != package.predecessor_ids
                or len({identity for _, identity in links}) != len(links)
                or tuple(item.component_revision for item in graph.manifests)
                != revisions
                or plan.component_lock_identity != execution.component_lock_identity
                or plan.root_component_revision != execution.root_revision
            ):
                _invalid()
            artifacts = tuple(
                item for item in plan.inputs if item.kind is PackageFileKind.ARTIFACT
            )
            if len({item.source_identity for item in artifacts}) != len(artifacts):
                _invalid()
            expected = create_package_plan(
                graph,
                root_component_revision=execution.root_revision,
                component_lock_identity=execution.component_lock_identity,
                target_identity=plan.target_identity,
                root_artifact_identity=plan.root_artifact_identity,
                package_kind=plan.package_kind,
                packager_identity=plan.packager_identity,
                destinations={
                    item.source_identity.uri: item.path for item in artifacts
                },
                entrypoints=plan.entrypoints,
                resource_inputs=tuple(
                    item
                    for item in plan.inputs
                    if item.kind is PackageFileKind.RESOURCE
                ),
                runtime_requirements=plan.runtime_requirements,
                native_library_root=plan.native_library_root,
                native_library_layout=plan.native_library_layout,
            )
            if expected != plan or value.to_bytes() != content:
                _invalid()
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            RecursionError,
            StopIteration,
        ) as exc:
            raise ActionWireError(
                "action_package.input_invalid", "PACKAGE envelope refused"
            ) from exc
        deadline.remaining()
        return value
