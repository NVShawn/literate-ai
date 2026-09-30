"""Exact contracts for non-Component Git repository source dependencies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    fields,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from .capabilities import DependencyKind
from .identity import (
    SCHEMA_PREFIX,
    ContentIdentity,
    ContentReference,
    contract_identity,
)

REPOSITORY_SOURCE_DEPENDENCY_CONTENT_KIND = "repository-source-dependency"
REPOSITORY_SOURCE_DEPENDENCY_SCHEMA = f"{SCHEMA_PREFIX}repository-source-dependency"
REPOSITORY_SOURCE_LOCK_SCHEMA = f"{SCHEMA_PREFIX}repository-source-lock"
REPOSITORY_BUILD_PLAN_SCHEMA = f"{SCHEMA_PREFIX}repository-build-plan"
REPOSITORY_SOURCE_ADMISSION_SCHEMA = f"{SCHEMA_PREFIX}repository-source-admission"

_DEPENDENCY_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,126}$")
_PORTABLE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_SHELL_PROGRAMS = frozenset(
    {
        "ash",
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "powershell.exe",
        "pwsh",
        "sh",
        "tcsh",
        "zsh",
    }
)


def _dependency_id(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=128)
    if not _DEPENDENCY_ID.fullmatch(raw):
        fail(path, "must be a lower-case portable dependency ID")
    return raw


def _git_commit(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=64)
    if not _COMMIT.fullmatch(raw):
        fail(path, "must be a full lower-case Git object ID (SHA-1 or SHA-256)")
    return raw


def _git_branch(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=255)
    if (
        not _PORTABLE_BRANCH.fullmatch(raw)
        or ".." in raw
        or "//" in raw
        or "@{" in raw
        or raw.endswith(("/", ".", ".lock"))
        or any(character in raw for character in "~^:?*[\\")
    ):
        fail(path, "must be a portable Git branch name")
    return raw


def _repository_url(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=4096)
    if "\\" in raw or any(ord(character) < 0x20 for character in raw):
        fail(path, "must be a normalized repository URL")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
        fail(path, "must use an absolute https:// or ssh:// repository URL")
    if parsed.fragment or parsed.query:
        fail(path, "must not contain a query or fragment; use revision_selector")
    if parsed.password is not None or (
        parsed.scheme == "https" and parsed.username is not None
    ):
        fail(path, "must not contain credentials")
    if not parsed.path or parsed.path == "/":
        fail(path, "must identify a repository path")
    return raw


def _portable_path(value: Any, path: str, *, root: bool = False) -> str:
    raw = string_value(value, path, max_length=1024)
    if "\x00" in raw:
        fail(path, "must not contain NUL")
    if root and raw == ".":
        return raw
    if raw.startswith("/") or "\\" in raw:
        fail(path, "must be a relative POSIX path")
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        fail(path, "must be normalized without empty, '.' or '..' segments")
    return raw


class RepositoryRevisionKind(StrEnum):
    COMMIT = "commit"
    BRANCH = "branch"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class RepositoryRevisionSelector:
    """A requested Git ref; only ``commit`` is immutable before resolution."""

    kind: RepositoryRevisionKind
    value: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RepositoryRevisionKind):
            fail("RepositoryRevisionSelector.kind", "must be a RepositoryRevisionKind")
        if self.kind is RepositoryRevisionKind.DEFAULT:
            if self.value is not None:
                fail(
                    "RepositoryRevisionSelector.value",
                    "must be null for the repository default",
                )
        elif self.kind is RepositoryRevisionKind.COMMIT:
            _git_commit(self.value, "RepositoryRevisionSelector.value")
        else:
            _git_branch(self.value, "RepositoryRevisionSelector.value")

    @property
    def immutable(self) -> bool:
        return self.kind is RepositoryRevisionKind.COMMIT

    def to_dict(self) -> dict[str, str | None]:
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryRevisionSelector"
    ) -> RepositoryRevisionSelector:
        data = fields(value, path=path, required=frozenset({"kind", "value"}))
        kind = enum_value(RepositoryRevisionKind, data["kind"], f"{path}.kind")
        raw = data["value"]
        if kind is RepositoryRevisionKind.DEFAULT:
            if raw is not None:
                fail(f"{path}.value", "must be null for the repository default")
            selected: str | None = None
        elif kind is RepositoryRevisionKind.COMMIT:
            selected = _git_commit(raw, f"{path}.value")
        else:
            selected = _git_branch(raw, f"{path}.value")
        return cls(kind, selected)


@dataclass(frozen=True, slots=True)
class RepositorySourceDependency:
    """A source dependency that deliberately does not qualify as a Component."""

    dependency_id: str
    repository_url: str
    revision_selector: RepositoryRevisionSelector
    dependency_kind: DependencyKind
    optional: bool = False
    integration_contract: ContentReference | None = None

    SCHEMA: ClassVar[str] = REPOSITORY_SOURCE_DEPENDENCY_SCHEMA

    def __post_init__(self) -> None:
        _dependency_id(self.dependency_id, "RepositorySourceDependency.dependency_id")
        _repository_url(
            self.repository_url, "RepositorySourceDependency.repository_url"
        )
        if not isinstance(self.revision_selector, RepositoryRevisionSelector):
            fail(
                "RepositorySourceDependency.revision_selector",
                "must be a RepositoryRevisionSelector",
            )
        if not isinstance(self.dependency_kind, DependencyKind):
            fail(
                "RepositorySourceDependency.dependency_kind",
                "must be a DependencyKind",
            )
        if not isinstance(self.optional, bool):
            fail("RepositorySourceDependency.optional", "must be a boolean")
        if not isinstance(self.integration_contract, (ContentReference, type(None))):
            fail(
                "RepositorySourceDependency.integration_contract",
                "must be a ContentReference or null",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "dependency_id": self.dependency_id,
            "repository_url": self.repository_url,
            "revision_selector": self.revision_selector.to_dict(),
            "dependency_kind": self.dependency_kind.value,
            "optional": self.optional,
            "integration_contract": (
                None
                if self.integration_contract is None
                else self.integration_contract.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositorySourceDependency"
    ) -> RepositorySourceDependency:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "dependency_id",
                    "repository_url",
                    "revision_selector",
                    "dependency_kind",
                    "optional",
                    "integration_contract",
                }
            ),
        )
        integration = data["integration_contract"]
        return cls(
            dependency_id=_dependency_id(
                data["dependency_id"], f"{path}.dependency_id"
            ),
            repository_url=_repository_url(
                data["repository_url"], f"{path}.repository_url"
            ),
            revision_selector=RepositoryRevisionSelector.from_dict(
                data["revision_selector"], path=f"{path}.revision_selector"
            ),
            dependency_kind=enum_value(
                DependencyKind, data["dependency_kind"], f"{path}.dependency_kind"
            ),
            optional=bool_value(data["optional"], f"{path}.optional"),
            integration_contract=(
                None
                if integration is None
                else ContentReference.from_dict(
                    integration, path=f"{path}.integration_contract"
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositorySourceLock:
    """One selector resolved to exact, target-neutral source bytes."""

    dependency: RepositorySourceDependency
    resolved_commit: str
    source_snapshot: ContentIdentity
    source_tree: ContentIdentity
    resolver: ContentIdentity

    SCHEMA: ClassVar[str] = REPOSITORY_SOURCE_LOCK_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.dependency, RepositorySourceDependency):
            fail("RepositorySourceLock.dependency", "must be a repository dependency")
        commit = _git_commit(
            self.resolved_commit, "RepositorySourceLock.resolved_commit"
        )
        selector = self.dependency.revision_selector
        if selector.kind is RepositoryRevisionKind.COMMIT and selector.value != commit:
            fail(
                "RepositorySourceLock.resolved_commit",
                "must equal the requested exact commit",
            )
        for name in (
            "source_snapshot",
            "source_tree",
            "resolver",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"RepositorySourceLock.{name}", "must be a ContentIdentity")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "dependency": self.dependency.to_dict(),
            "resolved_commit": self.resolved_commit,
            "source_snapshot": self.source_snapshot.to_dict(),
            "source_tree": self.source_tree.to_dict(),
            "resolver": self.resolver.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositorySourceLock"
    ) -> RepositorySourceLock:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "dependency",
                    "resolved_commit",
                    "source_snapshot",
                    "source_tree",
                    "resolver",
                }
            ),
        )
        return cls(
            dependency=RepositorySourceDependency.from_dict(
                data["dependency"], path=f"{path}.dependency"
            ),
            resolved_commit=_git_commit(
                data["resolved_commit"], f"{path}.resolved_commit"
            ),
            source_snapshot=ContentIdentity.from_dict(
                data["source_snapshot"], path=f"{path}.source_snapshot"
            ),
            source_tree=ContentIdentity.from_dict(
                data["source_tree"], path=f"{path}.source_tree"
            ),
            resolver=ContentIdentity.from_dict(
                data["resolver"], path=f"{path}.resolver"
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositoryBuildEnvironment:
    name: str
    value: str

    def __post_init__(self) -> None:
        if not _ENVIRONMENT_NAME.fullmatch(self.name):
            fail(
                "RepositoryBuildEnvironment.name",
                "must be a portable upper-case environment name",
            )
        string_value(
            self.value,
            "RepositoryBuildEnvironment.value",
            nonempty=False,
            max_length=4096,
        )
        if "\x00" in self.value:
            fail("RepositoryBuildEnvironment.value", "must not contain NUL")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryBuildEnvironment"
    ) -> RepositoryBuildEnvironment:
        data = fields(value, path=path, required=frozenset({"name", "value"}))
        return cls(
            string_value(data["name"], f"{path}.name", max_length=127),
            string_value(
                data["value"], f"{path}.value", nonempty=False, max_length=4096
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositoryBuildCommand:
    step_id: str
    argv: tuple[str, ...]
    working_directory: str = "."
    environment: tuple[RepositoryBuildEnvironment, ...] = ()
    network: bool = False

    def __post_init__(self) -> None:
        string_value(self.step_id, "RepositoryBuildCommand.step_id", max_length=128)
        if "\x00" in self.step_id:
            fail("RepositoryBuildCommand.step_id", "must not contain NUL")
        if not self.argv or len(self.argv) > 128:
            fail("RepositoryBuildCommand.argv", "must contain 1 to 128 arguments")
        for index, argument in enumerate(self.argv):
            string_value(
                argument,
                f"RepositoryBuildCommand.argv[{index}]",
                max_length=8192,
            )
            if "\x00" in argument:
                fail(f"RepositoryBuildCommand.argv[{index}]", "must not contain NUL")
        program = self.argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if program in _SHELL_PROGRAMS:
            fail(
                "RepositoryBuildCommand.argv[0]",
                "must invoke a build tool directly, not a command shell",
            )
        _portable_path(
            self.working_directory,
            "RepositoryBuildCommand.working_directory",
            root=True,
        )
        if len(self.environment) > 128:
            fail(
                "RepositoryBuildCommand.environment",
                "must contain at most 128 entries",
            )
        unique(
            tuple(item.name for item in self.environment),
            "RepositoryBuildCommand.environment",
            "environment names",
        )
        if not isinstance(self.network, bool):
            fail("RepositoryBuildCommand.network", "must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "argv": list(self.argv),
            "working_directory": self.working_directory,
            "environment": [item.to_dict() for item in self.environment],
            "network": self.network,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryBuildCommand"
    ) -> RepositoryBuildCommand:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {"step_id", "argv", "working_directory", "environment", "network"}
            ),
        )
        return cls(
            step_id=string_value(data["step_id"], f"{path}.step_id", max_length=128),
            argv=string_tuple(data["argv"], f"{path}.argv"),
            working_directory=_portable_path(
                data["working_directory"], f"{path}.working_directory", root=True
            ),
            environment=parse_tuple(
                data["environment"],
                f"{path}.environment",
                RepositoryBuildEnvironment.from_dict,
            ),
            network=bool_value(data["network"], f"{path}.network"),
        )


@dataclass(frozen=True, slots=True)
class RepositoryBuildOutput:
    """Content identity observed at one portable path declared by a build plan."""

    path: str
    identity: ContentIdentity

    def __post_init__(self) -> None:
        _portable_path(self.path, "RepositoryBuildOutput.path")
        if not isinstance(self.identity, ContentIdentity):
            fail("RepositoryBuildOutput.identity", "must be a ContentIdentity")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "identity": self.identity.to_dict()}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryBuildOutput"
    ) -> RepositoryBuildOutput:
        data = fields(value, path=path, required=frozenset({"path", "identity"}))
        return cls(
            path=_portable_path(data["path"], f"{path}.path"),
            identity=ContentIdentity.from_dict(
                data["identity"], path=f"{path}.identity"
            ),
        )


def _validate_expected_outputs(values: tuple[str, ...], path: str) -> None:
    if not values:
        fail(path, "must not be empty")
    if len(values) > 256:
        fail(path, "must contain at most 256 entries")
    unique(values, path, "output paths")
    for index, output in enumerate(values):
        _portable_path(output, f"{path}[{index}]")
    if values != tuple(sorted(values)):
        fail(path, "must be sorted lexicographically by portable output path")


def _validate_build_outputs(
    expected_outputs: tuple[str, ...],
    build_outputs: tuple[RepositoryBuildOutput, ...],
    path: str,
) -> None:
    _validate_expected_outputs(expected_outputs, f"{path}.expected_outputs")
    if len(build_outputs) > 256:
        fail(f"{path}.build_outputs", "must contain at most 256 entries")
    if any(not isinstance(item, RepositoryBuildOutput) for item in build_outputs):
        fail(
            f"{path}.build_outputs",
            "must contain RepositoryBuildOutput values",
        )
    output_paths = tuple(item.path for item in build_outputs)
    unique(output_paths, f"{path}.build_outputs", "output paths")
    if output_paths != expected_outputs:
        fail(
            f"{path}.build_outputs",
            "paths must exactly match expected_outputs in canonical order",
        )


@dataclass(frozen=True, slots=True)
class RepositoryBuildPlan:
    source_lock: ContentIdentity
    effective_revision: ContentIdentity
    flavor_set: ContentIdentity
    evidence: tuple[ContentIdentity, ...]
    model_decision: ContentIdentity
    toolchains: tuple[ContentIdentity, ...]
    commands: tuple[RepositoryBuildCommand, ...]
    expected_outputs: tuple[str, ...]

    SCHEMA: ClassVar[str] = REPOSITORY_BUILD_PLAN_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "source_lock",
            "effective_revision",
            "flavor_set",
            "model_decision",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"RepositoryBuildPlan.{name}", "must be a ContentIdentity")
        if not self.evidence:
            fail("RepositoryBuildPlan.evidence", "must not be empty")
        if len(self.evidence) > 256:
            fail("RepositoryBuildPlan.evidence", "must contain at most 256 entries")
        if not self.toolchains:
            fail("RepositoryBuildPlan.toolchains", "must not be empty")
        if len(self.toolchains) > 256:
            fail("RepositoryBuildPlan.toolchains", "must contain at most 256 entries")
        if not self.commands:
            fail("RepositoryBuildPlan.commands", "must not be empty")
        if len(self.commands) > 64:
            fail("RepositoryBuildPlan.commands", "must contain at most 64 entries")
        for name in ("evidence", "toolchains"):
            values = getattr(self, name)
            if any(not isinstance(item, ContentIdentity) for item in values):
                fail(
                    f"RepositoryBuildPlan.{name}",
                    "must contain ContentIdentity values",
                )
            unique(
                tuple(item.uri for item in values),
                f"RepositoryBuildPlan.{name}",
                "content identities",
            )
        unique(
            tuple(item.step_id for item in self.commands),
            "RepositoryBuildPlan.commands",
            "step IDs",
        )
        _validate_expected_outputs(
            self.expected_outputs, "RepositoryBuildPlan.expected_outputs"
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "source_lock": self.source_lock.to_dict(),
            "effective_revision": self.effective_revision.to_dict(),
            "flavor_set": self.flavor_set.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "model_decision": self.model_decision.to_dict(),
            "toolchains": [item.to_dict() for item in self.toolchains],
            "commands": [item.to_dict() for item in self.commands],
            "expected_outputs": list(self.expected_outputs),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryBuildPlan"
    ) -> RepositoryBuildPlan:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "source_lock",
                    "effective_revision",
                    "flavor_set",
                    "evidence",
                    "model_decision",
                    "toolchains",
                    "commands",
                    "expected_outputs",
                }
            ),
        )
        return cls(
            source_lock=ContentIdentity.from_dict(
                data["source_lock"], path=f"{path}.source_lock"
            ),
            effective_revision=ContentIdentity.from_dict(
                data["effective_revision"], path=f"{path}.effective_revision"
            ),
            flavor_set=ContentIdentity.from_dict(
                data["flavor_set"], path=f"{path}.flavor_set"
            ),
            evidence=parse_tuple(
                data["evidence"], f"{path}.evidence", ContentIdentity.from_dict
            ),
            model_decision=ContentIdentity.from_dict(
                data["model_decision"], path=f"{path}.model_decision"
            ),
            toolchains=parse_tuple(
                data["toolchains"], f"{path}.toolchains", ContentIdentity.from_dict
            ),
            commands=parse_tuple(
                data["commands"], f"{path}.commands", RepositoryBuildCommand.from_dict
            ),
            expected_outputs=string_tuple(
                data["expected_outputs"], f"{path}.expected_outputs"
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositorySourceAdmission:
    """Passing, authorized proof required before source becomes cache-ready."""

    dependency_id: str
    source_lock: ContentIdentity
    source_snapshot: ContentIdentity
    source_tree: ContentIdentity
    effective_revision: ContentIdentity
    flavor_set: ContentIdentity
    index_binding: ContentIdentity
    build_plan: ContentIdentity
    classification: ContentIdentity
    authorization: ContentIdentity
    build_result: ContentIdentity
    expected_outputs: tuple[str, ...]
    build_outputs: tuple[RepositoryBuildOutput, ...]
    cache_key: ContentIdentity

    SCHEMA: ClassVar[str] = REPOSITORY_SOURCE_ADMISSION_SCHEMA

    def __post_init__(self) -> None:
        _dependency_id(self.dependency_id, "RepositorySourceAdmission.dependency_id")
        for name in (
            "source_lock",
            "source_snapshot",
            "source_tree",
            "effective_revision",
            "flavor_set",
            "index_binding",
            "build_plan",
            "classification",
            "authorization",
            "build_result",
            "cache_key",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"RepositorySourceAdmission.{name}", "must be a ContentIdentity")
        _validate_build_outputs(
            self.expected_outputs,
            self.build_outputs,
            "RepositorySourceAdmission",
        )
        expected = self.compute_cache_key(
            self.dependency_id,
            self.effective_revision,
            self.flavor_set,
            self.source_lock,
        )
        if self.cache_key != expected:
            fail(
                "RepositorySourceAdmission.cache_key",
                "does not bind the dependency, target, and exact source lock",
            )

    @staticmethod
    def compute_cache_key(
        dependency_id: str,
        effective_revision: ContentIdentity,
        flavor_set: ContentIdentity,
        source_lock: ContentIdentity,
    ) -> ContentIdentity:
        from .identity import canonical_identity

        return canonical_identity(
            {
                "schema": f"{SCHEMA_PREFIX}repository-source-cache-key",
                "dependency_id": dependency_id,
                "effective_revision": effective_revision.to_dict(),
                "flavor_set": flavor_set.to_dict(),
                "source_lock": source_lock.to_dict(),
            }
        )

    @classmethod
    def create(
        cls,
        *,
        dependency_id: str,
        source_lock: ContentIdentity,
        source_snapshot: ContentIdentity,
        source_tree: ContentIdentity,
        effective_revision: ContentIdentity,
        flavor_set: ContentIdentity,
        index_binding: ContentIdentity,
        build_plan: ContentIdentity,
        classification: ContentIdentity,
        authorization: ContentIdentity,
        build_result: ContentIdentity,
        expected_outputs: tuple[str, ...],
        build_outputs: tuple[RepositoryBuildOutput, ...],
    ) -> RepositorySourceAdmission:
        return cls(
            dependency_id=dependency_id,
            source_lock=source_lock,
            source_snapshot=source_snapshot,
            source_tree=source_tree,
            effective_revision=effective_revision,
            flavor_set=flavor_set,
            index_binding=index_binding,
            build_plan=build_plan,
            classification=classification,
            authorization=authorization,
            build_result=build_result,
            expected_outputs=expected_outputs,
            build_outputs=build_outputs,
            cache_key=cls.compute_cache_key(
                dependency_id, effective_revision, flavor_set, source_lock
            ),
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.SCHEMA,
            "dependency_id": self.dependency_id,
            **{
                name: getattr(self, name).to_dict()
                for name in (
                    "source_lock",
                    "source_snapshot",
                    "source_tree",
                    "effective_revision",
                    "flavor_set",
                    "index_binding",
                    "build_plan",
                    "classification",
                    "authorization",
                    "build_result",
                    "cache_key",
                )
            },
        }
        value["expected_outputs"] = list(self.expected_outputs)
        value["build_outputs"] = [item.to_dict() for item in self.build_outputs]
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositorySourceAdmission"
    ) -> RepositorySourceAdmission:
        identity_fields = (
            "source_lock",
            "source_snapshot",
            "source_tree",
            "effective_revision",
            "flavor_set",
            "index_binding",
            "build_plan",
            "classification",
            "authorization",
            "build_result",
            "cache_key",
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "dependency_id",
                    "expected_outputs",
                    "build_outputs",
                    *identity_fields,
                }
            ),
        )
        identities = {
            name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
            for name in identity_fields
        }
        return cls(
            dependency_id=_dependency_id(
                data["dependency_id"], f"{path}.dependency_id"
            ),
            expected_outputs=string_tuple(
                data["expected_outputs"], f"{path}.expected_outputs"
            ),
            build_outputs=parse_tuple(
                data["build_outputs"],
                f"{path}.build_outputs",
                RepositoryBuildOutput.from_dict,
            ),
            **identities,
        )


__all__ = [
    "REPOSITORY_BUILD_PLAN_SCHEMA",
    "REPOSITORY_SOURCE_ADMISSION_SCHEMA",
    "REPOSITORY_SOURCE_DEPENDENCY_CONTENT_KIND",
    "REPOSITORY_SOURCE_DEPENDENCY_SCHEMA",
    "REPOSITORY_SOURCE_LOCK_SCHEMA",
    "RepositoryBuildCommand",
    "RepositoryBuildEnvironment",
    "RepositoryBuildOutput",
    "RepositoryBuildPlan",
    "RepositoryRevisionKind",
    "RepositoryRevisionSelector",
    "RepositorySourceAdmission",
    "RepositorySourceDependency",
    "RepositorySourceLock",
]
