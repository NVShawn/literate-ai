"""Canonical historical secondary-access facts; parsing never grants admission."""

import json
from dataclasses import dataclass

from .identity import ContentIdentity, canonical_identity, canonical_json_bytes
from .retained_project_lifecycle import RetainedProjectPlan, _fields

_BASE = {"schema", "plan_identity", "policy_identity"}
_ALLOCATION = _BASE | {
    "request_identity",
    "response_identity",
    "configuration_identity",
    "allocation_identity",
    "worker_identity",
    "remaining_seconds_at_response",
    "acceptance",
}
_EVENT = _BASE | {"allocation_observation_identity", "event"}
_EVENT_FIELDS = {
    "mounted": {"view_identity", "manifest_binding", "readonly", "nosuid"},
    "activated": {"action_id", "account_identity"},
    "deactivated": {"action_id"},
    "closed": {"cleanup_ok"},
}


@dataclass(frozen=True, slots=True)
class RetainedSecondaryAccessEvidence:
    """Immutable canonical bytes, with strict shape and exact contextual binding."""

    content: bytes

    SCHEMA = "literate-ai/retained-secondary-access-evidence@1"

    def __post_init__(self):
        if (
            type(self.content) is not bytes
            or not 0 < len(self.content) <= 16 * 1024 * 1024
        ):
            raise ValueError("retained.secondary.evidence-byte-budget")
        value = json.loads(self.content)
        _fields(
            value,
            _BASE
            | {
                "worker_identity",
                "account_identity",
                "command_observations",
                "allocation_observations",
                "view_observations",
                "cleanup_ok",
            },
        )
        if (
            canonical_json_bytes(value) != self.content
            or value["schema"] != self.SCHEMA
        ):
            raise ValueError("retained.secondary.canonical-evidence-required")
        for key in (
            "plan_identity",
            "policy_identity",
            "worker_identity",
            "account_identity",
        ):
            ContentIdentity.parse_uri(value[key])
        for key, limit in (
            ("command_observations", 100_000),
            ("allocation_observations", 8192),
            ("view_observations", 128),
        ):
            if type(value[key]) is not list or not 1 <= len(value[key]) <= limit:
                raise ValueError("retained.secondary.evidence-observation-budget")
        for identity in value["command_observations"]:
            ContentIdentity.parse_uri(identity)
        requests, allocations, configurations = set(), set(), set()
        inspections = {}
        for index, item in enumerate(value["allocation_observations"]):
            _fields(item, _ALLOCATION)
            if (
                item["schema"] != "literate-ai/retained-allocation-observation@1"
                or item["acceptance"] is not False
                or type(item["remaining_seconds_at_response"]) is not int
                or not 1 <= item["remaining_seconds_at_response"] <= 86400
            ):
                raise ValueError("retained.secondary.allocation-evidence-invalid")
            for key in _ALLOCATION - {
                "schema",
                "acceptance",
                "remaining_seconds_at_response",
            }:
                ContentIdentity.parse_uri(item[key])
            for key in ("plan_identity", "policy_identity", "worker_identity"):
                if item[key] != value[key]:
                    raise ValueError("retained.secondary.allocation-context-mismatch")
            if item["request_identity"] in requests:
                raise ValueError("retained.secondary.repeated-allocation-inspection")
            requests.add(item["request_identity"])
            allocations.add(item["allocation_identity"])
            configurations.add(item["configuration_identity"])
            inspections[canonical_identity(item).uri] = index
        if len(allocations) != 1 or len(configurations) != 1:
            raise ValueError("retained.secondary.allocation-history-changed")
        events = value["view_observations"]
        for item in events:
            if type(item) is not dict or item.get("event") not in _EVENT_FIELDS:
                raise ValueError("retained.secondary.view-event-invalid")
            event = item["event"]
            _fields(item, _EVENT | _EVENT_FIELDS[event])
            if (
                item["schema"] != "literate-ai/retained-readonly-view-observation@1"
                or any(
                    item[key] != value[key]
                    for key in ("plan_identity", "policy_identity")
                )
                or item["allocation_observation_identity"] not in inspections
            ):
                raise ValueError("retained.secondary.view-context-mismatch")
            if event == "mounted":
                for key in ("view_identity", "manifest_binding"):
                    ContentIdentity.parse_uri(item[key])
                if item["readonly"] is not True or item["nosuid"] is not True:
                    raise ValueError("retained.secondary.readonly-evidence-required")
            elif (
                event == "activated"
                and item["account_identity"] != value["account_identity"]
            ):
                raise ValueError("retained.secondary.account-evidence-mismatch")
        if (
            value["cleanup_ok"] is not True
            or events[-1]["event"] != "closed"
            or events[-1]["cleanup_ok"] is not True
            or sum(item["event"] == "closed" for item in events) != 1
        ):
            raise ValueError("retained.secondary.successful-cleanup-required")
        order = [inspections[v["allocation_observation_identity"]] for v in events]
        if order != sorted(order) or order[-1] >= len(inspections) - 1:
            raise ValueError("retained.secondary.inspection-order-or-final-check")

    @classmethod
    def from_dict(cls, value):
        return cls(canonical_json_bytes(value))

    def to_dict(self):
        return json.loads(self.content)

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def require_context(self, plan, *, export_manifest_identity, command_observations):
        if (
            not isinstance(plan, RetainedProjectPlan)
            or plan.profile.secondary_access is None
        ):
            raise TypeError("retained.secondary.evidence-reviewed-plan-required")
        policy = plan.profile.secondary_access
        value = self.to_dict()
        if (
            value["plan_identity"] != plan.identity.uri
            or value["policy_identity"] != policy.identity.uri
            or value["worker_identity"] != policy.worker_identity.uri
            or value["account_identity"] != policy.account.identity.uri
            or value["command_observations"] != [v.uri for v in command_observations]
        ):
            raise ValueError("retained.secondary.evidence-run-mismatch")
        expected = []
        for origin in ("inputs", "exports"):
            for view in policy.views:
                if view.origin == origin:
                    manifest = (
                        plan.manifest_identity
                        if origin == "inputs"
                        else export_manifest_identity
                    )
                    expected.append(
                        (
                            "mounted",
                            canonical_identity(view.to_dict()).uri,
                            canonical_identity(
                                {
                                    "view": view.to_dict(),
                                    "manifest_identity": manifest.uri,
                                }
                            ).uri,
                        )
                    )
        for action in plan.profile.actions:
            if action.action_id in policy.action_ids:
                expected.extend(
                    (("activated", action.action_id), ("deactivated", action.action_id))
                )
        expected.append(("closed",))
        actual = []
        for event in value["view_observations"]:
            name = event["event"]
            if name == "mounted":
                actual.append((name, event["view_identity"], event["manifest_binding"]))
            elif name in {"activated", "deactivated"}:
                actual.append((name, event["action_id"]))
            else:
                actual.append((name,))
        if actual != expected:
            raise ValueError("retained.secondary.evidence-event-sequence-mismatch")
