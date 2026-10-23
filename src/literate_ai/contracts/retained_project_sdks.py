"""Reviewed SDK product placement within an original-authoritative input tree."""

from dataclasses import dataclass
from typing import ClassVar

from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_paths
from .retained_project_lifecycle import _fields


@dataclass(frozen=True, slots=True)
class RetainedProjectSdkBinding:
    component_lock_identity: ContentIdentity
    input_identity: ContentIdentity
    prefix: str

    def __post_init__(self):
        if any(
            not isinstance(value, ContentIdentity)
            for value in (self.component_lock_identity, self.input_identity)
        ):
            raise TypeError("retained.sdk.typed-binding-required")
        canonical_relative_posix_paths((self.prefix,), label="retained SDK prefix")

    def to_dict(self):
        return {
            "component_lock_identity": self.component_lock_identity.uri,
            "input_identity": self.input_identity.uri,
            "prefix": self.prefix,
        }

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"component_lock_identity", "input_identity", "prefix"})
        return cls(
            ContentIdentity.parse_uri(value["component_lock_identity"]),
            ContentIdentity.parse_uri(value["input_identity"]),
            value["prefix"],
        )


@dataclass(frozen=True, slots=True)
class RetainedProjectSdkPolicy:
    bindings: tuple[RetainedProjectSdkBinding, ...]
    SCHEMA: ClassVar[str] = "literate-ai/retained-project-sdk-policy@1"

    def __post_init__(self):
        if (
            type(self.bindings) is not tuple
            or len(self.bindings) > 100_000
            or any(not isinstance(v, RetainedProjectSdkBinding) for v in self.bindings)
        ):
            raise TypeError("retained.sdk.typed-bindings-required")
        keys = tuple(
            (v.component_lock_identity.uri, v.input_identity.uri) for v in self.bindings
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("retained.sdk.unique-ordered-bindings-required")
        canonical_relative_posix_paths(
            (v.prefix for v in self.bindings), label="retained SDK placement"
        )

    def require_locks(self, identities):
        if not {v.component_lock_identity for v in self.bindings} <= set(identities):
            raise ValueError("retained.sdk.foreign-component-lock")

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "bindings": [v.to_dict() for v in self.bindings],
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"schema", "bindings"})
        if (
            value["schema"] != cls.SCHEMA
            or not isinstance(value["bindings"], list)
            or len(value["bindings"]) > 100_000
        ):
            raise ValueError("retained.sdk.policy-invalid")
        return cls(
            tuple(RetainedProjectSdkBinding.from_dict(v) for v in value["bindings"])
        )


@dataclass(frozen=True, slots=True)
class RetainedProjectSdkSelectionBinding:
    """Exact reproducible SDK expectation; never a transported producer grant."""

    component_lock_identity: ContentIdentity
    selection_identity: ContentIdentity
    snapshot_identity: ContentIdentity
    prefix: str

    def __post_init__(self):
        if any(
            not isinstance(value, ContentIdentity)
            for value in (
                self.component_lock_identity,
                self.selection_identity,
                self.snapshot_identity,
            )
        ):
            raise TypeError("retained.sdk.typed-selection-binding-required")
        canonical_relative_posix_paths((self.prefix,), label="retained SDK prefix")

    def to_dict(self):
        return {
            "component_lock_identity": self.component_lock_identity.uri,
            "selection_identity": self.selection_identity.uri,
            "snapshot_identity": self.snapshot_identity.uri,
            "prefix": self.prefix,
        }

    @classmethod
    def from_dict(cls, value):
        value = _fields(
            value,
            {
                "component_lock_identity",
                "selection_identity",
                "snapshot_identity",
                "prefix",
            },
        )
        return cls(
            ContentIdentity.parse_uri(value["component_lock_identity"]),
            ContentIdentity.parse_uri(value["selection_identity"]),
            ContentIdentity.parse_uri(value["snapshot_identity"]),
            value["prefix"],
        )


@dataclass(frozen=True, slots=True)
class RetainedProjectSdkSelectionPolicy:
    """Review stable content, then require matching live producers at execution."""

    bindings: tuple[RetainedProjectSdkSelectionBinding, ...]
    SCHEMA: ClassVar[str] = "literate-ai/retained-project-sdk-selection-policy@1"

    def __post_init__(self):
        if (
            type(self.bindings) is not tuple
            or len(self.bindings) > 100_000
            or any(
                not isinstance(v, RetainedProjectSdkSelectionBinding)
                for v in self.bindings
            )
        ):
            raise TypeError("retained.sdk.typed-selection-bindings-required")
        # One selected recipe has exactly one snapshot and one placement.
        keys = tuple(
            (v.component_lock_identity.uri, v.selection_identity.uri)
            for v in self.bindings
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("retained.sdk.unique-ordered-selections-required")
        canonical_relative_posix_paths(
            (v.prefix for v in self.bindings), label="retained SDK placement"
        )

    def require_locks(self, identities):
        if not {v.component_lock_identity for v in self.bindings} <= set(identities):
            raise ValueError("retained.sdk.foreign-component-lock")

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "bindings": [v.to_dict() for v in self.bindings],
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"schema", "bindings"})
        if (
            value["schema"] != cls.SCHEMA
            or not isinstance(value["bindings"], list)
            or len(value["bindings"]) > 100_000
        ):
            raise ValueError("retained.sdk.selection-policy-invalid")
        return cls(
            tuple(
                RetainedProjectSdkSelectionBinding.from_dict(v)
                for v in value["bindings"]
            )
        )


def read_retained_sdk_policy_document(value):
    """Select an explicit contract without reinterpreting legacy identities."""
    if (
        isinstance(value, dict)
        and value.get("schema") == RetainedProjectSdkSelectionPolicy.SCHEMA
    ):
        return RetainedProjectSdkSelectionPolicy.from_dict(value)
    return RetainedProjectSdkPolicy.from_dict(value)
