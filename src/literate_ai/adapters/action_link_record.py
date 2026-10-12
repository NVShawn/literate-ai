"""Bounded per-Component LINK input; proof reopening remains a worker duty."""

import json
from dataclasses import dataclass

from literate_ai.adapters.action_accept_record import AcceptWorkerInput
from literate_ai.adapters.action_accept_result_record import AcceptWorkerResult
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.application.artifact_graph import realize_manifest
from literate_ai.contracts import ContentIdentity, canonical_json_bytes


def _invalid():
    raise ActionWireError("action_link.input_invalid", "LINK input custody refused")


@dataclass(frozen=True)
class LinkWorkerInput:
    acceptance_input: AcceptWorkerInput
    acceptance_result: AcceptWorkerResult
    dependency_links: tuple[tuple[str, ContentIdentity], ...] = ()

    @property
    def build(self):
        return self.acceptance_input.execution_input.build_input

    @property
    def manifest(self):
        return realize_manifest(
            self.build.plan.manifest, self.acceptance_result.evidence.build.exports
        )

    def to_bytes(self):
        return canonical_json_bytes(
            {
                "schema": "literate-ai/link-worker-input@1",
                "acceptance_input": json.loads(self.acceptance_input.to_bytes()),
                "acceptance_result": json.loads(self.acceptance_result.to_bytes()),
                "dependency_links": [
                    {"action_id": action, "result_identity": identity.uri}
                    for action, identity in self.dependency_links
                ],
            }
        )

    @classmethod
    def admit(cls, content, identity, deadline):
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
                    "acceptance_input",
                    "acceptance_result",
                    "dependency_links",
                }
                or doc["schema"] != "literate-ai/link-worker-input@1"
                or canonical_json_bytes(doc) != content
                or not isinstance(doc["dependency_links"], list)
                or len(doc["dependency_links"]) > 4096
            ):
                _invalid()
            raw_input = canonical_json_bytes(doc["acceptance_input"])
            input_identity = record_identity(raw_input)
            accepted_input = AcceptWorkerInput.admit(
                raw_input, input_identity, deadline
            )
            raw_result = canonical_json_bytes(doc["acceptance_result"])
            accepted_result = AcceptWorkerResult.admit(
                raw_result,
                record_identity(raw_result),
                input_record=raw_input,
                input_identity=input_identity,
                deadline=deadline,
            )
            dependencies = []
            for item in doc["dependency_links"]:
                if not isinstance(item, dict) or set(item) != {
                    "action_id",
                    "result_identity",
                }:
                    _invalid()
                dependencies.append(
                    (
                        item["action_id"],
                        ContentIdentity.parse_uri(item["result_identity"]),
                    )
                )
            value = cls(accepted_input, accepted_result, tuple(dependencies))
            action_id = lifecycle_action_id(
                value.build.plan.component_revision, LifecycleActionKind.LINK
            )
            action = next(
                node
                for node in plan_lifecycle_action_dag(
                    value.build.execution_plan,
                    worker_ids=("link",),
                )
                if node.action_id == action_id
            )
            own_accept = lifecycle_action_id(
                value.build.plan.component_revision, LifecycleActionKind.ACCEPT
            )
            expected = tuple(
                item for item in action.predecessor_ids if item != own_accept
            )
            if tuple(action for action, _ in dependencies) != expected:
                _invalid()
            _ = value.manifest
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            RecursionError,
            StopIteration,
        ) as exc:
            raise ActionWireError(
                "action_link.input_invalid", "LINK envelope refused"
            ) from exc
        deadline.remaining()
        return value
