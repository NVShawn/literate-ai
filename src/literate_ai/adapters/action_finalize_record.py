"""Closed FINALIZE intent binds lock, component plans and exact PACKAGE custody."""

import json
from dataclasses import dataclass

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_package_record import PackageWorkerInput
from literate_ai.application.artifact_graph import realize_manifest
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildPlan,
    StandardProjectBuildPlan,
    StandardProjectLifecycleError,
)
from literate_ai.contracts import (
    ComponentAuthoring,
    ComponentLock,
    ContentIdentity,
    canonical_json_bytes,
)


def _invalid():
    raise ActionWireError("action_finalize.input_invalid", "FINALIZE intent refused")


@dataclass(frozen=True)
class FinalizeWorkerInput:
    component_lock: ComponentLock
    project_plan: StandardProjectBuildPlan
    package_input: PackageWorkerInput
    package_result_identity: ContentIdentity

    def to_bytes(self):
        project = self.project_plan
        return canonical_json_bytes(
            {
                "schema": "literate-ai/finalize-worker-input@1",
                "component_lock": self.component_lock.to_dict(),
                "authorings": [
                    item.to_dict() for item in self.component_lock.authorings
                ],
                "project_plan": {
                    "execution_plan_identity": project.execution_plan_identity.uri,
                    "components": [
                        item.to_dict() for item in self.project_plan.components
                    ],
                },
                "package_input": json.loads(self.package_input.to_bytes()),
                "package_result_identity": self.package_result_identity.uri,
            }
        )

    @classmethod
    def admit(cls, content, identity, deadline):
        """Admit descriptors only; proof and execution grants remain required."""
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
                    "component_lock",
                    "authorings",
                    "project_plan",
                    "package_input",
                    "package_result_identity",
                }
                or doc["schema"] != "literate-ai/finalize-worker-input@1"
                or canonical_json_bytes(doc) != content
                or not isinstance(doc["authorings"], list)
                or not 1 <= len(doc["authorings"]) <= 4096
            ):
                _invalid()
            project = doc["project_plan"]
            if (
                not isinstance(project, dict)
                or set(project) != {"execution_plan_identity", "components"}
                or not isinstance(project["components"], list)
                or not 1 <= len(project["components"]) <= 4096
            ):
                _invalid()
            package_raw = canonical_json_bytes(doc["package_input"])
            value = cls(
                ComponentLock.from_dict(
                    doc["component_lock"],
                    authorings=tuple(
                        ComponentAuthoring.from_dict(item) for item in doc["authorings"]
                    ),
                ),
                StandardProjectBuildPlan(
                    ContentIdentity.parse_uri(project["execution_plan_identity"]),
                    tuple(
                        StandardComponentBuildPlan.from_dict(item)
                        for item in project["components"]
                    ),
                ),
                PackageWorkerInput.admit(
                    package_raw, record_identity(package_raw), deadline
                ),
                ContentIdentity.parse_uri(doc["package_result_identity"]),
            )
            execution = value.package_input.execution_plan
            if (
                value.component_lock.identity != execution.component_lock_identity
                or value.component_lock.root_revision != execution.root_revision
                or value.project_plan.execution_plan_identity != execution.identity
                or tuple(
                    item.component_revision for item in value.project_plan.components
                )
                != tuple(item.component_revision for item in execution.generation_plans)
            ):
                _invalid()
            manifests = value.package_input.artifact_graph.manifests
            for plan, manifest in zip(
                value.project_plan.components, manifests, strict=True
            ):
                if realize_manifest(plan.manifest, manifest.exports) != manifest:
                    _invalid()
            if value.to_bytes() != content:
                _invalid()
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            RecursionError,
            StandardProjectLifecycleError,
        ) as exc:
            raise ActionWireError(
                "action_finalize.input_invalid", "FINALIZE envelope refused"
            ) from exc
        deadline.remaining()
        return value
