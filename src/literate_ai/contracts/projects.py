"""Canonical Literate AI project-profile contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    fields,
    int_value,
    list_value,
    mapping_value,
    string_tuple,
    string_value,
    unique,
)
from .channel_events import reject_secret_shaped
from .generation_cache import SourceCacheConfiguration, SourceCacheRootKind
from .html_observability import HtmlRenderRequest
from .identity import SCHEMA_PREFIX, ContentIdentity, contract_identity
from .repository_orchestration import RepositoryOrchestration
from .testing import ProjectTestReceiptPolicy
from .versioning import semantic_version

LEGACY_PROJECT_DEFINITION_SCHEMA = f"{SCHEMA_PREFIX}project-definition"
PROJECT_DEFINITION_SCHEMA = "urn:literate-ai:schema:v2:project-definition"
DEFAULT_PROJECT_TYPE = "application"
PROJECT_TYPES = frozenset(
    {
        "application",
        "library",
        "component-only",
        "full-stack",
        "document-generator",
        "firmware",
        "open-ended",
    }
)
PROJECT_TYPES_WITH_STARTER = frozenset({"application", "full-stack"})
LEGACY_PROJECT_SOURCE_INDEX_REQUIREMENT_SCHEMA = (
    f"{SCHEMA_PREFIX}project-source-index-requirement"
)
PROJECT_SOURCE_INTELLIGENCE_POLICY_SCHEMA = (
    f"{SCHEMA_PREFIX}project-source-intelligence-policy"
)
SOURCE_INTELLIGENCE_STAGE_STATUS_SCHEMA = (
    "literate-ai/source-intelligence-stage-status@1"
)
LEGACY_PROJECT_LIFECYCLE_DRIVER_SCHEMA = f"{SCHEMA_PREFIX}project-lifecycle-driver"
PROJECT_LIFECYCLE_DRIVER_SCHEMA = "urn:literate-ai:schema:v2:project-lifecycle-driver"
CI_TARGET_PREFERENCE_SCHEMA = "urn:literate-ai:schema:v2:ci-target-preference"
REPOSITORY_POLICY_SCHEMA = "literate-ai/repository-policy@1"
CANONICAL_PROJECT_PROFILE = "canonical"

MINIMUM_PROJECT_REBUILD_PHASES = (
    "compose-and-plan",
    "resolve-source-cache",
    "generate-source-and-tests-or-use-cache",
    "derive-source-intelligence",
    "validate-classify-and-authorize",
    "resolve-and-prepare-dependencies",
    "native-build",
    "verify-resolved-sbom",
    "run-generated-tests",
    "execute-application",
    "independent-acceptance",
    "admit-workspace",
    "write-candidate-receipt",
)

PROJECT_LIFECYCLE_EXTENSION_PHASES = (
    "package-artifacts",
    "publish-source-cache",
    "publish-artifacts",
    "deploy",
)

PROJECT_LIFECYCLE_DRIVER_PLACEHOLDERS = frozenset(
    {
        "{allow_host_execution}",
        "{candidate_receipt}",
        "{flavor_args}",
        "{lifecycle_request_identity}",
        "{project}",
        "{project_revision_identity}",
        "{python}",
        "{runtime_root}",
        "{specification}",
    }
)
_REQUIRED_LIFECYCLE_DRIVER_PLACEHOLDERS = frozenset(
    {
        "{allow_host_execution}",
        "{candidate_receipt}",
        "{lifecycle_request_identity}",
        "{project}",
        "{project_revision_identity}",
        "{runtime_root}",
        "{specification}",
    }
)
_ENVIRONMENT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

_FLAVOR_SELECTOR_NAME = r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?"
_FLAVOR_SELECTOR_VALUE = (
    rf"(?:{_FLAVOR_SELECTOR_NAME}|flavor://{_FLAVOR_SELECTOR_NAME}/"
    rf"{_FLAVOR_SELECTOR_NAME})"
)
_FLAVOR_SELECTOR_RE = re.compile(
    rf"^[+-](?:{_FLAVOR_SELECTOR_VALUE}|"
    rf"{_FLAVOR_SELECTOR_NAME}:{_FLAVOR_SELECTOR_VALUE})$"
)
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?$")
_SAFE_NAME_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,62}[:/])?"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$"
)
_REMOTE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
_COMPONENT_COORDINATE_RE = re.compile(
    r"^component://[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?/"
    r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
)
_PRE_RELEASE_VERSION_RE = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")


def _relative_path(value: str, path: str) -> str:
    raw = string_value(value, path)
    candidate = PurePosixPath(raw)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.as_posix() != raw
    ):
        fail(path, "must be a normalized relative POSIX path")
    return raw


def flavor_selector(value: Any, path: str = "flavor selector") -> str:
    """Validate one ordered Flavor selector shared by manifests and the CLI."""

    selector = string_value(value, path)
    if _FLAVOR_SELECTOR_RE.fullmatch(selector) is None:
        fail(
            path,
            "must be an ordered +flavor/-flavor or "
            "+slot-id:flavor/-slot-id:flavor selector",
        )
    return selector


class RepositoryMainState(StrEnum):
    FREE = "free"
    PRE_RELEASE = "pre-release"


class RepositoryMergeMethod(StrEnum):
    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


class RepositoryPatchAuthority(StrEnum):
    STRICT = "strict"
    LOOSE = "loose"


@dataclass(frozen=True, slots=True)
class RepositoryPolicy:
    default_branch: str = "main"
    main_state: RepositoryMainState = RepositoryMainState.FREE
    pre_release_version: str | None = None
    default_component: str | None = None
    sample_component_roots: tuple[str, ...] = ("samples",)
    work_queue: str = "docs/roadmap/active-work.md"
    remote: str = "origin"
    merge_method: RepositoryMergeMethod = RepositoryMergeMethod.MERGE
    pull_request_labels: tuple[str, ...] = ()
    release_engineer_source: str = "README.md#release-engineers"
    release_ci_workflow: str = "CI"
    patch_authority: RepositoryPatchAuthority = RepositoryPatchAuthority.STRICT
    writers: tuple[str, ...] = ()
    branch_gc_minimum_age_days: int = 7

    SCHEMA: ClassVar[str] = REPOSITORY_POLICY_SCHEMA

    def __post_init__(self) -> None:
        branch = string_value(self.default_branch, "RepositoryPolicy.default_branch")
        if (
            _SAFE_BRANCH_RE.fullmatch(branch) is None
            or any(value in branch for value in ("..", "//", "@{"))
            or branch.endswith(".lock")
        ):
            fail("RepositoryPolicy.default_branch", "must be a safe branch name")
        if not isinstance(self.main_state, RepositoryMainState):
            fail("RepositoryPolicy.main_state", "must be a typed repository state")
        if self.main_state is RepositoryMainState.PRE_RELEASE:
            if not isinstance(
                self.pre_release_version, str
            ) or not _PRE_RELEASE_VERSION_RE.fullmatch(self.pre_release_version):
                fail(
                    "RepositoryPolicy.pre_release_version",
                    "must be a major.minor version in pre-release state",
                )
        elif self.pre_release_version is not None:
            fail(
                "RepositoryPolicy.pre_release_version",
                "must be null unless main_state is pre-release",
            )
        if self.default_component is not None and not (
            self.default_component.startswith("component://")
            and _COMPONENT_COORDINATE_RE.fullmatch(self.default_component)
        ):
            _relative_path(self.default_component, "RepositoryPolicy.default_component")
        unique(self.sample_component_roots, "RepositoryPolicy.sample_component_roots")
        for index, root in enumerate(self.sample_component_roots):
            _relative_path(root, f"RepositoryPolicy.sample_component_roots[{index}]")
        _relative_path(self.work_queue, "RepositoryPolicy.work_queue")
        if (
            not isinstance(self.remote, str)
            or _REMOTE_RE.fullmatch(self.remote) is None
        ):
            fail("RepositoryPolicy.remote", "must be a safe remote name")
        if not isinstance(self.merge_method, RepositoryMergeMethod):
            fail("RepositoryPolicy.merge_method", "must be a typed merge method")
        unique(self.pull_request_labels, "RepositoryPolicy.pull_request_labels")
        for index, label in enumerate(self.pull_request_labels):
            string_value(label, f"RepositoryPolicy.pull_request_labels[{index}]")
        source, separator, fragment = self.release_engineer_source.partition("#")
        if not separator or not fragment or "#" in fragment:
            fail(
                "RepositoryPolicy.release_engineer_source",
                "must be a path followed by a non-empty fragment",
            )
        _relative_path(source, "RepositoryPolicy.release_engineer_source")
        string_value(self.release_ci_workflow, "RepositoryPolicy.release_ci_workflow")
        if not isinstance(self.patch_authority, RepositoryPatchAuthority):
            fail("RepositoryPolicy.patch_authority", "must be typed patch authority")
        unique(self.writers, "RepositoryPolicy.writers")
        for index, writer in enumerate(self.writers):
            if not isinstance(writer, str) or _SAFE_NAME_RE.fullmatch(writer) is None:
                fail(
                    f"RepositoryPolicy.writers[{index}]",
                    "must be a provider-qualified name or GitHub login",
                )
        int_value(
            self.branch_gc_minimum_age_days,
            "RepositoryPolicy.branch_gc_minimum_age_days",
            minimum=1,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "default_branch": self.default_branch,
            "main_state": self.main_state.value,
            "pre_release_version": self.pre_release_version,
            "default_component": self.default_component,
            "sample_component_roots": list(self.sample_component_roots),
            "work_queue": self.work_queue,
            "remote": self.remote,
            "merge_method": self.merge_method.value,
            "pull_request_labels": list(self.pull_request_labels),
            "release_engineer_source": self.release_engineer_source,
            "release_ci_workflow": self.release_ci_workflow,
            "patch_authority": self.patch_authority.value,
            "writers": list(self.writers),
            "branch_gc_minimum_age_days": self.branch_gc_minimum_age_days,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryPolicy"
    ) -> RepositoryPolicy:
        optional = {
            "default_branch",
            "main_state",
            "pre_release_version",
            "default_component",
            "sample_component_roots",
            "work_queue",
            "remote",
            "merge_method",
            "pull_request_labels",
            "release_engineer_source",
            "release_ci_workflow",
            "patch_authority",
            "writers",
            "branch_gc_minimum_age_days",
        }
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(),
            optional=frozenset(optional),
        )
        return cls(
            default_branch=string_value(data.get("default_branch", "main"), path),
            main_state=enum_value(
                RepositoryMainState, data.get("main_state", "free"), path
            ),
            pre_release_version=data.get("pre_release_version"),
            default_component=data.get("default_component"),
            sample_component_roots=(
                string_tuple(data["sample_component_roots"], path)
                if "sample_component_roots" in data
                else ("samples",)
            ),
            work_queue=string_value(
                data.get("work_queue", "docs/roadmap/active-work.md"), path
            ),
            remote=string_value(data.get("remote", "origin"), path),
            merge_method=enum_value(
                RepositoryMergeMethod, data.get("merge_method", "merge"), path
            ),
            pull_request_labels=(
                string_tuple(data["pull_request_labels"], path)
                if "pull_request_labels" in data
                else ()
            ),
            release_engineer_source=string_value(
                data.get("release_engineer_source", "README.md#release-engineers"), path
            ),
            release_ci_workflow=string_value(
                data.get("release_ci_workflow", "CI"), path
            ),
            patch_authority=enum_value(
                RepositoryPatchAuthority, data.get("patch_authority", "strict"), path
            ),
            writers=(string_tuple(data["writers"], path) if "writers" in data else ()),
            branch_gc_minimum_age_days=int_value(
                data.get("branch_gc_minimum_age_days", 7), path, minimum=1
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectLifecycleDriver:
    """A content-pinned, project-authorized driver for a complete host rebuild."""

    driver_id: str
    version: str
    implementation_paths: tuple[str, ...]
    implementation_identity: ContentIdentity
    argv: tuple[str, ...]
    environment_keys: tuple[str, ...]
    phases: tuple[str, ...]
    specification_scope: str
    timeout_seconds: int = 7_200

    SCHEMA: ClassVar[str] = PROJECT_LIFECYCLE_DRIVER_SCHEMA

    @property
    def binding(self) -> str:
        """Identify the legacy-compatible content-pinned external binding."""

        return "external"

    def __post_init__(self) -> None:
        string_value(self.driver_id, "ProjectLifecycleDriver.driver_id")
        semantic_version(self.version, "ProjectLifecycleDriver.version")
        if not isinstance(self.implementation_paths, tuple):
            fail(
                "ProjectLifecycleDriver.implementation_paths",
                "must be a tuple",
            )
        if not self.implementation_paths:
            fail(
                "ProjectLifecycleDriver.implementation_paths",
                "must identify at least one project-relative implementation root",
            )
        if len(self.implementation_paths) > 64:
            fail(
                "ProjectLifecycleDriver.implementation_paths",
                "must contain at most 64 files",
            )
        normalized_paths = tuple(
            _relative_path(
                item,
                f"ProjectLifecycleDriver.implementation_paths[{index}]",
            )
            for index, item in enumerate(self.implementation_paths)
        )
        unique(
            normalized_paths,
            "ProjectLifecycleDriver.implementation_paths",
            "implementation paths",
        )
        if normalized_paths != tuple(sorted(normalized_paths)):
            fail(
                "ProjectLifecycleDriver.implementation_paths",
                "must be sorted",
            )
        if not isinstance(self.implementation_identity, ContentIdentity):
            fail(
                "ProjectLifecycleDriver.implementation_identity",
                "must be a ContentIdentity",
            )
        if self.specification_scope not in {"component", "project"}:
            fail(
                "ProjectLifecycleDriver.specification_scope",
                "must be 'component' or 'project'",
            )
        if not self.argv:
            fail("ProjectLifecycleDriver.argv", "must contain an executable")
        if len(self.argv) > 128:
            fail("ProjectLifecycleDriver.argv", "must contain at most 128 arguments")
        placeholders: list[str] = []
        for index, raw in enumerate(self.argv):
            argument = string_value(
                raw,
                f"ProjectLifecycleDriver.argv[{index}]",
                max_length=4096,
            )
            if "\x00" in argument:
                fail(
                    f"ProjectLifecycleDriver.argv[{index}]",
                    "must not contain a NUL byte",
                )
            if "{" in argument or "}" in argument:
                if argument not in PROJECT_LIFECYCLE_DRIVER_PLACEHOLDERS:
                    fail(
                        f"ProjectLifecycleDriver.argv[{index}]",
                        "contains an unknown or embedded lifecycle placeholder",
                    )
                placeholders.append(argument)
        unique(placeholders, "ProjectLifecycleDriver.argv", "placeholders")
        missing = _REQUIRED_LIFECYCLE_DRIVER_PLACEHOLDERS - set(placeholders)
        if missing:
            fail(
                "ProjectLifecycleDriver.argv",
                "must include exact lifecycle placeholders: "
                + ", ".join(sorted(missing)),
            )
        if self.argv[0] != "{python}":
            executable = self.argv[0]
            if (
                executable in PROJECT_LIFECYCLE_DRIVER_PLACEHOLDERS
                or executable.strip() != executable
                or any(character.isspace() for character in executable)
                or "/" in executable
                or "\\" in executable
            ):
                fail(
                    "ProjectLifecycleDriver.argv[0]",
                    "must be {python} or one portable command name resolved "
                    "through PATH",
                )
            if "{python}" in placeholders:
                fail(
                    "ProjectLifecycleDriver.argv",
                    "may use {python} only as the Python lifecycle driver's launcher",
                )
        unique(
            self.environment_keys,
            "ProjectLifecycleDriver.environment_keys",
            "environment keys",
        )
        for index, key in enumerate(self.environment_keys):
            value = string_value(
                key,
                f"ProjectLifecycleDriver.environment_keys[{index}]",
                max_length=128,
            )
            if _ENVIRONMENT_KEY_RE.fullmatch(value) is None:
                fail(
                    f"ProjectLifecycleDriver.environment_keys[{index}]",
                    "must be a portable environment variable name",
                )
        prefix = MINIMUM_PROJECT_REBUILD_PHASES[:-1]
        suffix = MINIMUM_PROJECT_REBUILD_PHASES[-1:]
        if self.phases[: len(prefix)] != prefix or self.phases[-1:] != suffix:
            fail(
                "ProjectLifecycleDriver.phases",
                "must declare the complete ordered minimum project rebuild lifecycle",
            )
        extensions = self.phases[len(prefix) : -1]
        if extensions != tuple(
            phase for phase in PROJECT_LIFECYCLE_EXTENSION_PHASES if phase in extensions
        ) or any(
            phase not in PROJECT_LIFECYCLE_EXTENSION_PHASES for phase in extensions
        ):
            fail(
                "ProjectLifecycleDriver.phases",
                "contains unknown, duplicate, or out-of-order lifecycle extensions",
            )
        int_value(
            self.timeout_seconds,
            "ProjectLifecycleDriver.timeout_seconds",
            minimum=1,
            maximum=86_400,
        )

    @property
    def accepts_flavor_selectors(self) -> bool:
        return "{flavor_args}" in self.argv

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "driver_id": self.driver_id,
            "version": self.version,
            "implementation_paths": list(self.implementation_paths),
            "implementation_identity": self.implementation_identity.to_dict(),
            "argv": list(self.argv),
            "environment_keys": list(self.environment_keys),
            "phases": list(self.phases),
            "specification_scope": self.specification_scope,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "ProjectLifecycleDriver",
    ) -> ProjectLifecycleDriver:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_PROJECT_LIFECYCLE_DRIVER_SCHEMA
        ):
            value = dict(value)
            value["schema"] = cls.SCHEMA
            phases = value.get("phases")
            if isinstance(phases, list):
                value["phases"] = [
                    ("derive-source-intelligence" if phase == "index-source" else phase)
                    for phase in phases
                ]
        if isinstance(value, dict) and value.get("binding") == "external":
            value = {key: item for key, item in value.items() if key != "binding"}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "driver_id",
                    "version",
                    "implementation_paths",
                    "implementation_identity",
                    "argv",
                    "environment_keys",
                    "phases",
                    "specification_scope",
                    "timeout_seconds",
                }
            ),
        )
        return cls(
            driver_id=string_value(data["driver_id"], f"{path}.driver_id"),
            version=semantic_version(data["version"], f"{path}.version"),
            implementation_paths=string_tuple(
                data["implementation_paths"], f"{path}.implementation_paths"
            ),
            implementation_identity=ContentIdentity.from_dict(
                data["implementation_identity"],
                path=f"{path}.implementation_identity",
            ),
            argv=string_tuple(data["argv"], f"{path}.argv"),
            environment_keys=string_tuple(
                data["environment_keys"], f"{path}.environment_keys"
            ),
            phases=string_tuple(data["phases"], f"{path}.phases"),
            specification_scope=string_value(
                data["specification_scope"], f"{path}.specification_scope"
            ),
            timeout_seconds=int_value(
                data["timeout_seconds"],
                f"{path}.timeout_seconds",
                minimum=1,
                maximum=86_400,
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardProjectLifecycleDriver:
    """Declarative binding to an installed framework artifact and exact policy.

    This contract selects authority; it does not claim that a CLI execution adapter is
    available or that Standard is the project's runnable default.
    """

    framework_distribution_identity: ContentIdentity
    policy_identity: ContentIdentity

    SCHEMA: ClassVar[str] = PROJECT_LIFECYCLE_DRIVER_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.framework_distribution_identity, ContentIdentity):
            fail(
                "StandardProjectLifecycleDriver.framework_distribution_identity",
                "must be a ContentIdentity",
            )
        if not isinstance(self.policy_identity, ContentIdentity):
            fail(
                "StandardProjectLifecycleDriver.policy_identity",
                "must be a ContentIdentity",
            )

    @property
    def binding(self) -> str:
        return "standard"

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "binding": self.binding,
            "framework_distribution_identity": (
                self.framework_distribution_identity.to_dict()
            ),
            "policy_identity": self.policy_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardProjectLifecycleDriver",
    ) -> StandardProjectLifecycleDriver:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "binding",
                    "framework_distribution_identity",
                    "policy_identity",
                }
            ),
        )
        if data["binding"] != "standard":
            fail(f"{path}.binding", "must be 'standard'")
        return cls(
            framework_distribution_identity=ContentIdentity.from_dict(
                data["framework_distribution_identity"],
                path=f"{path}.framework_distribution_identity",
            ),
            policy_identity=ContentIdentity.from_dict(
                data["policy_identity"], path=f"{path}.policy_identity"
            ),
        )


ExternalProjectLifecycleDriver = ProjectLifecycleDriver
ProjectLifecycleDriverBinding = (
    ExternalProjectLifecycleDriver | StandardProjectLifecycleDriver
)


def _project_lifecycle_driver_from_dict(
    value: Any, *, path: str
) -> ProjectLifecycleDriverBinding:
    if isinstance(value, dict) and value.get("binding") == "standard":
        return StandardProjectLifecycleDriver.from_dict(value, path=path)
    return ProjectLifecycleDriver.from_dict(value, path=path)


class SourceIntelligenceMode(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    OFF = "off"


class SourceIntelligenceStage(StrEnum):
    PROJECT_MAINTENANCE = "project-maintenance"
    SOURCE_GENERATION = "source-generation"
    CACHE_CONSUMPTION = "cache-consumption"
    SOURCE_TO_SPECIFICATION = "source-to-specification"
    REPOSITORY_SOURCE_ADMISSION = "repository-source-admission"
    STRUCTURAL_REVIEW = "structural-review"


class SourceIntelligenceArtifactPublication(StrEnum):
    METADATA_ONLY = "metadata-only"
    INCLUDE_ARTIFACT = "include-artifact"


@dataclass(frozen=True, slots=True)
class SourceIntelligenceStageStatus:
    """Canonical effective intelligence state at one lifecycle boundary."""

    stage: SourceIntelligenceStage
    mode: SourceIntelligenceMode
    state: str
    provider_id: str
    reason_code: str | None = None

    SCHEMA: ClassVar[str] = SOURCE_INTELLIGENCE_STAGE_STATUS_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.stage, SourceIntelligenceStage):
            fail("SourceIntelligenceStageStatus.stage", "must be a typed stage")
        if not isinstance(self.mode, SourceIntelligenceMode):
            fail("SourceIntelligenceStageStatus.mode", "must be a typed mode")
        provider_id = string_value(
            self.provider_id, "SourceIntelligenceStageStatus.provider_id"
        )
        allowed = {
            SourceIntelligenceMode.REQUIRED: {"current"},
            SourceIntelligenceMode.PREFERRED: {"current", "unavailable"},
            SourceIntelligenceMode.OFF: {"off"},
        }
        if self.state not in allowed[self.mode]:
            fail(
                "SourceIntelligenceStageStatus.state",
                "is inconsistent with the selected mode",
            )
        if self.state == "unavailable":
            string_value(self.reason_code, "SourceIntelligenceStageStatus.reason_code")
        elif self.reason_code is not None:
            fail(
                "SourceIntelligenceStageStatus.reason_code",
                "is only valid for unavailable intelligence",
            )
        if provider_id != provider_id.strip():
            fail(
                "SourceIntelligenceStageStatus.provider_id",
                "must be canonical",
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": self.SCHEMA,
            "stage": self.stage.value,
            "mode": self.mode.value,
            "state": self.state,
            "provider_id": self.provider_id,
        }
        if self.reason_code is not None:
            result["reason_code"] = self.reason_code
        return result

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceIntelligenceStageStatus"
    ) -> SourceIntelligenceStageStatus:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"stage", "mode", "state", "provider_id"}),
            optional=frozenset({"reason_code"}),
        )
        return cls(
            stage=enum_value(SourceIntelligenceStage, data["stage"], f"{path}.stage"),
            mode=enum_value(SourceIntelligenceMode, data["mode"], f"{path}.mode"),
            state=string_value(data["state"], f"{path}.state"),
            provider_id=string_value(data["provider_id"], f"{path}.provider_id"),
            reason_code=(
                string_value(data["reason_code"], f"{path}.reason_code")
                if "reason_code" in data
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectSourceIntelligencePolicy:
    """Provider selection and explicit requirements at each lifecycle boundary."""

    provider_id: str
    command: str | None
    minimum_version: str | None
    artifact_path: str | None
    stages: tuple[tuple[SourceIntelligenceStage, SourceIntelligenceMode], ...]
    artifact_publication: SourceIntelligenceArtifactPublication

    SCHEMA: ClassVar[str] = PROJECT_SOURCE_INTELLIGENCE_POLICY_SCHEMA

    def __post_init__(self) -> None:
        provider_id = string_value(
            self.provider_id, "ProjectSourceIntelligencePolicy.provider_id"
        )
        if not isinstance(
            self.artifact_publication, SourceIntelligenceArtifactPublication
        ):
            fail(
                "ProjectSourceIntelligencePolicy.artifact_publication",
                "must be a SourceIntelligenceArtifactPublication",
            )
        expected_stages = tuple(SourceIntelligenceStage)
        actual_stages = tuple(stage for stage, _mode in self.stages)
        if actual_stages != expected_stages:
            fail(
                "ProjectSourceIntelligencePolicy.stages",
                "must cover every lifecycle stage in canonical order",
            )
        for index, (stage, mode) in enumerate(self.stages):
            if not isinstance(stage, SourceIntelligenceStage) or not isinstance(
                mode, SourceIntelligenceMode
            ):
                fail(
                    f"ProjectSourceIntelligencePolicy.stages[{index}]",
                    "must contain a stage and mode",
                )
        if provider_id == "none":
            if any(
                value is not None
                for value in (self.command, self.minimum_version, self.artifact_path)
            ) or any(mode is not SourceIntelligenceMode.OFF for _, mode in self.stages):
                fail(
                    "ProjectSourceIntelligencePolicy",
                    "provider 'none' requires null tool fields and every stage off",
                )
            if self.artifact_publication is not (
                SourceIntelligenceArtifactPublication.METADATA_ONLY
            ):
                fail(
                    "ProjectSourceIntelligencePolicy.artifact_publication",
                    "provider 'none' cannot publish an artifact",
                )
            return
        command = string_value(self.command, "ProjectSourceIntelligencePolicy.command")
        if (
            command.strip() != command
            or any(character.isspace() for character in command)
            or "/" in command
            or "\\" in command
        ):
            fail(
                "ProjectSourceIntelligencePolicy.command",
                "must be one portable command name resolved through PATH",
            )
        semantic_version(
            self.minimum_version,
            "ProjectSourceIntelligencePolicy.minimum_version",
        )
        _relative_path(
            self.artifact_path,
            "ProjectSourceIntelligencePolicy.artifact_path",
        )

    def mode_for(self, stage: SourceIntelligenceStage) -> SourceIntelligenceMode:
        if not isinstance(stage, SourceIntelligenceStage):
            raise TypeError("source-intelligence stage must be typed")
        return dict(self.stages)[stage]

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "provider_id": self.provider_id,
            "command": self.command,
            "minimum_version": self.minimum_version,
            "artifact_path": self.artifact_path,
            "stages": {stage.value: mode.value for stage, mode in self.stages},
            "artifact_publication": self.artifact_publication.value,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "ProjectSourceIntelligencePolicy",
    ) -> ProjectSourceIntelligencePolicy:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "provider_id",
                    "command",
                    "minimum_version",
                    "artifact_path",
                    "stages",
                    "artifact_publication",
                }
            ),
        )
        raw_stages = data["stages"]
        if not isinstance(raw_stages, dict) or set(raw_stages) != {
            stage.value for stage in SourceIntelligenceStage
        }:
            fail(f"{path}.stages", "must cover every source-intelligence stage")
        return cls(
            provider_id=string_value(data["provider_id"], f"{path}.provider_id"),
            command=(
                None
                if data["command"] is None
                else string_value(data["command"], f"{path}.command")
            ),
            minimum_version=(
                None
                if data["minimum_version"] is None
                else semantic_version(
                    data["minimum_version"], f"{path}.minimum_version"
                )
            ),
            artifact_path=(
                None
                if data["artifact_path"] is None
                else _relative_path(data["artifact_path"], f"{path}.artifact_path")
            ),
            stages=tuple(
                (
                    stage,
                    enum_value(
                        SourceIntelligenceMode,
                        raw_stages[stage.value],
                        f"{path}.stages.{stage.value}",
                    ),
                )
                for stage in SourceIntelligenceStage
            ),
            artifact_publication=enum_value(
                SourceIntelligenceArtifactPublication,
                data["artifact_publication"],
                f"{path}.artifact_publication",
            ),
        )


class CiTarget(StrEnum):
    """A CI execution surface a project can prefer.

    ``GITHUB`` and ``GITLAB`` are mutually exclusive within one project: only one
    remote can be the repository's native forge-triggered CI. ``LOCAL`` (a private
    worker fleet) can be combined with either, since it runs independently of any
    forge webhook and typically covers more OS/CPU/GPU combinations than a hosted
    remote runner offers.
    """

    LOCAL = "local"
    GITHUB = "github"
    GITLAB = "gitlab"


class CiExecutionMode(StrEnum):
    """How a CI target preference entry runs relative to the ones before it."""

    SERIAL = "serial"
    PARALLEL = "parallel"


@dataclass(frozen=True, slots=True)
class CiTargetPreference:
    """One entry in a project's ordered CI target preference list.

    Array order is the fallback/priority order (first entry is most preferred);
    ``mode`` says whether this entry runs concurrently with the entries before it
    or only serially after them.
    """

    target: CiTarget
    mode: CiExecutionMode

    SCHEMA: ClassVar[str] = CI_TARGET_PREFERENCE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.target, CiTarget):
            fail("CiTargetPreference.target", "must be a typed CI target")
        if not isinstance(self.mode, CiExecutionMode):
            fail("CiTargetPreference.mode", "must be a typed CI execution mode")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target.value,
            "mode": self.mode.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CiTargetPreference"
    ) -> CiTargetPreference:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "mode"}),
        )
        return cls(
            target=enum_value(CiTarget, data["target"], f"{path}.target"),
            mode=enum_value(CiExecutionMode, data["mode"], f"{path}.mode"),
        )


def _channel_identifier(value: Any, path: str) -> str:
    text = reject_secret_shaped(string_value(value, path), path)
    if "://" in text or "/" in text or "hooks.slack" in text.casefold():
        fail(path, "must be a channel or issue id, not a URL or webhook")
    return text


@dataclass(frozen=True, slots=True)
class InstitutionalChannels:
    """Optional Slack/Outlook/Jira ids in project policy. Tokens stay operator-local."""

    slack_channel: str = ""
    outlook_from: str = ""
    outlook_to: tuple[str, ...] = ()
    jira_issue: str = ""

    def __post_init__(self) -> None:
        if not (
            self.slack_channel
            or self.outlook_from
            or self.outlook_to
            or self.jira_issue
        ):
            fail(
                "InstitutionalChannels",
                "must declare at least one channel, mailbox, or issue id",
            )
        if self.slack_channel:
            _channel_identifier(
                self.slack_channel, "InstitutionalChannels.slack_channel"
            )
        if self.outlook_from:
            reject_secret_shaped(
                string_value(self.outlook_from, "InstitutionalChannels.outlook_from"),
                "InstitutionalChannels.outlook_from",
            )
        unique(self.outlook_to, "InstitutionalChannels.outlook_to", "recipients")
        for index, item in enumerate(self.outlook_to):
            reject_secret_shaped(
                string_value(item, f"InstitutionalChannels.outlook_to[{index}]"),
                f"InstitutionalChannels.outlook_to[{index}]",
            )
        if self.jira_issue:
            _channel_identifier(self.jira_issue, "InstitutionalChannels.jira_issue")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {}
        if self.slack_channel:
            value["slack_channel"] = self.slack_channel
        if self.outlook_from:
            value["outlook_from"] = self.outlook_from
        if self.outlook_to:
            value["outlook_to"] = list(self.outlook_to)
        if self.jira_issue:
            value["jira_issue"] = self.jira_issue
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "InstitutionalChannels"
    ) -> InstitutionalChannels:
        data = fields(
            value,
            path=path,
            required=frozenset(),
            optional=frozenset(
                {"slack_channel", "outlook_from", "outlook_to", "jira_issue"}
            ),
        )
        return cls(
            slack_channel=(
                string_value(data["slack_channel"], f"{path}.slack_channel")
                if "slack_channel" in data
                else ""
            ),
            outlook_from=(
                string_value(data["outlook_from"], f"{path}.outlook_from")
                if "outlook_from" in data
                else ""
            ),
            outlook_to=(
                string_tuple(data["outlook_to"], f"{path}.outlook_to")
                if "outlook_to" in data
                else ()
            ),
            jira_issue=(
                string_value(data["jira_issue"], f"{path}.jira_issue")
                if "jira_issue" in data
                else ""
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectDefinition:
    """One explicit project root and its portable catalog boundaries."""

    project_id: str
    version: str
    profile: str
    agent_skill: str
    component_roots: tuple[str, ...]
    flavor_roots: tuple[str, ...]
    skill_roots: tuple[str, ...]
    workflow_roots: tuple[str, ...]
    routing_roots: tuple[str, ...]
    documentation_roots: tuple[str, ...]
    source_intelligence: ProjectSourceIntelligencePolicy
    lifecycle_driver: ProjectLifecycleDriverBinding | None = None
    test_receipt: str | None = None
    test_receipt_policy: ProjectTestReceiptPolicy | None = None
    default_flavor_selectors: tuple[str, ...] = ()
    component_flavor_selectors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source_cache: SourceCacheConfiguration | None = None
    log_dir: str = "logs"
    agent_development_workflow: str = "dev"
    ci_targets: tuple[CiTargetPreference, ...] = ()
    mcp_roots: tuple[str, ...] = ()
    institutional_channels: InstitutionalChannels | None = None
    project_type: str | None = None
    repository_policy: RepositoryPolicy = field(default_factory=RepositoryPolicy)
    html_render_requests: tuple[HtmlRenderRequest, ...] = ()
    repository_orchestration: RepositoryOrchestration | None = None

    SCHEMA: ClassVar[str] = PROJECT_DEFINITION_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.project_id, "ProjectDefinition.project_id")
        semantic_version(self.version, "ProjectDefinition.version")
        string_value(self.profile, "ProjectDefinition.profile")
        if self.profile != CANONICAL_PROJECT_PROFILE:
            fail(
                "ProjectDefinition.profile",
                f"must be {CANONICAL_PROJECT_PROFILE!r} in schema v1",
            )
        _relative_path(self.agent_skill, "ProjectDefinition.agent_skill")
        if self.agent_skill != "SKILL.md":
            fail(
                "ProjectDefinition.agent_skill",
                "must identify the provider-neutral root SKILL.md",
            )
        for label in (
            "component_roots",
            "flavor_roots",
            "skill_roots",
            "workflow_roots",
            "routing_roots",
            "documentation_roots",
            "mcp_roots",
        ):
            values = getattr(self, label)
            unique(values, f"ProjectDefinition.{label}")
            for index, item in enumerate(values):
                _relative_path(item, f"ProjectDefinition.{label}[{index}]")
        if not self.documentation_roots:
            fail(
                "ProjectDefinition.documentation_roots",
                "must declare at least one documentation root",
            )
        if not isinstance(self.source_intelligence, ProjectSourceIntelligencePolicy):
            fail(
                "ProjectDefinition.source_intelligence",
                "must be a ProjectSourceIntelligencePolicy",
            )
        if self.test_receipt is not None:
            receipt = PurePosixPath(
                _relative_path(self.test_receipt, "ProjectDefinition.test_receipt")
            )
            protected_files = {
                PurePosixPath(self.agent_skill),
                PurePosixPath("literate.project.json"),
            }
            roots = tuple(
                PurePosixPath(item)
                for label in (
                    "component_roots",
                    "flavor_roots",
                    "skill_roots",
                    "workflow_roots",
                    "routing_roots",
                    "documentation_roots",
                    "mcp_roots",
                )
                for item in getattr(self, label)
            )
            if receipt in protected_files or any(
                receipt == root or root in receipt.parents or receipt in root.parents
                for root in roots
            ):
                fail(
                    "ProjectDefinition.test_receipt",
                    "must not overlap the project manifest, agent skill, or a "
                    "declared catalog root",
                )
        if self.test_receipt_policy is not None:
            if not isinstance(self.test_receipt_policy, ProjectTestReceiptPolicy):
                fail(
                    "ProjectDefinition.test_receipt_policy",
                    "must be a ProjectTestReceiptPolicy",
                )
            if self.test_receipt is None:
                fail(
                    "ProjectDefinition.test_receipt_policy",
                    "requires a configured test_receipt path",
                )
        if self.lifecycle_driver is not None and not isinstance(
            self.lifecycle_driver,
            (ProjectLifecycleDriver, StandardProjectLifecycleDriver),
        ):
            fail(
                "ProjectDefinition.lifecycle_driver",
                "must be a standard or external Project lifecycle driver",
            )
        if self.source_cache is not None and not isinstance(
            self.source_cache, SourceCacheConfiguration
        ):
            fail(
                "ProjectDefinition.source_cache",
                "must be a SourceCacheConfiguration",
            )
        for index, selector in enumerate(self.default_flavor_selectors):
            flavor_selector(
                selector,
                f"ProjectDefinition.default_flavor_selectors[{index}]",
            )
        if not isinstance(self.component_flavor_selectors, dict):
            fail(
                "ProjectDefinition.component_flavor_selectors",
                "must be a dict mapping Component paths to selector tuples",
            )
        for component_path, selectors in self.component_flavor_selectors.items():
            if not isinstance(component_path, str) or not component_path:
                fail(
                    "ProjectDefinition.component_flavor_selectors",
                    "keys must be non-empty Component path strings",
                )
            _relative_path(
                component_path,
                f"ProjectDefinition.component_flavor_selectors[{component_path!r}]",
            )
            if not isinstance(selectors, (tuple, list)):
                fail(
                    f"ProjectDefinition.component_flavor_selectors[{component_path!r}]",
                    "values must be selector tuples",
                )
            for index, selector in enumerate(selectors):
                flavor_selector(
                    selector,
                    f"ProjectDefinition.component_flavor_selectors"
                    f"[{component_path!r}][{index}]",
                )
        if self.agent_development_workflow not in {"dev", "staging", "production"}:
            fail(
                "ProjectDefinition.agent_development_workflow",
                "must be 'dev', 'staging', or 'production'",
            )
        seen_targets: set[CiTarget] = set()
        for index, preference in enumerate(self.ci_targets):
            if not isinstance(preference, CiTargetPreference):
                fail(
                    f"ProjectDefinition.ci_targets[{index}]",
                    "must be a typed CiTargetPreference",
                )
            if preference.target in seen_targets:
                fail(
                    "ProjectDefinition.ci_targets",
                    f"repeats CI target {preference.target.value!r}",
                )
            seen_targets.add(preference.target)
        if {CiTarget.GITHUB, CiTarget.GITLAB} <= seen_targets:
            fail(
                "ProjectDefinition.ci_targets",
                "github and gitlab are mutually exclusive for one repository",
            )
        if self.institutional_channels is not None and not isinstance(
            self.institutional_channels, InstitutionalChannels
        ):
            fail(
                "ProjectDefinition.institutional_channels",
                "must be InstitutionalChannels",
            )
        if self.project_type is not None:
            typed = string_value(self.project_type, "ProjectDefinition.project_type")
            if typed not in PROJECT_TYPES:
                fail(
                    "ProjectDefinition.project_type",
                    "must be one of: " + ", ".join(sorted(PROJECT_TYPES)),
                )
        if not isinstance(self.repository_policy, RepositoryPolicy):
            fail(
                "ProjectDefinition.repository_policy",
                "must be a RepositoryPolicy",
            )
        if (
            not isinstance(self.html_render_requests, tuple)
            or len(self.html_render_requests) > 128
        ):
            fail(
                "ProjectDefinition.html_render_requests",
                "must be an immutable tuple of at most 128 render requests",
            )
        for request in self.html_render_requests:
            if not isinstance(request, HtmlRenderRequest):
                fail(
                    "ProjectDefinition.html_render_requests",
                    "must contain typed HtmlRenderRequest records",
                )
        unique(
            tuple(
                request.output_path.casefold() for request in self.html_render_requests
            ),
            "ProjectDefinition.html_render_requests.output_paths",
        )
        if self.repository_orchestration is not None:
            if not isinstance(self.repository_orchestration, RepositoryOrchestration):
                fail(
                    "ProjectDefinition.repository_orchestration",
                    "requires typed authority",
                )
            owned_paths = [
                *self.component_roots,
                *self.flavor_roots,
                *self.skill_roots,
                *self.workflow_roots,
                *self.routing_roots,
                *self.documentation_roots,
                *self.mcp_roots,
                self.agent_skill,
                self.log_dir,
                *(request.output_path for request in self.html_render_requests),
            ]
            owned_paths.extend(
                path
                for path in (self.test_receipt, self.source_intelligence.artifact_path)
                if path is not None
            )
            if self.source_cache is not None:
                owned_paths.extend(
                    target.root_reference
                    for target in self.source_cache.targets
                    if target.root_kind is SourceCacheRootKind.PROJECT_RELATIVE
                )
            for pin in self.repository_orchestration.repositories:
                child = PurePosixPath(pin.path.casefold())
                for owned in owned_paths:
                    root = PurePosixPath(owned.casefold())
                    if root == child or root in child.parents or child in root.parents:
                        fail(
                            "ProjectDefinition.repository_orchestration",
                            "root catalogs or output paths "
                            "cannot overlap child authority",
                        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "project_id": self.project_id,
            "version": self.version,
            "profile": self.profile,
            "agent_skill": self.agent_skill,
            "component_roots": list(self.component_roots),
            "flavor_roots": list(self.flavor_roots),
            "skill_roots": list(self.skill_roots),
            "workflow_roots": list(self.workflow_roots),
            "routing_roots": list(self.routing_roots),
            "documentation_roots": list(self.documentation_roots),
            "source_intelligence": self.source_intelligence.to_dict(),
        }
        if self.lifecycle_driver is not None:
            value["lifecycle_driver"] = self.lifecycle_driver.to_dict()
        if self.source_cache is not None:
            value["source_cache"] = self.source_cache.to_dict()
        if self.test_receipt is not None:
            value["test_receipt"] = self.test_receipt
        if self.test_receipt_policy is not None:
            value["test_receipt_policy"] = self.test_receipt_policy.to_dict()
        if self.default_flavor_selectors:
            value["default_flavor_selectors"] = list(self.default_flavor_selectors)
        if self.component_flavor_selectors:
            value["component_flavor_selectors"] = {
                key: list(selectors)
                for key, selectors in self.component_flavor_selectors.items()
            }
        if self.log_dir != "logs":
            value["log_dir"] = self.log_dir
        if self.agent_development_workflow != "dev":
            value["agent_development_workflow"] = self.agent_development_workflow
        if self.ci_targets:
            value["ci_targets"] = [
                preference.to_dict() for preference in self.ci_targets
            ]
        if self.mcp_roots:
            value["mcp_roots"] = list(self.mcp_roots)
        if self.institutional_channels is not None:
            value["institutional_channels"] = self.institutional_channels.to_dict()
        if self.project_type is not None:
            value["project_type"] = self.project_type
        value["repository_policy"] = self.repository_policy.to_dict()
        if self.html_render_requests:
            value["html_render_requests"] = [
                request.to_dict() for request in self.html_render_requests
            ]
        if self.repository_orchestration is not None:
            value["repository_orchestration"] = self.repository_orchestration.to_dict()
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectDefinition"
    ) -> ProjectDefinition:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_PROJECT_DEFINITION_SCHEMA
        ):
            if "repository_orchestration" in value:
                fail(
                    path,
                    "legacy project definitions do not admit orchestration authority",
                )
            legacy = dict(value)
            raw_source_index = legacy.pop("source_index", None)
            source_index = contract_fields(
                raw_source_index,
                path=f"{path}.source_index",
                schema_uri=LEGACY_PROJECT_SOURCE_INDEX_REQUIREMENT_SCHEMA,
                required=frozenset(
                    {
                        "provider_id",
                        "command",
                        "minimum_version",
                        "index_path",
                    }
                ),
            )
            legacy["schema"] = cls.SCHEMA
            legacy["source_intelligence"] = {
                "schema": PROJECT_SOURCE_INTELLIGENCE_POLICY_SCHEMA,
                "provider_id": source_index["provider_id"],
                "command": source_index["command"],
                "minimum_version": source_index["minimum_version"],
                "artifact_path": source_index["index_path"],
                "stages": {
                    stage.value: (
                        SourceIntelligenceMode.OFF.value
                        if stage is SourceIntelligenceStage.CACHE_CONSUMPTION
                        else (
                            SourceIntelligenceMode.PREFERRED.value
                            if stage is SourceIntelligenceStage.STRUCTURAL_REVIEW
                            else SourceIntelligenceMode.REQUIRED.value
                        )
                    )
                    for stage in SourceIntelligenceStage
                },
                "artifact_publication": (
                    SourceIntelligenceArtifactPublication.INCLUDE_ARTIFACT.value
                ),
            }
            value = legacy
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "project_id",
                    "version",
                    "profile",
                    "agent_skill",
                    "component_roots",
                    "flavor_roots",
                    "skill_roots",
                    "workflow_roots",
                    "routing_roots",
                    "documentation_roots",
                    "source_intelligence",
                }
            ),
            optional=frozenset(
                {
                    "test_receipt",
                    "test_receipt_policy",
                    "default_flavor_selectors",
                    "component_flavor_selectors",
                    "lifecycle_driver",
                    "source_cache",
                    "log_dir",
                    "agent_development_workflow",
                    "ci_targets",
                    "mcp_roots",
                    "institutional_channels",
                    "project_type",
                    "repository_policy",
                    "html_render_requests",
                    "repository_orchestration",
                }
            ),
        )
        return cls(
            project_id=string_value(data["project_id"], f"{path}.project_id"),
            version=semantic_version(data["version"], f"{path}.version"),
            profile=string_value(data["profile"], f"{path}.profile"),
            agent_skill=_relative_path(data["agent_skill"], f"{path}.agent_skill"),
            component_roots=string_tuple(
                data["component_roots"], f"{path}.component_roots"
            ),
            flavor_roots=string_tuple(data["flavor_roots"], f"{path}.flavor_roots"),
            skill_roots=string_tuple(data["skill_roots"], f"{path}.skill_roots"),
            workflow_roots=string_tuple(
                data["workflow_roots"], f"{path}.workflow_roots"
            ),
            routing_roots=string_tuple(data["routing_roots"], f"{path}.routing_roots"),
            documentation_roots=string_tuple(
                data["documentation_roots"], f"{path}.documentation_roots"
            ),
            source_intelligence=ProjectSourceIntelligencePolicy.from_dict(
                data["source_intelligence"], path=f"{path}.source_intelligence"
            ),
            lifecycle_driver=(
                _project_lifecycle_driver_from_dict(
                    data["lifecycle_driver"], path=f"{path}.lifecycle_driver"
                )
                if "lifecycle_driver" in data
                else None
            ),
            source_cache=(
                SourceCacheConfiguration.from_dict(
                    data["source_cache"], path=f"{path}.source_cache"
                )
                if "source_cache" in data
                else None
            ),
            test_receipt=(
                _relative_path(data["test_receipt"], f"{path}.test_receipt")
                if "test_receipt" in data
                else None
            ),
            test_receipt_policy=(
                ProjectTestReceiptPolicy.from_dict(
                    data["test_receipt_policy"],
                    path=f"{path}.test_receipt_policy",
                )
                if "test_receipt_policy" in data
                else None
            ),
            default_flavor_selectors=(
                string_tuple(
                    data["default_flavor_selectors"],
                    f"{path}.default_flavor_selectors",
                )
                if "default_flavor_selectors" in data
                else ()
            ),
            component_flavor_selectors=(
                {
                    string_value(key, f"{path}.component_flavor_selectors.key"): tuple(
                        string_value(
                            item,
                            f"{path}.component_flavor_selectors[{key!r}][{i}]",
                        )
                        for i, item in enumerate(
                            list_value(
                                selectors,
                                f"{path}.component_flavor_selectors[{key!r}]",
                            )
                        )
                    )
                    for key, selectors in (
                        mapping_value(
                            data["component_flavor_selectors"],
                            f"{path}.component_flavor_selectors",
                        ).items()
                    )
                }
                if "component_flavor_selectors" in data
                else {}
            ),
            log_dir=(
                string_value(data["log_dir"], f"{path}.log_dir")
                if "log_dir" in data
                else "logs"
            ),
            agent_development_workflow=(
                string_value(
                    data["agent_development_workflow"],
                    f"{path}.agent_development_workflow",
                )
                if "agent_development_workflow" in data
                else "dev"
            ),
            ci_targets=(
                tuple(
                    CiTargetPreference.from_dict(
                        item, path=f"{path}.ci_targets[{index}]"
                    )
                    for index, item in enumerate(
                        list_value(data["ci_targets"], f"{path}.ci_targets")
                    )
                )
                if "ci_targets" in data
                else ()
            ),
            mcp_roots=(
                string_tuple(data["mcp_roots"], f"{path}.mcp_roots")
                if "mcp_roots" in data
                else ()
            ),
            institutional_channels=(
                InstitutionalChannels.from_dict(
                    data["institutional_channels"],
                    path=f"{path}.institutional_channels",
                )
                if "institutional_channels" in data
                else None
            ),
            project_type=(
                string_value(data["project_type"], f"{path}.project_type")
                if "project_type" in data
                else None
            ),
            repository_policy=(
                RepositoryPolicy.from_dict(
                    data["repository_policy"], path=f"{path}.repository_policy"
                )
                if "repository_policy" in data
                else RepositoryPolicy()
            ),
            html_render_requests=tuple(
                HtmlRenderRequest.from_dict(
                    item, path=f"{path}.html_render_requests[{index}]"
                )
                for index, item in enumerate(
                    list_value(
                        data.get("html_render_requests", []),
                        f"{path}.html_render_requests",
                    )
                )
            ),
            repository_orchestration=(
                RepositoryOrchestration.from_dict(data["repository_orchestration"])
                if "repository_orchestration" in data
                else None
            ),
        )


__all__ = [
    "CANONICAL_PROJECT_PROFILE",
    "CI_TARGET_PREFERENCE_SCHEMA",
    "DEFAULT_PROJECT_TYPE",
    "CiExecutionMode",
    "CiTarget",
    "CiTargetPreference",
    "ExternalProjectLifecycleDriver",
    "InstitutionalChannels",
    "MINIMUM_PROJECT_REBUILD_PHASES",
    "PROJECT_DEFINITION_SCHEMA",
    "PROJECT_LIFECYCLE_EXTENSION_PHASES",
    "PROJECT_LIFECYCLE_DRIVER_PLACEHOLDERS",
    "PROJECT_LIFECYCLE_DRIVER_SCHEMA",
    "PROJECT_SOURCE_INTELLIGENCE_POLICY_SCHEMA",
    "REPOSITORY_POLICY_SCHEMA",
    "PROJECT_TYPES",
    "PROJECT_TYPES_WITH_STARTER",
    "ProjectDefinition",
    "ProjectLifecycleDriver",
    "ProjectLifecycleDriverBinding",
    "ProjectSourceIntelligencePolicy",
    "RepositoryMainState",
    "RepositoryMergeMethod",
    "RepositoryPatchAuthority",
    "RepositoryPolicy",
    "SourceIntelligenceArtifactPublication",
    "SourceIntelligenceMode",
    "SourceIntelligenceStage",
    "StandardProjectLifecycleDriver",
    "flavor_selector",
]
