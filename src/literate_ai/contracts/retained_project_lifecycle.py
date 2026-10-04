"""Explicit retained-origin Standard planning authority (ADR 0048).

These records describe reviewed work. Neither a plan nor successful command exits
constitute lifecycle acceptance, regenerative source authority, or release permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths


def _fields(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Retained lifecycle record has missing or unknown fields")
    return value


def _name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("Retained lifecycle name must be a bounded nonempty string")
    if any(ord(c) < 32 for c in value):
        raise ValueError("Retained lifecycle name contains control characters")
    return value


@dataclass(frozen=True, slots=True)
class RetainedProjectAction:
    """One locked action with explicit inventory and exact dependency edges."""

    action_id: str
    stage: str
    tool: str
    argv: tuple[str, ...]
    requires: tuple[str, ...]
    inventory: tuple[str, ...]
    outputs: tuple[str, ...]
    timeout_seconds: int

    def __post_init__(self) -> None:
        for value in (self.action_id, self.tool):
            _name(value)
        if self.stage not in {
            "acquire",
            "stage",
            "generate",
            "build",
            "test",
            "docs",
            "accept",
        }:
            raise ValueError("Unknown retained lifecycle action stage")
        for values in (self.argv, self.requires, self.inventory, self.outputs):
            if not isinstance(values, tuple) or len(values) > 100_000:
                raise ValueError("Action sequences must be bounded tuples")
            for value in values:
                _name(value)
        if not self.inventory or len(set(self.inventory)) != len(self.inventory):
            raise ValueError("Action must name its exact nonempty inventory")
        if len(set(self.requires)) != len(self.requires):
            raise ValueError("Action dependency edges must be unique")
        canonical_relative_posix_paths(self.outputs, label="retained output")
        if (
            type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 86_400
        ):
            raise ValueError("Action requires a bounded positive timeout")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.action_id,
            "stage": self.stage,
            "tool": self.tool,
            "argv": list(self.argv),
            "requires": list(self.requires),
            "inventory": list(self.inventory),
            "outputs": list(self.outputs),
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, value: object) -> RetainedProjectAction:
        value = _fields(
            value,
            {
                "id",
                "stage",
                "tool",
                "argv",
                "requires",
                "inventory",
                "outputs",
                "timeout_seconds",
            },
        )
        for key in ("argv", "requires", "inventory", "outputs"):
            if not isinstance(value[key], list):
                raise ValueError("Action sequence must be a JSON array")
        return cls(
            value["id"],
            value["stage"],
            value["tool"],
            tuple(value["argv"]),
            tuple(value["requires"]),
            tuple(value["inventory"]),
            tuple(value["outputs"]),
            value["timeout_seconds"],
        )


@dataclass(frozen=True, slots=True)
class RetainedProjectProfile:
    """A complete declarative lifecycle profile, independently locked by its bytes."""

    target: str
    worker_identity: ContentIdentity
    toolchain_identity: ContentIdentity
    dependency_identity: ContentIdentity
    test_policy_identity: ContentIdentity
    release_policy_identity: ContentIdentity
    tools: tuple[tuple[str, str, ContentIdentity], ...]
    actions: tuple[RetainedProjectAction, ...]
    package_inventory: tuple[str, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-project-profile@1"

    def __post_init__(self) -> None:
        _name(self.target)
        for identity in (
            self.worker_identity,
            self.toolchain_identity,
            self.dependency_identity,
            self.test_policy_identity,
            self.release_policy_identity,
        ):
            if not isinstance(identity, ContentIdentity):
                raise TypeError("Profile identities must be typed content identities")
        if (
            not isinstance(self.tools, tuple)
            or not self.tools
            or len(self.tools) > 1024
        ):
            raise ValueError("Profile must declare a bounded exact tool closure")
        names = set()
        for name, path, identity in self.tools:
            _name(name)
            canonical_relative_posix_path(path, label="retained tool")
            if name in names or not isinstance(identity, ContentIdentity):
                raise ValueError("Tools require unique names and exact identities")
            names.add(name)
        if (
            not isinstance(self.actions, tuple)
            or not self.actions
            or len(self.actions) > 100_000
        ):
            raise ValueError("Profile must declare a bounded complete action graph")
        seen: set[str] = set()
        inventories: set[tuple[str, str]] = set()
        stages = set()
        for action in self.actions:
            if not isinstance(action, RetainedProjectAction):
                raise TypeError("Profile actions must be typed")
            if action.action_id in seen or not set(action.requires) <= seen:
                raise ValueError(
                    "Action graph is duplicated, unordered, cyclic or incomplete"
                )
            if action.tool not in names:
                raise ValueError("Action tool is outside the declared exact closure")
            for item in action.inventory:
                key = (action.stage, item)
                if key in inventories:
                    raise ValueError("Lifecycle inventory member has multiple owners")
                inventories.add(key)
            stages.add(action.stage)
            seen.add(action.action_id)
        if not {"build", "test", "docs", "accept"} <= stages:
            raise ValueError(
                "Build, tests, docs and independent acceptance are required"
            )
        ancestors: dict[str, set[str]] = {}
        build_ids = {
            action.action_id for action in self.actions if action.stage == "build"
        }
        executed_ids = {
            action.action_id for action in self.actions if action.stage != "accept"
        }
        for action in self.actions:
            prior = set(action.requires)
            for dependency in action.requires:
                prior.update(ancestors[dependency])
            ancestors[action.action_id] = prior
            if action.stage in {"test", "docs"} and not build_ids <= prior:
                raise ValueError(
                    "Tests and docs must follow the complete declared build"
                )
            if action.stage == "accept" and not executed_ids <= prior:
                raise ValueError(
                    "Acceptance must depend on every declared execution stage"
                )
        if not isinstance(self.package_inventory, tuple) or not self.package_inventory:
            raise ValueError(
                "Retained execution requires an explicit package inventory"
            )
        canonical_relative_posix_paths(
            self.package_inventory, label="retained package inventory"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "authority_mode": "retained-project",
            "target": self.target,
            "worker_identity": self.worker_identity.uri,
            "toolchain_identity": self.toolchain_identity.uri,
            "dependency_identity": self.dependency_identity.uri,
            "test_policy_identity": self.test_policy_identity.uri,
            "release_policy_identity": self.release_policy_identity.uri,
            "tools": [
                {"name": name, "path": path, "identity": identity.uri}
                for name, path, identity in self.tools
            ],
            "actions": [action.to_dict() for action in self.actions],
            "package_inventory": list(self.package_inventory),
        }

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> RetainedProjectProfile:
        value = _fields(
            value,
            {
                "schema",
                "authority_mode",
                "target",
                "worker_identity",
                "toolchain_identity",
                "dependency_identity",
                "test_policy_identity",
                "release_policy_identity",
                "tools",
                "actions",
                "package_inventory",
            },
        )
        if (
            value["schema"] != cls.SCHEMA
            or value["authority_mode"] != "retained-project"
        ):
            raise ValueError(
                "Explicit retained-project profile schema and authority mode required"
            )
        for key in ("tools", "actions", "package_inventory"):
            if not isinstance(value[key], list):
                raise ValueError("Profile collections must be JSON arrays")
        tools = []
        for tool in value["tools"]:
            tool = _fields(tool, {"name", "path", "identity"})
            tools.append(
                (
                    tool["name"],
                    tool["path"],
                    ContentIdentity.parse_uri(tool["identity"]),
                )
            )
        return cls(
            value["target"],
            *(
                ContentIdentity.parse_uri(value[key])
                for key in (
                    "worker_identity",
                    "toolchain_identity",
                    "dependency_identity",
                    "test_policy_identity",
                    "release_policy_identity",
                )
            ),
            tuple(tools),
            tuple(
                RetainedProjectAction.from_dict(action) for action in value["actions"]
            ),
            tuple(value["package_inventory"]),
        )


@dataclass(frozen=True, slots=True)
class RetainedProjectPlan:
    project_id: str
    project_identity: ContentIdentity
    component_locks: tuple[ContentIdentity, ...]
    conversion_identity: ContentIdentity
    standard_binding_identity: ContentIdentity
    manifest_identity: ContentIdentity
    profile: RetainedProjectProfile

    def __post_init__(self) -> None:
        _name(self.project_id)
        if not isinstance(self.component_locks, tuple) or not self.component_locks:
            raise ValueError("Retained planning requires current Component locks")
        for identity in (
            *self.component_locks,
            self.project_identity,
            self.conversion_identity,
            self.standard_binding_identity,
            self.manifest_identity,
        ):
            if not isinstance(identity, ContentIdentity):
                raise TypeError("Retained plans require typed identities")
        if len(set(self.component_locks)) != len(self.component_locks):
            raise ValueError("Retained Component locks must be unique")
        if not isinstance(self.profile, RetainedProjectProfile):
            raise TypeError("Retained plans require a typed command profile")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/retained-project-plan@1",
            "authority_mode": "retained-project",
            "project_id": self.project_id,
            "project_identity": self.project_identity.uri,
            "component_locks": [item.uri for item in self.component_locks],
            "conversion_identity": self.conversion_identity.uri,
            "standard_binding_identity": self.standard_binding_identity.uri,
            "manifest_identity": self.manifest_identity.uri,
            "profile": self.profile.to_dict(),
            "admitted": False,
            "source_authority": "original",
        }

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def require_authorization(self, authorization: str | None) -> None:
        if authorization != self.identity.uri:
            raise ValueError(
                "Authorize the exact current retained-project plan identity"
            )
