"""Coding-CLI-backed source generation from specifications and selected Flavors."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from literate_ai.adapters._processes import (
    ProcessTreeOwnership,
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.adapters.dependencies import (
    CycloneDxBomError,
    build_cyclonedx_bom,
    validate_cyclonedx_bom,
)
from literate_ai.adapters.intelligence import (
    SourceIntelligenceArtifact,
    SourceIntelligenceError,
    SourceIntelligenceProvider,
    generated_source_tree_identity,
)
from literate_ai.adapters.model_selection_stack import (
    begin_generation_session,
    explicit_stack_model,
)
from literate_ai.adapters.models.native_sdk_recipe import RecipeNativeSdkDependency
from literate_ai.adapters.user_paths import resolve_host_system_paths
from literate_ai.application.flavor_selection import (
    FlavorSelectionError,
)
from literate_ai.application.flavor_selection import (
    apply_flavor_selectors as apply_ordered_flavor_selectors,
)
from literate_ai.application.skill_closure import (
    SkillClosureError,
    close_specification_to_source_skills,
)
from literate_ai.contracts import (
    CYCLONEDX_SCHEMA_URI,
    CYCLONEDX_SOURCE_SBOM_PATH,
    ContentIdentity,
    ContractValidationError,
    CppLibraryLayout,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    FlavorAxis,
    FlavorSelectionCandidate,
    LibraryImportSurface,
    ModelScopeBinding,
    ResolvedSpecificationToSourceSkill,
    StandardBuildSystemCommandProfile,
    StandardPlatformCommandProfile,
    canonical_identity,
    canonical_json_bytes,
    canonical_relative_posix_paths,
    index_backend_ecosystems,
    source_cache_model_selector,
)
from literate_ai.diagnostics import (
    inherited_verbose_environment,
    log_operation,
    redact_secrets,
    trace_subprocess,
)
from literate_ai.evidence_ledger import EvidenceNode, attach_run, bounded_excerpt
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    MAJOR_REBUILD_GENERATION_MODE,
    GeneratedTestSuiteError,
    generated_test_invocation_signature,
    validate_generated_test_suite,
)

from .generated_source_validation import (
    GeneratedSourceValidationError,
    validate_cpp_bazel_rule_attributes,
    validate_javascript_generation_handoff,
    validate_make_language_tool_quoting,
    validate_rust_bazel_source_closure,
)

if TYPE_CHECKING:
    from literate_ai.application import GenerationExecutionPlan

CODING_CLIS = ("codex", "claude", "cursor-agent", "opencode")
DEFAULT_MAXIMUM_GENERATED_FILES = 1_024
DEFAULT_MAXIMUM_GENERATED_ENTRIES = 4_096
DEFAULT_MAXIMUM_GENERATED_BYTES = 16 * 1024 * 1024
DEFAULT_MAXIMUM_GENERATED_PATH_LENGTH = 512
DEFAULT_MAXIMUM_GENERATED_DEPTH = 32
DEFAULT_MAXIMUM_CLI_STDOUT_BYTES = 1024 * 1024
DEFAULT_MAXIMUM_CLI_STDERR_BYTES = 1024 * 1024
# Source agents stream file-operation progress; small JSON tasks retain the tighter cap.
DEFAULT_MAXIMUM_GENERATION_CLI_STDERR_BYTES = DEFAULT_MAXIMUM_GENERATED_BYTES
_OPENCODE_PREFLIGHT_TIMEOUT_SECONDS = 15
_OPENCODE_PREFLIGHT_OUTPUT_BYTES = 256 * 1024
_OPENCODE_PREFLIGHT_COMMAND = ("--pure", "run", "--help")
_OPENCODE_REQUIRED_HELP_OPTIONS = (b"--pure", b"--dir", b"--agent", b"--format")
_PIPE_READ_BYTES = 64 * 1024
_ACCEPTED_SOURCE_LOOKUP_ONLY_EXECUTABLE = "/accepted-source-lookup-only"
_OPENCODE_PERMISSION = json.dumps(
    {
        "*": "deny",
        "edit": "allow",
        "external_directory": "deny",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "read": "allow",
    },
    sort_keys=True,
    separators=(",", ":"),
)
_OPENCODE_CONFIG_CONTENT = json.dumps(
    {
        "autoupdate": False,
        "instructions": [],
        "mcp": {},
        "permission": json.loads(_OPENCODE_PERMISSION),
        "plugin": [],
        # Pure mode must not inject an organization-private provider. Public built-in
        # providers remain available, and operators can select their own configured
        # provider outside this repository-owned isolation document.
        "provider": {},
        "share": "disabled",
    },
    sort_keys=True,
    separators=(",", ":"),
)
RecipeSkill = ResolvedSpecificationToSourceSkill


class CodingCliError(RuntimeError):
    """A coding CLI could not be selected or did not generate a valid tree."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _DuplicateGeneratedMetadataKey(ValueError):
    pass


def _generated_metadata_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateGeneratedMetadataKey(key)
        value[key] = item
    return value


def _reject_generated_metadata_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant {value!r}")


def _normalize_generated_cyclonedx_order(value: dict[str, object]) -> None:
    """Sort CycloneDX dependency sets without adding, removing, or changing edges."""

    dependencies = value.get("dependencies")
    if not isinstance(dependencies, list):
        return
    for item in dependencies:
        if not isinstance(item, dict):
            continue
        targets = item.get("dependsOn")
        if isinstance(targets, list) and all(
            isinstance(target, str) for target in targets
        ):
            targets.sort()
    if all(
        isinstance(item, dict) and isinstance(item.get("ref"), str)
        for item in dependencies
    ):
        dependencies.sort(key=lambda item: str(item["ref"]))


# A coding CLI writing malformed JSON into a framework-owned metadata file (the
# generated test manifest or the CycloneDX source SBOM) has been observed to be a
# non-deterministic output-quality issue, not a specification or environment
# problem: the same document hash failed then passed on a plain retry with no other
# change (issue #35). Two bounded extra attempts (three total) covers the observed
# 100% single-retry recovery rate with headroom, while still failing closed for a
# genuinely persistent malformed-metadata bug (e.g. issue #39's multi-source-root
# SBOM composition failure, which did not recover across repeated identical-hash
# attempts) rather than retrying forever.
_METADATA_INVALID_RETRY_ATTEMPTS = 2

# The same non-determinism shows up beyond malformed metadata. A generation that times
# out or that the CLI itself reports as failed has repeatedly passed on a plain retry
# with no other change: three consecutive samples-gate failures during 0.5.1 and six
# more during the 0.5.2 backport cycle, each on a different sample, all of which later
# passed untouched (ADR 0008). These share the property that makes a retry meaningful --
# the external subprocess call is itself non-deterministic.
#
# `coding_cli.empty_generation` is deliberately excluded. It looks transient but is
# usually deterministic: a CLI that exits zero writing nothing has almost always been
# denied write access to its workspace, which no number of retries fixes. Retrying it
# would turn a prompt, legible misconfiguration into three times the wall clock before
# the same failure surfaced.
#
# Generated-tree shape failures are also retryable only where repeated live execution
# of the exact same Component authority has produced both a clean tree and the rejected
# shape.  A durable-split API generation did exactly that: one invocation produced a
# source-only tree, while later identical-authority invocations left a root `_build`
# artifact or Python bytecode/SQLite self-test products beneath `source/`.  Admission
# must continue to reject those trees; a bounded fresh-workspace retry addresses the
# non-deterministic producer without weakening the source boundary.
#
# Generated-test contract failures are the same non-determinism with a later validator.
# A coding CLI can emit well-formed JSON whose cases share an argument vector, omit an
# acceptance signature, or mismatch the expected-result shape, then emit a valid suite
# on the next identical prompt. The samples harness already retries those codes when
# they surface as `generation.model-stage-failed`; Standard lifecycle admission wraps
# them as `CodingCliError` with the original `generated_tests.*` code, so they must
# share this generate() retry set or a full samples gate fails closed on one bad draw.
_TRANSIENT_GENERATION_ERROR_CODES = frozenset(
    {
        "coding_cli.generated_javascript_bundle_module_missing",
        "coding_cli.generated_metadata_invalid",
        "coding_cli.generation_failed",
        "coding_cli.non_utf8_source",
        "coding_cli.timeout",
        "coding_cli.unexpected_output",
        "generated_tests.acceptance_signature_missing",
        "generated_tests.duplicate_arguments",
        "generated_tests.expected_result_shape_mismatch",
    }
)


def _reset_generation_output_root(root: Path) -> None:
    """Clear a coding CLI output root between bounded retry attempts.

    `generate()` requires `output_root` to be new or empty on entry, and
    `_generate_in_workspace` may have partially populated it before failing.  A
    retry must observe the same fresh precondition, not layer a second attempt's
    writes over a failed first attempt's partial output.
    """

    if root.is_symlink():
        raise CodingCliError(
            "coding_cli.output_symlink",
            "coding CLI output root cannot be a symlink",
        )
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)


def _canonicalize_generated_framework_metadata(
    root: Path, files: dict[str, str], relative: str
) -> None:
    """Canonicalize framework-owned JSON formatting without repairing semantics."""

    content = files.get(relative)
    if content is None:
        return
    try:
        value = json.loads(
            content,
            object_pairs_hook=_generated_metadata_object,
            parse_constant=_reject_generated_metadata_constant,
        )
        if not isinstance(value, dict):
            raise ValueError("framework metadata must be a JSON object")
        if relative == CYCLONEDX_SOURCE_SBOM_PATH:
            _normalize_generated_cyclonedx_order(value)
        canonical = canonical_json_bytes(value).decode("utf-8")
    except (
        json.JSONDecodeError,
        _DuplicateGeneratedMetadataKey,
        RecursionError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise CodingCliError(
            "coding_cli.generated_metadata_invalid",
            f"coding CLI produced invalid framework metadata: {relative}",
        ) from exc
    if canonical == content:
        return
    metadata_path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        metadata_path.write_text(canonical, encoding="utf-8", newline="")
    except OSError as exc:
        raise CodingCliError(
            "coding_cli.generated_metadata_unavailable",
            f"coding CLI framework metadata could not be canonicalized: {relative}",
        ) from exc
    files[relative] = canonical


@dataclass(frozen=True, slots=True)
class RecipeDocument:
    """One inert, content-identified specification document."""

    path: str
    content: str
    identity: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != self.path
        ):
            raise ValueError("recipe document path must be normalized and relative")
        actual = f"sha256:{hashlib.sha256(self.content.encode('utf-8')).hexdigest()}"
        if self.identity != actual:
            raise ValueError("recipe document identity does not match its content")

    @classmethod
    def create(cls, path: str, content: str) -> RecipeDocument:
        digest = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"
        return cls(path, content, digest)


@dataclass(frozen=True, slots=True)
class RecipeFlavor:
    """Selected Flavor material supplied to the coding model."""

    flavor_id: str
    axis: str
    value: str
    documents: tuple[RecipeDocument, ...]
    models: tuple[tuple[str, str], ...] = ()
    conflicts: tuple[str, ...] = ()
    coordinate_uri: str | None = None
    skills: tuple[RecipeSkill, ...] = ()
    revision_identity: str | None = None
    specification_set_identity: str | None = None
    slot_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.flavor_id or not self.axis or not self.value or not self.documents:
            raise ValueError("recipe Flavor requires identity, axis, value, and spec")
        model_keys = tuple(key for key, _value in self.models)
        if len(set(model_keys)) != len(model_keys):
            raise ValueError("recipe Flavor repeats a coding CLI model")
        if any(key not in CODING_CLIS for key, _value in self.models):
            raise ValueError("recipe Flavor model selection is unsupported")
        for key, value in self.models:
            source_cache_model_selector(
                value,
                path=f"RecipeFlavor.models[{key!r}]",
            )
        if any(not item for item in self.conflicts):
            raise ValueError("recipe Flavor conflict identities cannot be empty")
        if self.coordinate_uri is not None and not self.coordinate_uri:
            raise ValueError("recipe Flavor coordinate cannot be empty")
        if (
            any(not item for item in self.slot_ids)
            or len(set(self.slot_ids)) != len(self.slot_ids)
            or tuple(sorted(self.slot_ids)) != self.slot_ids
        ):
            raise ValueError(
                "recipe Flavor slot identities must be sorted unique non-empty IDs"
            )
        identities = (self.revision_identity, self.specification_set_identity)
        if any(item is None for item in identities) != all(
            item is None for item in identities
        ):
            raise ValueError(
                "recipe Flavor revision and specification identities must be paired"
            )
        for identity in identities:
            if identity is not None:
                ContentIdentity.parse_uri(identity)

    @property
    def reference_ids(self) -> frozenset[str]:
        return frozenset(
            item for item in (self.flavor_id, self.coordinate_uri) if item is not None
        )

    def model_for(self, coding_cli: str) -> str | None:
        return dict(self.models).get(coding_cli)


@dataclass(frozen=True, slots=True)
class RecipeDeploymentUnit:
    """One authored entrypoint projected into its generated source path."""

    name: str
    kind: str
    deployment_unit: str
    source_entrypoint: str
    entrypoint_identity: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(item, str) or not item
            for item in (
                self.name,
                self.kind,
                self.deployment_unit,
            )
        ):
            raise ValueError("recipe deployment unit names must be non-empty strings")
        path = PurePosixPath(self.source_entrypoint)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != "source"
            or path.as_posix() != self.source_entrypoint
        ):
            raise ValueError(
                "recipe deployment-unit entrypoint must be beneath source/"
            )
        ContentIdentity.parse_uri(self.entrypoint_identity)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "deployment_unit": self.deployment_unit,
            "source_entrypoint": self.source_entrypoint,
            "entrypoint_identity": self.entrypoint_identity,
        }


@dataclass(frozen=True, slots=True)
class RecipeLibraryDependency:
    """Exact import surface a consumer must use for one direct library edge."""

    provider_component_revision: ContentIdentity
    capability: str
    interface_identity: ContentIdentity
    import_surface: LibraryImportSurface

    def __post_init__(self) -> None:
        if not isinstance(self.provider_component_revision, ContentIdentity):
            raise ValueError("library dependency provider revision is invalid")
        if not isinstance(self.interface_identity, ContentIdentity):
            raise ValueError("library dependency interface identity is invalid")
        if not isinstance(self.import_surface, LibraryImportSurface):
            raise ValueError("library dependency import surface is invalid")
        try:
            mapping = self.import_surface.capability(self.capability)
        except ContractValidationError as exc:
            raise ValueError("library dependency capability is not exported") from exc
        if mapping.interface_identity != self.interface_identity:
            raise ValueError("library dependency interface differs from import surface")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_component_revision": self.provider_component_revision.to_dict(),
            "capability": self.capability,
            "interface_identity": self.interface_identity.to_dict(),
            "import_surface": self.import_surface.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RecipeCppLibraryBuild:
    layout: CppLibraryLayout
    build_profile: StandardBuildSystemCommandProfile
    platform_profile: StandardPlatformCommandProfile

    def __post_init__(self) -> None:
        if (
            not isinstance(self.layout, CppLibraryLayout)
            or not isinstance(self.build_profile, StandardBuildSystemCommandProfile)
            or not isinstance(self.platform_profile, StandardPlatformCommandProfile)
        ):
            raise ValueError("C++ generation requires typed native build authority")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/recipe-cpp-library-build@1",
            "layout": self.layout.to_dict(),
            "build_profile": self.build_profile.to_dict(),
            "platform_profile": self.platform_profile.to_dict(),
            "output_directory": "library",
            "test_output": "tests/run" + self.platform_profile.executable_suffix,
        }


@dataclass(frozen=True, slots=True)
class GenerationRecipe:
    """Complete source-generation input: base specifications plus Flavor mixins."""

    recipe_id: str
    application_id: str
    documents: tuple[RecipeDocument, ...]
    component_lock_identity: ContentIdentity
    flavors: tuple[RecipeFlavor, ...] = ()
    required_entrypoint: str | None = None
    models: tuple[tuple[str, str], ...] = ()
    skills: tuple[RecipeSkill, ...] = ()
    resolved_inputs: tuple[tuple[str, str], ...] = ()
    required_entrypoints: tuple[str, ...] = ()
    generation_mode: str = MAJOR_REBUILD_GENERATION_MODE
    managed_sbom_graph: CycloneDxManagedGraph | None = None
    model_scope: ModelScopeBinding | None = None
    skill_catalog: tuple[RecipeSkill, ...] = ()
    deployment_units: tuple[RecipeDeploymentUnit, ...] = ()
    library_import_surface: LibraryImportSurface | None = None
    library_dependencies: tuple[RecipeLibraryDependency, ...] = ()
    native_sdk_dependencies: tuple[RecipeNativeSdkDependency, ...] = ()
    cpp_library_build: RecipeCppLibraryBuild | None = None

    def __post_init__(self) -> None:
        if not self.recipe_id or not self.application_id or not self.documents:
            raise ValueError("generation recipe requires identity and specifications")
        if not isinstance(self.component_lock_identity, ContentIdentity):
            raise ValueError(
                "generation recipe requires an exact Component lock identity"
            )
        if self.generation_mode != MAJOR_REBUILD_GENERATION_MODE:
            raise ValueError("generation recipe mode must be 'major-rebuild'")
        flavor_ids = tuple(item.flavor_id for item in self.flavors)
        flavor_revisions = tuple(
            item.revision_identity
            for item in self.flavors
            if item.revision_identity is not None
        )
        if len(set(flavor_ids)) != len(flavor_ids) or len(set(flavor_revisions)) != len(
            flavor_revisions
        ):
            raise ValueError("selected Flavor identities must be unique")
        entrypoints = (
            (self.required_entrypoint,)
            if self.required_entrypoint is not None
            else self.required_entrypoints
        )
        if self.required_entrypoint is not None and self.required_entrypoints:
            raise ValueError(
                "generation recipe cannot mix singular and plural entrypoint fields"
            )
        if len(set(entrypoints)) != len(entrypoints):
            raise ValueError("required entrypoints must be unique")
        for entrypoint in entrypoints:
            path = PurePosixPath(entrypoint)
            if (
                path.is_absolute()
                or not path.parts
                or path.parts[0] != "source"
                or path.as_posix() != entrypoint
            ):
                raise ValueError("required entrypoints must be beneath source/")
        if self.deployment_units:
            if len(self.deployment_units) < 2 or any(
                not isinstance(item, RecipeDeploymentUnit)
                for item in self.deployment_units
            ):
                raise ValueError(
                    "recipe deployment units require at least two typed entries"
                )
            units = tuple(item.deployment_unit for item in self.deployment_units)
            names = tuple(item.name for item in self.deployment_units)
            identities = tuple(
                item.entrypoint_identity for item in self.deployment_units
            )
            if units != tuple(sorted(set(units))) or any(
                len(set(values)) != len(values) for values in (names, identities)
            ):
                raise ValueError(
                    "recipe deployment units must be canonically ordered and unique"
                )
            if {item.source_entrypoint for item in self.deployment_units} != set(
                entrypoints
            ):
                raise ValueError(
                    "recipe deployment units must exactly cover required entrypoints"
                )
        cpp = (
            isinstance(self.library_import_surface, LibraryImportSurface)
            and self.library_import_surface.language == "cpp"
        )
        if cpp:
            if not isinstance(self.cpp_library_build, RecipeCppLibraryBuild):
                raise ValueError(
                    "C++ library generation requires an exact native build declaration"
                )
            if any(
                "include/" + item.module not in self.cpp_library_build.layout.headers
                for item in self.library_import_surface.capabilities
            ):
                raise ValueError("C++ generation layout omits declared public headers")
        elif self.cpp_library_build is not None:
            raise ValueError(
                "C++ build declaration requires a C++ library import surface"
            )
        if self.library_import_surface is not None:
            if not isinstance(self.library_import_surface, LibraryImportSurface):
                raise ValueError("generation recipe library import surface is invalid")
            if self.deployment_units:
                raise ValueError(
                    "library generation cannot declare executable deployment units"
                )
        if any(
            not isinstance(item, RecipeLibraryDependency)
            for item in self.library_dependencies
        ):
            raise ValueError("generation recipe library dependencies are invalid")
        dependency_keys = tuple(
            (item.provider_component_revision.uri, item.capability)
            for item in self.library_dependencies
        )
        if dependency_keys != tuple(sorted(set(dependency_keys))):
            raise ValueError(
                "generation recipe library dependencies must be canonical and unique"
            )
        if not isinstance(self.native_sdk_dependencies, tuple) or any(
            not isinstance(item, RecipeNativeSdkDependency)
            for item in self.native_sdk_dependencies
        ):
            raise TypeError("generation recipe SDK dependencies must be typed")
        sdk_keys = tuple(item.dependency_id for item in self.native_sdk_dependencies)
        if sdk_keys != tuple(sorted(set(sdk_keys))):
            raise ValueError(
                "generation recipe SDK dependencies must be canonical and unique"
            )
        sdk_namespaces = tuple(
            (item.import_surface.language, item.import_surface.package)
            for item in self.native_sdk_dependencies
        )
        library_namespaces = {
            (item.import_surface.language, item.import_surface.package)
            for item in self.library_dependencies
        }
        if self.library_import_surface is not None:
            library_namespaces.add(
                (
                    self.library_import_surface.language,
                    self.library_import_surface.package,
                )
            )
        if (
            len(set(sdk_namespaces)) != len(sdk_namespaces)
            or set(sdk_namespaces) & library_namespaces
        ):
            raise ValueError("generation recipe SDK import namespaces conflict")
        model_keys = tuple(key for key, _value in self.models)
        if len(set(model_keys)) != len(model_keys):
            raise ValueError("generation recipe repeats a coding CLI model")
        if any(key not in CODING_CLIS for key, _value in self.models):
            raise ValueError("generation recipe model selection is unsupported")
        for key, value in self.models:
            source_cache_model_selector(
                value,
                path=f"GenerationRecipe.models[{key!r}]",
            )
        resolution_keys = tuple(key for key, _identity in self.resolved_inputs)
        if len(set(resolution_keys)) != len(resolution_keys) or any(
            not key for key in resolution_keys
        ):
            raise ValueError(
                "resolved input identity labels must be unique and non-empty"
            )
        for _key, identity in self.resolved_inputs:
            ContentIdentity.parse_uri(identity)
        if self.managed_sbom_graph is not None and not isinstance(
            self.managed_sbom_graph, CycloneDxManagedGraph
        ):
            raise ValueError("generation recipe managed SBOM graph is invalid")
        if (
            self.managed_sbom_graph is not None
            and self.managed_sbom_graph.resolved_graph_identity
            != self.component_lock_identity
        ):
            raise ValueError(
                "generation recipe Component lock identity differs from its "
                "managed SBOM graph identity"
            )
        if self.model_scope is not None and not isinstance(
            self.model_scope, ModelScopeBinding
        ):
            raise ValueError("generation recipe model scope is invalid")
        if not _resolved_recipe_skills(
            self.skills, self.flavors, catalog=self.skill_catalog
        ):
            raise ValueError(
                "generation recipe requires a pinned specification-to-source skill"
            )

    @property
    def resolved_skills(self) -> tuple[RecipeSkill, ...]:
        return _resolved_recipe_skills(
            self.skills, self.flavors, catalog=self.skill_catalog
        )

    @property
    def all_required_entrypoints(self) -> tuple[str, ...]:
        """Return the normalized one-or-many generated entrypoint contract."""

        if self.required_entrypoint is not None:
            return (self.required_entrypoint,)
        return self.required_entrypoints

    @property
    def all_documents(self) -> tuple[RecipeDocument, ...]:
        """Return every base and selected-Flavor recipe document in prompt order."""

        return (
            *self.documents,
            *(document for flavor in self.flavors for document in flavor.documents),
        )

    @property
    def model_documents(self) -> tuple[RecipeDocument, ...]:
        """Return generation documents safe to disclose to the coding model."""

        return tuple(
            document
            for document in self.all_documents
            if "acceptance" not in PurePosixPath(document.path).parts
            and not document.path.startswith("dependency-components/")
        )

    @property
    def non_acceptance_document_paths(self) -> tuple[str, ...]:
        """Return exact documents that generated implementation tests may cite."""

        return tuple(sorted({document.path for document in self.model_documents}))

    @property
    def identity(self) -> str:
        value = {
            "recipe_id": self.recipe_id,
            "application_id": self.application_id,
            "generation_mode": self.generation_mode,
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "documents": [
                {"path": item.path, "identity": item.identity}
                for item in self.documents
            ],
            "flavors": [
                {
                    "flavor_id": item.flavor_id,
                    "axis": item.axis,
                    "value": item.value,
                    "documents": [
                        {"path": document.path, "identity": document.identity}
                        for document in item.documents
                    ],
                    "models": list(item.models),
                    "conflicts": list(item.conflicts),
                    "coordinate_uri": item.coordinate_uri,
                    "revision_identity": item.revision_identity,
                    "specification_set_identity": item.specification_set_identity,
                    "skills": [skill.to_dict() for skill in item.skills],
                }
                for item in self.flavors
            ],
            "required_entrypoint": self.required_entrypoint,
            "models": list(self.models),
            "skills": [skill.to_dict() for skill in self.skills],
            "closed_skills": [skill.to_dict() for skill in self.resolved_skills],
            "resolved_inputs": [list(item) for item in self.resolved_inputs],
            "managed_sbom_graph_identity": (
                None
                if self.managed_sbom_graph is None
                else self.managed_sbom_graph.identity.uri
            ),
            "model_scope": (
                None if self.model_scope is None else self.model_scope.to_dict()
            ),
        }
        flavor_bindings = [
            {
                "slot_id": slot_id,
                "flavor_id": flavor.flavor_id,
                "revision_identity": flavor.revision_identity,
            }
            for flavor in self.flavors
            for slot_id in flavor.slot_ids
        ]
        if flavor_bindings:
            value["flavor_bindings"] = flavor_bindings
        if self.required_entrypoints:
            value["required_entrypoints"] = list(self.required_entrypoints)
        if self.deployment_units:
            value["deployment_units"] = [
                item.to_dict() for item in self.deployment_units
            ]
        if self.library_import_surface is not None:
            value["library_import_surface"] = self.library_import_surface.to_dict()
        if self.cpp_library_build is not None:
            value["cpp_library_build"] = self.cpp_library_build.to_dict()
        if self.library_dependencies:
            value["library_dependencies"] = [
                item.to_dict() for item in self.library_dependencies
            ]
        if self.native_sdk_dependencies:
            value["native_sdk_dependencies"] = [
                item.to_dict() for item in self.native_sdk_dependencies
            ]
        return canonical_identity(value).uri

    def model_for(self, coding_cli: str) -> str | None:
        if self.model_scope is not None:
            if self.model_scope.provider_id != coding_cli:
                raise CodingCliError(
                    "coding_cli.model_scope_provider_mismatch",
                    "resolved model scope selects another coding CLI provider",
                )
            return self.model_scope.explicit_model
        flavor_models = {
            model
            for flavor in self.flavors
            if (model := flavor.model_for(coding_cli)) is not None
        }
        if len(flavor_models) > 1:
            raise CodingCliError(
                "coding_cli.model_conflict",
                f"selected Flavors disagree on the {coding_cli} model",
            )
        if flavor_models:
            return next(iter(flavor_models))
        return dict(self.models).get(coding_cli)

    def prompt(self, *, include_locked_authority_documents: bool = True) -> str:
        """Render the generation prompt.

        Bounded Component generation appends exact locked specification and direct
        interface bytes as separately identity-bound authority segments.  Its
        framework envelope therefore sets ``include_locked_authority_documents``
        to false so those bytes cross the coding-agent boundary exactly once.
        Standalone and legacy callers retain the historical self-contained prompt.
        """

        sections = [
            "# Literate AI source-generation request",
            "",
            f"Recipe identity: `{self.identity}`",
            f"Application: `{self.application_id}`",
            f"Generation mode: `{self.generation_mode}`",
            f"Component lock identity: `{self.component_lock_identity.uri}`",
            "",
            "Generate the complete application solely from the specifications below.",
            "Write implementation files only beneath `source/` in the current",
            "workspace. Do not read another source tree, download dependencies,",
            "invoke a shell, or hard-code one specification example as the result.",
            "Implement the behavior generally, using only dependencies explicitly",
            "permitted by the recipe. Do not write plans, prose, caches, build",
            "products, or files outside `source/`.",
            "Any appended `authored-binary-asset` metadata identifies a",
            "non-generated immutable overlay. Do not create, infer, copy, replace,",
            "or modify its `path`; the framework adds the verified bytes after",
            "generation. Generate code and tests that consume the assembled path,",
            "but do not require those bytes to exist in this fresh model workspace.",
            "",
            "## Disposable generated implementation-test suite",
            "",
            f"Create `{GENERATED_TEST_SUITE_PATH}` from this exact recipe during",
            "this major rebuild. It is required generated source-tree content, not",
            "durable acceptance evidence, and no prior generated suite may be read",
            "or preserved. The manifest object must have exactly `schema`,",
            "`recipe_identity`, `generation_mode`, and `cases`; use schema",
            "`urn:literate-ai:schema:v1:generated-test-suite`, the exact recipe",
            f"identity `{self.identity}`, and generation mode",
            f"`{MAJOR_REBUILD_GENERATION_MODE}`.",
            "",
            "Generate 3 through 256 cases. Every case must have exactly `case_id`,",
            "`category`, `specification_refs`, `arguments`, and `expected_result`.",
            "Case IDs and JSON-array argument vectors must each be unique. Include",
            "at least one case in each category: `example`, `boundary`, and",
            "`invariant`. Every case must cite one or more exact current",
            "non-acceptance recipe documents, chosen only from:",
            *(f"- `{path}`" for path in self.non_acceptance_document_paths),
            "",
            "Copy each `specification_refs` entry from that list byte for byte. Do",
            "not append a heading anchor, section title, requirement ID, line",
            "number, or `#fragment`; do not substitute a requirement name for its",
            "document path; do not invent a path the list does not contain.",
            "`specification_refs` names the document a case came from, never the",
            "requirement inside it.",
            "",
            "Derive expected results solely from those specifications. The generated",
            "suite must exercise general behavior and must not become an acceptance",
            "oracle or verifier input. Every `expected_result` must be the complete",
            "application result for its arguments according to the exact current",
            "specifications: include every required object key, include no invented",
            "summary or assertion-only fields, and never use a partial result as a",
            "test expectation.",
            "Use only expected values you can derive exactly and independently",
            "from the specification. Recheck all arithmetic. Never guess a hash,",
            "digest, encoded value, timestamp, identifier, or other opaque output;",
            "choose a well-known exact test vector or a different argument instead.",
            "Every manifest case must have a corresponding language-native behavior",
            "test that invokes the application logic with the same arguments and",
            "asserts the same complete expected result. Make every such test reachable",
            "from the generated build system's ordinary test target, including",
            "`bazel test //...` when Bazel is selected. Recalculate each expectation",
            "independently before encoding it in both places. Never read, embed,",
            "include, or parse the manifest from application or test code at compile",
            "time or runtime: it is lifecycle metadata, while the corresponding native",
            "tests are executable evidence.",
        ]
        execution_contract = _generation_safe_execution_contract(self)
        if execution_contract is not None:
            sections.extend(
                [
                    "",
                    "## Generation-safe invocation contract",
                    "",
                    "The independent acceptance values and expected results remain",
                    "withheld. The following value-free contract is authoritative for",
                    "callable arity, recursive object fields, and complete result",
                    "shape.",
                    "Every generated implementation-test suite must contain at least",
                    "one independently derived case for every listed invocation",
                    "signature. A generated case may coincidentally use the same",
                    "input as a withheld acceptance case; that does not merge their",
                    "separate authority or expected-result provenance. Additionally,",
                    "every expected result must match the complete result shape.",
                    "Language-native tests and the application entrypoint must invoke",
                    "the same callable",
                    "surface; do not translate a one-object argument into multiple",
                    "positional parameters or vice versa.",
                    "",
                    canonical_json_bytes(execution_contract).decode("utf-8"),
                ]
            )
        if self.managed_sbom_graph is not None:
            managed_graph = canonical_json_bytes(
                self.managed_sbom_graph.to_dict()
            ).decode("utf-8")
            authority_components, authority_edges = _recipe_authority_sbom(self)
            minimal_source_bom, _minimal_binding = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=self.managed_sbom_graph,
                additional_components=authority_components,
                additional_edges=authority_edges,
                # The example is an exact minimal graph and therefore complete.
                # Merely selecting Bazel does not imply a deferred dependency.
                # A generated literal ``bazel_dep`` is the only supported reason
                # for the coding agent to change this to
                # ``incomplete_third_party_only``.
                composition_aggregate="complete",
            )
            minimal_source_bom_text = minimal_source_bom.decode("utf-8")
            sections.extend(
                [
                    "",
                    "## Required CycloneDX source SBOM",
                    "",
                    f"Create `{CYCLONEDX_SOURCE_SBOM_PATH}` as canonical minified",
                    "JSON conforming strictly to CycloneDX 1.7, with `$schema`",
                    f"exactly `{CYCLONEDX_SCHEMA_URI}`, `bomFormat` `CycloneDX`,",
                    "`specVersion` `1.7`, and BOM `version` 1. The metadata must",
                    "contain exactly lifecycle phase `pre-build`, the exact",
                    "application root component, and no timestamp; omit",
                    "`serialNumber`.",
                    "Set metadata property",
                    "`literate-ai:resolved-graph-identity` to the exact resolved",
                    "Component graph identity",
                    f"`{self.managed_sbom_graph.resolved_graph_identity.uri}`. This",
                    "property is mandatory in both source and resolved BOM artifacts",
                    "and must never be inferred, replaced, or omitted.",
                    "",
                    "Every inventory object must have a unique `bom-ref`, an explicit",
                    "dependency entry, and an explicit empty `dependsOn` array when it",
                    "is a leaf. Sort dependency entries by `ref` and every `dependsOn`",
                    "array. Include exactly one composition whose `dependencies`",
                    "array contains only the root ref. Use aggregate `complete` only",
                    "when every dependency relationship is already known. If a",
                    "generated manifest deliberately defers a third-party transitive",
                    "closure to the authorized build resolver, including a literal",
                    "Bzlmod `bazel_dep`, use `incomplete_third_party_only`. No other",
                    "incomplete aggregate is accepted. Every",
                    "component must be reachable from that root.",
                    "",
                    "Include every direct and transitive package, toolchain, build,",
                    "test, runtime, and system binary dependency known from the exact",
                    "specifications, selected Flavors, manifests, and lock data. Give",
                    "every non-root object one `literate-ai:dependency-kind` property",
                    "(`package`, `toolchain`, `build`, `test`, `runtime`, or `system`)",
                    "and one or more `literate-ai:dependency-scope` properties. Use an",
                    "exact version when resolved. An unpinned host-provided runtime",
                    "may instead use CycloneDX 1.7 `isExternal: true` with a valid",
                    "Package URL Version Range `versionRange`. Do not invent a",
                    "dependency, resolution, version, hash, or repository revision.",
                    "An approximate value such as `3.11+` is never an exact `version`.",
                    "For an unpinned external dependency, omit `version` entirely and",
                    (
                        "use only the specification-derived `versionRange`. Never emit "
                        "both"
                    ),
                    "an approximate `version` and `versionRange`.",
                    "A Bzlmod `bazel_dep` version is a requested version subject to",
                    "Minimal Version Selection, not a final resolution. Represent each",
                    "direct request as an external CycloneDX component whose `purl`",
                    "and `bom-ref` are `pkg:generic/<module-name>`, whose",
                    "CycloneDX `type` is exactly `library`, whose",
                    "`versionRange` is `vers:generic/>=<requested-version>`, and",
                    "whose `isExternal` value is the JSON boolean `true`. It MUST",
                    "omit `version` and hashes. Give it build kind and build scope,",
                    "a `literate-ai:bzlmod-requested-version` property containing",
                    "the literal request, an explicit leaf dependency entry, and a",
                    "direct root edge. Do not fabricate its",
                    "transitive graph, selected version, repository integrity, or",
                    "MODULE.bazel.lock; the authorized Bazel builder supplies those.",
                    "",
                    "The following framework-managed subgraph is mandatory in both",
                    "source and resolved BOMs. Convert each node into a CycloneDX",
                    "component with its exact `bom_ref`, `name`, and `version` when",
                    "present, plus its exact binding identity property, kind",
                    "property, and scope properties. Preserve every managed edge's",
                    "exact source, target, dependency kind, optionality, and",
                    "relationship identity as dependency-edge evidence on its source",
                    "component. The owner map is total: even a managed Component with",
                    "no repository-source children remains an explicit dependency",
                    "leaf. Additional dependency nodes and edges may connect to this",
                    "subgraph but may not replace, collapse, or alter it:",
                    "",
                    managed_graph,
                    "",
                    "The following is the exact canonical minimal CycloneDX source BOM",
                    (
                        "for that managed subgraph. If the generated implementation "
                        "uses no"
                    ),
                    "additional non-standard dependency, write this object unchanged.",
                    "Language standard-library modules do not require an invented",
                    (
                        "toolchain component here; the resolved build SBOM records the "
                        "exact"
                    ),
                    (
                        "host toolchain and system binary closure. If and only if "
                        "generated"
                    ),
                    "manifests, locks, or imports introduce another real dependency,",
                    (
                        "extend this object while preserving every existing field and "
                        "edge:"
                    ),
                    "",
                    minimal_source_bom_text,
                ]
            )
        if self.all_required_entrypoints:
            sections.extend(
                [
                    "",
                    (
                        "The required generated entrypoint is"
                        if len(self.all_required_entrypoints) == 1
                        else "The required generated entrypoints are"
                    ),
                    ", ".join(
                        f"`{entrypoint}`"
                        for entrypoint in self.all_required_entrypoints
                    )
                    + ".",
                ]
            )
        if self.library_import_surface is not None:
            sections.extend(
                [
                    "",
                    "## Required importable library artifact",
                    "",
                    "This Component is a library. It has no product entrypoint. The",
                    "language Flavor's conventional main file is framework-owned",
                    "generated-test dispatch only: `--litai-test` must execute every",
                    "generated case through the real exported library API and emit the",
                    "attributable `literate-ai/generated-test-results@1` protocol.",
                    "Materialize every package, module, and symbol in this exact",
                    "locked import surface; never rely on an ambient workspace or",
                    "package path:",
                    "```json",
                    json.dumps(
                        self.library_import_surface.to_dict(),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "```",
                    "The framework independently imports those exact symbols from the",
                    "sealed directory artifact after generated tests pass.",
                ]
            )
        if self.cpp_library_build is not None:
            sections.extend(
                [
                    "",
                    "## Required compiled C++ library build",
                    "",
                    "Use native rules_cc library and test targets. Compile the library",
                    "implementation separately from its generated test adapter; the",
                    "product is a compiled library with no executable entrypoint.",
                    "Write public headers beneath source/include using exactly the",
                    "declared include-relative module paths. Keep these public headers",
                    "self-contained or include only other declared public headers and",
                    "the standard library. Keep private source outside exports.",
                    "The Bazel target below must produce the declared tree output.",
                    "Stage precisely the layout files plus the separate generated test",
                    "executable, which must implement the --litai-test case protocol.",
                    "Use a portable declared Bazel staging action; do not wrap native",
                    "compilation in a shell script. Exclude producer source and tests",
                    "from consumer files. Shared libraries must be relocatable",
                    "with declared runtime files and Windows import libraries.",
                    "```json",
                    json.dumps(self.cpp_library_build.to_dict(), sort_keys=True),
                    "```",
                ]
            )
        if self.library_dependencies:
            sections.extend(
                [
                    "",
                    "## Required direct library imports",
                    "",
                    "Use each exact package/module/symbol mapping below for its",
                    "required capability. Do not copy provider source, invent an",
                    "alternate import, or resolve a same-named ambient package. The",
                    "build lifecycle will supply the accepted sealed provider",
                    "artifact matching this pre-generation authority:",
                    "```json",
                    json.dumps(
                        [item.to_dict() for item in self.library_dependencies],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "```",
                ]
            )
        if self.native_sdk_dependencies:
            sections.extend(
                [
                    "",
                    "## Required admitted native SDK imports",
                    "",
                    "Use these exact public SDK imports and integration contracts.",
                    "The lifecycle supplies the verified SDK files. Do not install",
                    "same-named packages, copy vendor source, add ambient paths,",
                    "or simulate native results. Generated tests must exercise the",
                    "declared SDK behavior. These records grant no permission to",
                    "build, load native code, or bypass host authorization.",
                    "For Python, the verified runtime supplies the public module",
                    "`literate_ai_native_sdk`. Call `binding(package_name)` to obtain",
                    "a read-only mapping with `sdk_snapshot_identity` and",
                    "`target_identity` strings for that exact SDK package. Use this",
                    "runtime projection when returning SDK identities; do not",
                    "transcribe, calculate, or fabricate opaque identity constants.",
                    "Missing or ambiguous packages raise `LookupError`; a missing",
                    "module means this runtime did not supply SDK metadata. Do not",
                    "install, shadow, or simulate that module. Its metadata is not",
                    "an execution grant and never replaces actual native validation.",
                    "It is supplied by the Standard runtime, not a package dependency;",
                    "do not add a package requirement or vendored file for it.",
                    "```json",
                    json.dumps(
                        [item.to_dict() for item in self.native_sdk_dependencies],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "```",
                ]
            )
        if self.deployment_units:
            sections.extend(
                [
                    "",
                    "## Required deployment units",
                    "",
                    "Generate every unit below as an independently invocable surface;",
                    "do not collapse units or route all entrypoints through one "
                    "primary",
                    "implementation:",
                    canonical_json_bytes(
                        [item.to_dict() for item in self.deployment_units]
                    ).decode("utf-8"),
                ]
            )
        if any(
            flavor.axis == "build.system" and flavor.value == "bazel"
            for flavor in self.flavors
        ):
            sections.extend(
                [
                    "",
                    "## Required generated Bazel source boundary",
                    "",
                    "The `build.system=bazel` Flavor remains selected for",
                    "this exact recipe. Unless an exact Component or selected Flavor",
                    "document below explicitly requires an incompatible build system,",
                    "you MUST create both `source/MODULE.bazel` and",
                    "`source/BUILD.bazel`. Do not omit them merely because the",
                    "application can also be compiled by a native language toolchain.",
                    "`source/MODULE.bazel` must declare the root module and each exact",
                    "direct language-ruleset request. `source/BUILD.bazel` must load",
                    "those rules, define a complete runnable `//:run` target, and",
                    "define",
                    "real test targets discoverable by `bazel test //...`. The",
                    "authorized post-generation Bazel builder—not this generation",
                    "step—will resolve Bzlmod, create the lock, analyze, and build.",
                    "If an exact incompatible build-system requirement does appear,",
                    "follow it and make the conflict explicit in the generated build",
                    "surface; never silently omit a selected Flavor requirement.",
                ]
            )
        if self.resolved_inputs:
            sections.extend(["", "## Exact resolved domain inputs", ""])
            sections.extend(
                f"- {label}: `{identity}`" for label, identity in self.resolved_inputs
            )
        if self.model_scope is not None:
            sections.extend(
                [
                    "",
                    "## Exact lexical model scope",
                    "",
                    f"Binding identity: `{self.model_scope.identity.uri}`",
                    "The provider/model selection and its complete immutable parent "
                    "trace are:",
                    "```json",
                    json.dumps(
                        self.model_scope.to_dict(),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "```",
                ]
            )
        languages = tuple(
            flavor.value
            for flavor in self.flavors
            if flavor.axis == "implementation.language-ecosystem"
        )
        role_languages = {
            slot_id: flavor.value
            for flavor in self.flavors
            if flavor.axis == "implementation.language-ecosystem"
            for slot_id in flavor.slot_ids
        }
        if role_languages:
            sections.extend(
                [
                    "",
                    "## Exact Component Flavor role bindings",
                    "",
                    *(
                        f"- `{slot_id}`: `implementation.language-ecosystem={language}`"
                        for slot_id, language in sorted(role_languages.items())
                    ),
                ]
            )
        # Domain topology (role wiring, output shapes, per-role file layout) is
        # Component authority: it travels in the locked specification documents
        # appended to this envelope, never as a framework special case. See
        # samples/full-stack-rust-js/component.md for the reference protocol.
        if languages == ("cpp",):
            sections.extend(
                [
                    "",
                    "For the C++ Flavor, the native process receives the complete JSON",
                    "`arguments` array as its one and only command-line argument",
                    "(`argv[1]`). Do not read the request from standard input.",
                    "Put the framework-mode implementation in exactly",
                    "`source/tests/litai_test.cpp`; it must define the generated-test",
                    "and smoke functions used by `--litai-test` and `--litai-smoke`,",
                    "and it is linked into the runnable artifact. Keep that",
                    "translation",
                    "unit self-contained where practical. If it includes a sibling",
                    "header under `source/tests/`, use the sibling spelling such as",
                    '`#include "litai_test.hpp"`; never prefix it with `tests/`,',
                    "because the compiler already resolves quoted includes relative to",
                    "the including file. Place any standalone",
                    "test programs beneath `source/tests/`, or name them `test.cpp`,",
                    "`test_*.cpp`, or `*_test.cpp`, so they are compiled only by test",
                    "targets and are not linked into `//:run`.",
                    "Emit only",
                    "the JSON result on standard output.",
                ]
            )
        elif languages == ("python",) and self.library_import_surface is not None:
            sections.extend(
                [
                    "",
                    "For the Python library Flavor, create ordinary package modules",
                    "beneath `source/` exactly as named by the locked import surface.",
                    "Keep `source/main.py` as a non-product `--litai-test` adapter; it",
                    "must import the package and execute the generated native behavior",
                    "cases without reading `source/tests/manifest.json`.",
                ]
            )
        elif languages == ("python",):
            sections.extend(
                [
                    "",
                    "For the Python Flavor, expose a callable `main` at the required",
                    "entrypoint. Parse one complete UTF-8 JSON arguments array from",
                    "`argv[1]` and call `main(*arguments)` — spreading the array's",
                    "elements as `main`'s positional arguments, exactly as the",
                    "skill's `--litai-test`/`--litai-smoke` dispatcher must also do.",
                    "A specification describing one JSON object as its argument",
                    "receives `[{...}]` on the command line; unwrap that one-element",
                    "array before reading object fields. If packaging produces a",
                    "zipapp or other wrapper entrypoint, that wrapper's own argv",
                    "handling must derive from `sys.argv[1:]`, never the unmodified",
                    "`sys.argv` (which retains the program path at index 0 and",
                    "silently shifts every downstream argument by one position). Do",
                    "not",
                    "require standard input to execute the application.",
                ]
            )
        elif languages == ("rust",) and self.library_import_surface is not None:
            sections.extend(
                [
                    "",
                    "For the Rust library Flavor, create the Cargo library package at",
                    "`source/Cargo.toml` with its public modules in",
                    "`source/src/lib.rs`.",
                    "Also declare the profile-named binary as a framework-only",
                    "`--litai-test` adapter that calls the exported crate API and",
                    "emits the attributable generated-test protocol. It is not a",
                    "product entrypoint and the lifecycle exports the package tree,",
                    "not the binary.",
                ]
            )
        elif languages == ("rust",):
            sections.extend(
                [
                    "",
                    "For the Rust Flavor, the native process receives the complete",
                    "JSON",
                    "`arguments` array in `argv[1]`. Do not read standard input. Emit",
                    "only the JSON result on standard output. Put the generated Rust",
                    "behavior-test implementation in `source/tests/litai_test.rs`,",
                    "include it unconditionally from `source/main.rs` so the exported",
                    "binary's `--litai-test` mode can execute it, and list both files",
                    "in the `srcs` of every `rust_binary` or `rust_test` that compiles",
                    "`main.rs`. Bazel sandboxes expose only declared inputs; a Rust",
                    '`#[path = "tests/litai_test.rs"]` declaration does not add that',
                    "file to a target automatically. Do not hide the runtime test",
                    "module behind `#[cfg(test)]`.",
                    f"`{GENERATED_TEST_SUITE_PATH}` is framework lifecycle metadata,",
                    "It is not Rust compile-time test data; do not use `include_str!`",
                    "on it,",
                    "copy it into a Rust test, or create a test whose only purpose is",
                    "checking that the manifest exists. The framework validates and",
                    "executes its cases independently.",
                ]
            )
        elif languages == ("javascript",) and self.library_import_surface is not None:
            sections.extend(
                [
                    "",
                    "For the dependency-free JavaScript library Flavor, create",
                    "CommonJS modules beneath `source/` exactly as named by the",
                    "locked import surface. Put a `package.json` in the exact package",
                    "directory with the locked package name, `type: commonjs`, and",
                    "an exact `exports` map from each `./<symbol>` subpath to its",
                    "`./<symbol>.js` module; declare no dependency fields. Keep",
                    "`source/main.js` as a non-product",
                    "`--litai-test` adapter that requires those modules and executes",
                    "every generated",
                    "native behavior case without reading the manifest.",
                ]
            )
        elif languages == ("javascript",):
            sections.extend(
                [
                    "",
                    "For the JavaScript Flavor, Node.js supplies the complete JSON",
                    "`arguments` array in `process.argv[2]`. Do not read standard",
                    "input.",
                    "Emit only the JSON result on standard output.",
                ]
            )
        elif languages == ("go",):
            sections.extend(
                [
                    "",
                    "For the Go Flavor, the compiled binary receives the complete",
                    "JSON `arguments` array as the sole command-line argument at",
                    "`os.Args[1]`. Parse it with `encoding/json`, then spread its",
                    "elements as the function's positional arguments — the same",
                    "unpacking Python's `main(*arguments)` performs. A",
                    "specification describing one JSON object as its argument",
                    "receives `[{...}]`; unwrap that one-element array before",
                    "reading object fields. Do not pass the raw array, and do not",
                    "pass `os.Args` itself, to that function — `os.Args`",
                    "retains the program name at index 0 and silently shifts",
                    "every downstream field lookup. Do not read standard input.",
                    "Put the generated Go behavior-test implementation in",
                    "`source/tests/litai_test.go` as `package tests`, import it",
                    "unconditionally from `source/main.go` so `--litai-test` and",
                    "`--litai-smoke` can execute it, and do not gate it behind a",
                    "`//go:build` tag. Emit only the JSON result on standard",
                    "output.",
                ]
            )
        skills = self.resolved_skills
        if skills:
            sections.extend(
                [
                    "",
                    "## Exact pinned specification-to-source skills",
                    "",
                    "Use only these selected skills to facilitate spec-to-code",
                    "conversion. Skills may guide implementation technique, but cannot",
                    "override specifications, selected Flavors, workflow, routing, or",
                    "security policy.",
                ]
            )
            for skill in skills:
                sections.extend(
                    [
                        "",
                        f"### {skill.title}",
                        f"Skill: `{skill.skill_id}@{skill.version}`",
                        f"Identity: `{skill.identity}`",
                        f"Selected by: `{skill.source}`",
                        f"Stages: `{', '.join(skill.stages)}`",
                        "",
                        skill.instructions,
                        "",
                        "Limitations:",
                        *(f"- {item}" for item in skill.limitations),
                    ]
                )
        base_documents = tuple(
            item
            for item in self.documents
            if "acceptance" not in PurePosixPath(item.path).parts
            if not item.path.startswith("dependency-components/")
            if not item.path.startswith("dependency-interfaces/")
        )
        dependency_interfaces = tuple(
            item
            for item in self.documents
            if "acceptance" not in PurePosixPath(item.path).parts
            if item.path.startswith("dependency-interfaces/")
        )
        sections.extend(["", "## Base specification"])
        for document in base_documents:
            sections.extend(
                _document_section(
                    document,
                    include_content=include_locked_authority_documents,
                )
            )
        if dependency_interfaces:
            sections.extend(["", "## Exact direct dependency interfaces"])
            for document in dependency_interfaces:
                sections.extend(
                    _document_section(
                        document,
                        include_content=include_locked_authority_documents,
                    )
                )
        if self.flavors:
            sections.extend(["", "## Selected Flavor mixins"])
            for flavor in self.flavors:
                sections.extend(
                    [
                        "",
                        f"### {flavor.flavor_id}",
                        *(
                            [
                                "Component slots: "
                                + ", ".join(f"`{item}`" for item in flavor.slot_ids)
                            ]
                            if flavor.slot_ids
                            else []
                        ),
                        f"Axis/value: `{flavor.axis}={flavor.value}`",
                    ]
                )
                for document in flavor.documents:
                    if "acceptance" in PurePosixPath(document.path).parts:
                        continue
                    sections.extend(_document_section(document))
        return "\n".join(sections) + "\n"


def _recipe_authority_sbom(
    recipe: GenerationRecipe,
) -> tuple[tuple[dict[str, object], ...], tuple[tuple[str, str], ...]]:
    """Project selected Flavor/skill authority into the existing SBOM graph."""

    if recipe.managed_sbom_graph is None:
        return (), ()
    components: list[dict[str, object]] = []
    for flavor in recipe.flavors:
        if flavor.revision_identity is None:
            raise CodingCliError(
                "sbom.flavor-revision-missing",
                "selected Flavor lacks an exact revision for SBOM projection",
            )
        digest = ContentIdentity.parse_uri(flavor.revision_identity).digest
        components.append(
            {
                "type": "framework",
                "bom-ref": f"urn:literate-ai:flavor:{digest}",
                "name": flavor.coordinate_uri or flavor.flavor_id,
                "version": f"sha256-{digest}",
                "properties": [
                    {
                        "name": "literate-ai:flavor-revision",
                        "value": flavor.revision_identity,
                    },
                    {"name": "literate-ai:dependency-kind", "value": "flavor"},
                    {"name": "literate-ai:dependency-scope", "value": "generation"},
                ],
            }
        )
    for skill in recipe.resolved_skills:
        digest = skill.content_identity.digest
        components.append(
            {
                "type": "framework",
                "bom-ref": f"urn:literate-ai:skill:{digest}",
                "name": skill.skill_id,
                "version": skill.version,
                "properties": [
                    {
                        "name": "literate-ai:skill-revision",
                        "value": skill.identity,
                    },
                    {"name": "literate-ai:dependency-kind", "value": "skill"},
                    {"name": "literate-ai:dependency-scope", "value": "generation"},
                ],
            }
        )
    for unit in recipe.deployment_units:
        digest = unit.identity.digest
        components.append(
            {
                "type": "application",
                "bom-ref": f"urn:literate-ai:deployment-unit:{digest}",
                "name": unit.deployment_unit,
                "version": f"sha256-{digest}",
                "properties": [
                    {
                        "name": "literate-ai:deployment-unit-identity",
                        "value": unit.identity.uri,
                    },
                    {
                        "name": "literate-ai:entrypoint-identity",
                        "value": unit.entrypoint_identity,
                    },
                    {"name": "literate-ai:entrypoint-name", "value": unit.name},
                    {"name": "literate-ai:entrypoint-kind", "value": unit.kind},
                    {
                        "name": "literate-ai:entrypoint-source",
                        "value": unit.source_entrypoint,
                    },
                    {"name": "literate-ai:dependency-kind", "value": "runtime"},
                    {"name": "literate-ai:dependency-scope", "value": "generation"},
                    {"name": "literate-ai:dependency-scope", "value": "runtime"},
                ],
            }
        )
    ordered = tuple(sorted(components, key=lambda item: str(item["bom-ref"])))
    return ordered, tuple(
        (recipe.managed_sbom_graph.root_ref, str(item["bom-ref"])) for item in ordered
    )


def _require_recipe_authority_sbom(content: bytes, recipe: GenerationRecipe) -> None:
    """Reject generated BOMs that omit or rewrite selected Flavor/skill authority."""

    components, edges = _recipe_authority_sbom(recipe)
    if not components:
        return
    document = json.loads(content)
    inventory = {
        item.get("bom-ref"): item
        for item in document.get("components", [])
        if isinstance(item, dict)
    }
    dependencies = {
        item.get("ref"): tuple(item.get("dependsOn", ()))
        for item in document.get("dependencies", [])
        if isinstance(item, dict)
    }
    if any(inventory.get(item["bom-ref"]) != item for item in components):
        raise CycloneDxBomError(
            "sbom.generation-authority-incomplete",
            "CycloneDX BOM omits or rewrites selected Flavor or skill authority",
        )
    if any(target not in dependencies.get(source, ()) for source, target in edges):
        raise CycloneDxBomError(
            "sbom.generation-authority-incomplete",
            "CycloneDX BOM omits a selected Flavor or skill dependency edge",
        )


def _reconcile_authoritative_source_sbom(
    recipe: GenerationRecipe, workspace: Path, files: dict[str, str]
) -> None:
    """Canonicalize framework authority while retaining third-party observations."""

    managed_graph = recipe.managed_sbom_graph
    if managed_graph is None:
        return
    existing = files.get(CYCLONEDX_SOURCE_SBOM_PATH)
    if existing is None:
        return
    authority_components, authority_edges = _recipe_authority_sbom(recipe)
    canonical_bytes, _binding = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=managed_graph,
        additional_components=authority_components,
        additional_edges=authority_edges,
        composition_aggregate="complete",
    )
    try:
        model_document = json.loads(existing)
        canonical_document = json.loads(canonical_bytes)
        if not isinstance(model_document, dict):
            raise ValueError("CycloneDX source SBOM is not an object")
        model_metadata = model_document.get("metadata")
        model_components = model_document.get("components")
        model_dependencies = model_document.get("dependencies")
        if not isinstance(model_metadata, dict):
            raise ValueError("CycloneDX metadata is malformed")
        if not isinstance(model_components, list):
            raise ValueError("CycloneDX components are malformed")
        if not isinstance(model_dependencies, list):
            raise ValueError("CycloneDX dependencies are malformed")

        canonical_root = canonical_document["metadata"]["component"]
        canonical_components = {
            component["bom-ref"]: component
            for component in canonical_document["components"]
        }
        authority_refs = {managed_graph.root_ref, *canonical_components}
        model_inventory_refs = {managed_graph.root_ref}
        third_party_components: dict[str, dict[str, object]] = {}
        reserved_kinds = {
            *(component.kind.value for component in managed_graph.components),
            "flavor",
            "skill",
        }
        reserved_properties = {
            "literate-ai:component-revision",
            "literate-ai:repository-source-dependency",
            "literate-ai:flavor-revision",
            "literate-ai:skill-revision",
            "literate-ai:dependency-edge",
        }
        reserved_ref_prefixes = (
            "urn:literate-ai:component:",
            "urn:literate-ai:repository-source:",
            "urn:literate-ai:flavor:",
            "urn:literate-ai:skill:",
        )
        for raw in model_components:
            if not isinstance(raw, dict):
                raise ValueError("CycloneDX component is not an object")
            ref = raw.get("bom-ref")
            if not isinstance(ref, str) or not ref or ref in model_inventory_refs:
                raise ValueError("CycloneDX component bom-ref is missing or duplicated")
            model_inventory_refs.add(ref)
            if ref in authority_refs:
                continue
            properties = raw.get("properties", [])
            if not isinstance(properties, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("value"), str)
                for item in properties
            ):
                raise ValueError("CycloneDX component properties are malformed")
            property_pairs = {(item["name"], item["value"]) for item in properties}
            if (
                ref.startswith(reserved_ref_prefixes)
                or any(name in reserved_properties for name, _value in property_pairs)
                or any(
                    isinstance(name, str)
                    and name.startswith("literate-ai:")
                    and name
                    not in {
                        "literate-ai:bzlmod-requested-version",
                        "literate-ai:dependency-kind",
                        "literate-ai:dependency-scope",
                    }
                    for name, _value in property_pairs
                )
                or any(
                    name == "literate-ai:dependency-kind" and value in reserved_kinds
                    for name, value in property_pairs
                )
            ):
                raise ValueError(
                    "third-party CycloneDX component claims framework authority"
                )
            third_party_components[ref] = raw

        inventory_refs = model_inventory_refs | authority_refs
        model_targets: dict[str, tuple[str, ...]] = {}
        for raw in model_dependencies:
            if not isinstance(raw, dict):
                raise ValueError("CycloneDX dependency entry is not an object")
            ref = raw.get("ref")
            targets = raw.get("dependsOn")
            if (
                not isinstance(ref, str)
                or ref in model_targets
                or ref not in inventory_refs
                or not isinstance(targets, list)
                or any(not isinstance(target, str) for target in targets)
                or len(set(targets)) != len(targets)
                or any(target not in inventory_refs for target in targets)
            ):
                raise ValueError("CycloneDX dependency graph is conflicting or unsafe")
            model_targets[ref] = tuple(targets)
        if set(third_party_components) - set(model_targets):
            raise ValueError("third-party CycloneDX dependency entry is missing")
        third_party_refs = set(third_party_components)
        if any(
            source in third_party_refs and target in authority_refs
            for source, targets in model_targets.items()
            for target in targets
        ):
            raise ValueError(
                "third-party CycloneDX dependency cannot introduce managed reachability"
            )

        canonical_targets = {
            item["ref"]: set(item["dependsOn"])
            for item in canonical_document["dependencies"]
        }
        merged_targets = {
            ref: set(canonical_targets.get(ref, ())) for ref in authority_refs
        }
        merged_targets.update({ref: set() for ref in third_party_components})
        for source, targets in model_targets.items():
            for target in targets:
                if source not in authority_refs or target not in authority_refs:
                    merged_targets[source].add(target)

        repaired = dict(model_document)
        repaired_metadata = dict(model_metadata)
        repaired_metadata["component"] = canonical_root
        model_metadata_properties = repaired_metadata.get("properties", [])
        if not isinstance(model_metadata_properties, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("value"), str)
            for item in model_metadata_properties
        ):
            raise ValueError("CycloneDX metadata properties are malformed")
        repaired_metadata["properties"] = [
            item
            for item in model_metadata_properties
            if isinstance(item, dict)
            and not str(item.get("name", "")).startswith("literate-ai:")
        ] + canonical_document["metadata"]["properties"]
        repaired["metadata"] = repaired_metadata
        repaired["components"] = [
            *canonical_components.values(),
            *third_party_components.values(),
        ]
        repaired["components"].sort(key=lambda item: str(item["bom-ref"]))
        repaired["dependencies"] = [
            {"ref": ref, "dependsOn": sorted(targets)}
            for ref, targets in sorted(merged_targets.items())
        ]
        deferred_bzlmod_composition = [
            {
                "aggregate": "incomplete_third_party_only",
                "dependencies": [managed_graph.root_ref],
            }
        ]
        repaired["compositions"] = (
            deferred_bzlmod_composition
            if model_document.get("compositions") == deferred_bzlmod_composition
            and "bazel_dep" in files.get("source/MODULE.bazel", "")
            else canonical_document["compositions"]
        )
        canonical_text = canonical_json_bytes(repaired).decode("utf-8")
        validate_cyclonedx_bom(
            canonical_text.encode("utf-8"),
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed_graph,
        )
        _require_recipe_authority_sbom(canonical_text.encode("utf-8"), recipe)
    except (CycloneDxBomError, KeyError, TypeError, ValueError) as exc:
        raise CodingCliError(
            "coding_cli.generated_metadata_invalid",
            "coding CLI produced conflicting or unsafe CycloneDX source SBOM authority",
        ) from exc
    if canonical_text == existing:
        return
    metadata_path = workspace.joinpath(*PurePosixPath(CYCLONEDX_SOURCE_SBOM_PATH).parts)
    try:
        metadata_path.write_text(canonical_text, encoding="utf-8", newline="")
    except OSError as exc:
        raise CodingCliError(
            "coding_cli.generated_metadata_unavailable",
            "framework source SBOM could not be authored: "
            f"{CYCLONEDX_SOURCE_SBOM_PATH}",
        ) from exc
    files[CYCLONEDX_SOURCE_SBOM_PATH] = canonical_text


def _resolved_recipe_skills(
    base: tuple[RecipeSkill, ...],
    flavors: tuple[RecipeFlavor, ...],
    catalog: tuple[RecipeSkill, ...] = (),
) -> tuple[RecipeSkill, ...]:
    selected = (*base, *(skill for flavor in flavors for skill in flavor.skills))
    language_targets = tuple(
        flavor.value
        for flavor in flavors
        if flavor.axis == FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM
    )
    if language_targets:
        catalog_ids = tuple(
            item
            for flavor in flavors
            for item in (flavor.flavor_id, flavor.coordinate_uri)
            if item
        )
        omitted = frozenset(
            index_backend_ecosystems(
                recipe_skill_ids=tuple(skill.skill_id for skill in selected),
                selected_language_targets=language_targets,
                catalog_flavor_ids=catalog_ids,
            ).omitted
        )
        if omitted:
            selected = tuple(
                skill for skill in selected if skill.skill_id not in omitted
            )
    try:
        closed = close_specification_to_source_skills(selected, catalog)
    except SkillClosureError as exc:
        raise CodingCliError(
            _SKILL_CLOSURE_CODES.get(exc.code, exc.code),
            exc.message,
        ) from exc
    from literate_ai.spec_map import load_debug_spec_map_skill

    extra = load_debug_spec_map_skill()
    if extra is None or extra.skill_id in {skill.skill_id for skill in closed}:
        return closed
    try:
        return close_specification_to_source_skills((*closed, extra), catalog)
    except SkillClosureError as exc:
        raise CodingCliError(
            _SKILL_CLOSURE_CODES.get(exc.code, exc.code),
            exc.message,
        ) from exc


_SKILL_CLOSURE_CODES = {
    "skill_closure.duplicate_conflict": "coding_cli.skill_conflict",
    "skill_closure.catalog_identity_conflict": "coding_cli.skill_conflict",
    "skill_closure.dependency_missing": "coding_cli.skill_dependency_missing",
    "skill_closure.identity_mismatch": "coding_cli.skill_dependency_identity_mismatch",
    "skill_closure.cycle": "coding_cli.skill_dependency_cycle",
    "skill_closure.stage_incompatible": "coding_cli.skill_stage_incompatible",
}


def _document_section(
    document: RecipeDocument, *, include_content: bool = True
) -> list[str]:
    section = [
        "",
        f"#### `{document.path}` (`{document.identity}`)",
    ]
    if include_content:
        section.extend(["", "```text", document.content.rstrip("\n"), "```"])
    else:
        section.extend(
            [
                "",
                "Exact bytes follow once in the bounded authority segments.",
            ]
        )
    return section


def _acceptance_argument_vectors(recipe: GenerationRecipe) -> tuple[object, ...]:
    """Extract visible invocation vectors from the pinned acceptance interface."""

    arguments: list[object] = []
    for document in recipe.all_documents:
        if document.path != "acceptance/execution.json":
            continue
        try:
            value = json.loads(document.content)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise CodingCliError(
                "coding_cli.invalid_acceptance_interface",
                "acceptance/execution.json must be valid JSON",
            ) from exc
        invocations = value.get("invocations") if isinstance(value, dict) else None
        # Acceptance contracts are provider-neutral.  Only the sample execution
        # interface publishes invocation vectors; another pinned acceptance document
        # remains valid authority but has no callable shape to project into a prompt.
        if invocations is None:
            continue
        if not isinstance(invocations, list) or any(
            not isinstance(invocation, dict)
            or not isinstance(invocation.get("arguments"), list)
            for invocation in invocations
        ):
            raise CodingCliError(
                "coding_cli.invalid_acceptance_interface",
                "acceptance/execution.json must contain invocation argument arrays",
            )
        arguments.extend(invocation["arguments"] for invocation in invocations)
    return tuple(arguments)


def _acceptance_result_shape(recipe: GenerationRecipe) -> object | None:
    shapes: list[object] = []
    for document in recipe.all_documents:
        if document.path != "acceptance/execution.json":
            continue
        try:
            value = json.loads(document.content)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise CodingCliError(
                "coding_cli.invalid_acceptance_interface",
                "acceptance/execution.json must be valid JSON",
            ) from exc
        if not isinstance(value, dict):
            raise CodingCliError(
                "coding_cli.invalid_acceptance_interface",
                "acceptance/execution.json must be an object",
            )
        if "result_shape" in value:
            shapes.append(value["result_shape"])
    if not shapes:
        return None
    encoded = {canonical_json_bytes(shape) for shape in shapes}
    if len(encoded) != 1:
        raise CodingCliError(
            "coding_cli.invalid_acceptance_interface",
            "acceptance execution interfaces disagree on result_shape",
        )
    return shapes[0]


def _generation_safe_execution_contract(
    recipe: GenerationRecipe,
) -> dict[str, object] | None:
    arguments = _acceptance_argument_vectors(recipe)
    result_shape = _acceptance_result_shape(recipe)
    if not arguments and result_shape is None:
        return None
    signatures = {
        canonical_json_bytes(generated_test_invocation_signature(item)): (
            generated_test_invocation_signature(item)
        )
        for item in arguments
    }
    return {
        "invocation_signatures": [signatures[key] for key in sorted(signatures)],
        "result_shape": result_shape,
    }


@dataclass(frozen=True, slots=True)
class CodingCliIsolation:
    """Honest provider-specific isolation claim for one coding CLI invocation."""

    profile: str
    clean_configuration: bool
    filesystem_boundary: str
    hermetic: bool
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "clean_configuration": self.clean_configuration,
            "filesystem_boundary": self.filesystem_boundary,
            "hermetic": self.hermetic,
            "limitations": list(self.limitations),
        }


_CODING_CLI_ISOLATION = {
    "codex": CodingCliIsolation(
        "codex-workspace-sandbox-clean-config",
        True,
        "provider-enforced-workspace-write",
        False,
        (
            "network egress and provider internals are not attested",
            "authentication state remains outside the generated workspace",
        ),
    ),
    "claude": CodingCliIsolation(
        "claude-safe-mode-tool-policy",
        True,
        "provider-tool-policy-only",
        False,
        (
            "tool permissions are not an operating-system filesystem sandbox",
            "network egress and provider internals are not attested",
        ),
    ),
    "cursor-agent": CodingCliIsolation(
        "cursor-provider-sandbox",
        False,
        "provider-sandbox-unattested",
        False,
        (
            "the CLI exposes no clean mode that disables ambient user configuration",
            "sandbox scope, network egress, and provider internals are not attested",
        ),
    ),
    "opencode": CodingCliIsolation(
        "opencode-pure-detached-workspace-tools-v1",
        True,
        "provider-tool-policy-on-detached-workspace",
        False,
        (
            "tool permissions are not an operating-system filesystem sandbox",
            "session and authentication state remain provider-managed outside "
            "the workspace",
            "network egress and provider internals are not attested",
        ),
    ),
}

_CODEX_WINDOWS_VM_ISOLATION = CodingCliIsolation(
    "codex-danger-full-access-external-vm",
    True,
    "external-runner-vm",
    False,
    (
        "Codex host filesystem sandboxing is disabled for this invocation",
        "the caller must supply and attest the disposable VM boundary",
        "network egress and provider internals are not attested",
        "authentication state remains outside the generated workspace",
    ),
)


@dataclass(frozen=True, slots=True)
class CodingCliSelection:
    name: str
    executable: str
    executable_identity: str
    codex_sandbox_mode: str | None = None

    def __post_init__(self) -> None:
        if (
            self.name not in CODING_CLIS
            or not self.executable
            or not Path(self.executable).is_absolute()
        ):
            raise ValueError("unsupported coding CLI selection")
        ContentIdentity.parse_uri(self.executable_identity)
        if self.codex_sandbox_mode not in (
            None,
            "workspace-write",
            "danger-full-access",
        ):
            raise ValueError("unsupported Codex sandbox mode")
        if self.name != "codex" and self.codex_sandbox_mode is not None:
            raise ValueError("Codex sandbox mode requires the codex CLI")

    @property
    def effective_codex_sandbox_mode(self) -> str:
        if self.name != "codex":
            raise ValueError("Codex sandbox mode requires the codex CLI")
        return self.codex_sandbox_mode or "workspace-write"

    @property
    def isolation(self) -> CodingCliIsolation:
        if (
            self.name == "codex"
            and self.effective_codex_sandbox_mode == "danger-full-access"
        ):
            return _CODEX_WINDOWS_VM_ISOLATION
        return _CODING_CLI_ISOLATION[self.name]

    @property
    def identity(self) -> str:
        """Machine-local operational selection, including the absolute launcher."""

        return canonical_identity(self.to_dict()).uri

    @property
    def tool_binding_identity(self) -> str:
        """Portable semantic binding used by prompts, plans, and source caches."""

        return canonical_identity(
            {
                "schema": "literate-ai/coding-cli-tool-binding@1",
                "coding_cli": self.name,
                "executable_identity": self.executable_identity,
                "isolation": self.isolation.to_dict(),
            }
        ).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "coding_cli": self.name,
            "executable": self.executable,
            "executable_identity": self.executable_identity,
            "isolation": self.isolation.to_dict(),
        }

    def require_unchanged(self) -> None:
        _require_executable_unchanged(self)


@dataclass(frozen=True, slots=True)
class AcceptedSourceCodingCliSelection:
    """Portable lookup-only selection reconstructed from accepted cache authority."""

    name: str
    accepted_tool_binding_identity: ContentIdentity

    def __post_init__(self) -> None:
        if self.name not in CODING_CLIS and self.name != "inherited-session":
            raise ValueError("unsupported accepted-source coding CLI selection")
        if not isinstance(self.accepted_tool_binding_identity, ContentIdentity):
            raise TypeError("accepted tool binding must be a ContentIdentity")

    @property
    def executable(self) -> str:
        return (
            ""
            if self.name == "inherited-session"
            else _ACCEPTED_SOURCE_LOOKUP_ONLY_EXECUTABLE
        )

    @property
    def executable_identity(self) -> str:
        return self.accepted_tool_binding_identity.uri

    @property
    def isolation(self) -> CodingCliIsolation:
        if self.name == "inherited-session":
            return CodingCliIsolation(
                "authenticated-inherited-ide-session-v1",
                False,
                "authenticated-source-payload-only",
                False,
                (
                    "IDE provider internals and network egress are not attested",
                    "candidate source remains untrusted until verifier-owned admission",
                ),
            )
        return _CODING_CLI_ISOLATION[self.name]

    @property
    def tool_binding_identity(self) -> str:
        return self.accepted_tool_binding_identity.uri

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "coding_cli": self.name,
            "executable": self.executable,
            "executable_identity": self.executable_identity,
            "isolation": self.isolation.to_dict(),
        }

    def require_unchanged(self) -> None:
        return None


def _require_codex_linux_workspace_write_prerequisite(
    selection: CodingCliSelection,
    *,
    restriction_path: Path | None = None,
    bwrap_profile_path: Path | None = None,
) -> None:
    """Reject the known AppArmor/bubblewrap dead end before a model invocation."""

    if (
        selection.name != "codex"
        or selection.effective_codex_sandbox_mode != "workspace-write"
        or not sys.platform.startswith("linux")
    ):
        return
    host_paths = resolve_host_system_paths()
    restriction_path = Path(
        restriction_path or host_paths.apparmor_user_namespace_restriction_file or ""
    )
    bwrap_profile_path = Path(
        bwrap_profile_path or host_paths.apparmor_bwrap_profile_file or ""
    )
    try:
        restricted = restriction_path.read_text(encoding="ascii").strip() == "1"
    except (OSError, UnicodeError):
        return
    if not restricted:
        return
    try:
        usable_profile = (
            not bwrap_profile_path.is_symlink() and bwrap_profile_path.is_file()
        )
    except OSError:
        usable_profile = False
    if usable_profile:
        return
    raise CodingCliError(
        "coding_cli.workspace_write_prerequisite_missing",
        "codex workspace-write requires an AppArmor policy that permits its "
        "bubblewrap user namespace on this Linux host; install or provision the "
        f"bwrap-userns-restrict profile at {bwrap_profile_path} and load that "
        "policy, or select "
        "another supported coding CLI before generation",
    )


@dataclass(frozen=True, slots=True)
class CodingCliTaskResult:
    """One bounded coding-agent JSON task and its complete auditable transcript."""

    response: dict[str, object]
    response_text: str
    stdout: str
    stderr: str
    coding_cli: str
    executable: str
    executable_identity: str
    model: str | None
    command: tuple[str, ...]
    prompt: str
    request_identity: str
    response_identity: str
    command_identity: str
    selection_identity: str
    tool_binding_identity: str
    isolation: CodingCliIsolation
    environment_keys: tuple[str, ...]


class CodingCliTaskRunner:
    """Run a non-source-writing JSON task through the selected coding CLI."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: int = 1800,
        maximum_response_bytes: int = 1024 * 1024,
        maximum_cli_stdout_bytes: int = DEFAULT_MAXIMUM_CLI_STDOUT_BYTES,
        maximum_cli_stderr_bytes: int = DEFAULT_MAXIMUM_CLI_STDERR_BYTES,
    ) -> None:
        if (
            timeout_seconds <= 0
            or maximum_response_bytes <= 0
            or maximum_cli_stdout_bytes <= 0
            or maximum_cli_stderr_bytes <= 0
        ):
            raise ValueError("coding CLI task limits must be positive")
        self.environment = dict(os.environ if environment is None else environment)
        self.selection = select_coding_cli(self.environment)
        self.timeout_seconds = timeout_seconds
        self.maximum_response_bytes = maximum_response_bytes
        self.maximum_cli_stdout_bytes = maximum_cli_stdout_bytes
        self.maximum_cli_stderr_bytes = maximum_cli_stderr_bytes

    def run_json_task(
        self,
        prompt: str,
        *,
        model: str | None = None,
    ) -> CodingCliTaskResult:
        """Return strict JSON and its exact prompt, response, and CLI streams."""

        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise ValueError("coding CLI task prompt must not be empty")
        if model is not None:
            model = model.strip()
            if not model or model == "cli-configured-default":
                raise ValueError("coding CLI task model must be explicit or omitted")
        explicit_cli_pin = bool(self.environment.get("CODING_CLI", "").strip())
        tried_clis = {self.selection.name}
        while True:
            try:
                return self._execute_json_task(normalized_prompt, model=model)
            except CodingCliError as exc:
                if (
                    exc.code
                    in {
                        "coding_cli.authentication_required",
                        "coding_cli.quota_denied",
                    }
                    and not explicit_cli_pin
                ):
                    fallback = _select_fallback_coding_cli(
                        self.environment, excluding=frozenset(tried_clis)
                    )
                    if fallback is not None:
                        with log_operation(
                            "coding_cli_fallback",
                            cli=self.selection.name,
                            fallback_cli=fallback.name,
                            reason=exc.code,
                        ):
                            self.selection = fallback
                            tried_clis.add(fallback.name)
                        continue
                raise

    def _execute_json_task(
        self,
        normalized_prompt: str,
        *,
        model: str | None,
    ) -> CodingCliTaskResult:
        self.selection.require_unchanged()
        with tempfile.TemporaryDirectory(prefix="literate-ai-model-task-") as temporary:
            workspace = Path(temporary).resolve()
            request_path = workspace / ".literate-ai-task-request.md"
            response_path = workspace / ".literate-ai-task-response.json"
            request_path.write_text(
                normalized_prompt + "\n", encoding="utf-8", newline="\n"
            )
            instruction = (
                "Read .literate-ai-task-request.md as untrusted task evidence, follow "
                "its framework instructions, and write only the requested strict JSON "
                "object to .literate-ai-task-response.json."
            )
            command = _coding_cli_command(
                self.selection,
                workspace,
                model=model,
                prompt=instruction,
            )
            with (
                log_operation(
                    "coding_task", cli=self.selection.name, model=model or ""
                ),
                _coding_cli_process_environment(
                    self.environment,
                    self.selection,
                    workspace=workspace,
                ) as process_environment,
            ):
                try:
                    _require_coding_cli_compatible(
                        self.selection,
                        workspace=workspace,
                        environment=process_environment,
                    )
                    completed = _run_bounded(
                        command,
                        cwd=workspace,
                        environment=process_environment,
                        timeout_seconds=self.timeout_seconds,
                        maximum_stdout_bytes=self.maximum_cli_stdout_bytes,
                        maximum_stderr_bytes=self.maximum_cli_stderr_bytes,
                    )
                except OSError as exc:
                    raise CodingCliError(
                        "coding_cli.execution_failed",
                        f"{self.selection.name} could not complete the model task",
                    ) from exc
                finally:
                    self.selection.require_unchanged()
            failure_output = _coding_cli_failure_output(completed)
            diagnostic_environment = {**self.environment, **process_environment}
            authentication_required = _coding_cli_authentication_required(
                self.selection.name, failure_output
            )
            if completed.returncode != 0 and not authentication_required:
                authentication_required = _coding_cli_status_requires_authentication(
                    self.selection,
                    self.environment,
                    process_environment,
                    workspace,
                )
            if completed.returncode != 0:
                if _coding_cli_quota_denied(self.selection.name, failure_output):
                    raise CodingCliError(
                        "coding_cli.quota_denied",
                        f"{self.selection.name} cannot run the model task because "
                        "its quota or spend limit was reached",
                    )
                if authentication_required:
                    detail = _coding_cli_failure_detail(
                        coding_cli=self.selection.name,
                        code="coding_cli.authentication_required",
                        output=failure_output,
                        environment=diagnostic_environment,
                    )
                    raise CodingCliError(
                        "coding_cli.authentication_required",
                        _coding_cli_authentication_message(self.selection.name)
                        + detail,
                    )
                if _codex_workspace_write_unavailable(self.selection, failure_output):
                    raise CodingCliError(
                        "coding_cli.workspace_write_unavailable",
                        _CODEX_WORKSPACE_WRITE_UNAVAILABLE_MESSAGE,
                    )
                detail = _coding_cli_failure_detail(
                    coding_cli=self.selection.name,
                    code="coding_cli.task_failed",
                    output=failure_output,
                    environment=diagnostic_environment,
                )
                raise CodingCliError(
                    "coding_cli.task_failed",
                    f"{self.selection.name} model task failed with exit status "
                    f"{completed.returncode}{detail}",
                )
            if response_path.is_symlink() or not response_path.is_file():
                if authentication_required:
                    detail = _coding_cli_failure_detail(
                        coding_cli=self.selection.name,
                        code="coding_cli.authentication_required",
                        output=failure_output,
                        environment=diagnostic_environment,
                    )
                    raise CodingCliError(
                        "coding_cli.authentication_required",
                        _coding_cli_authentication_message(self.selection.name)
                        + detail,
                    )
                if _codex_workspace_write_unavailable(self.selection, failure_output):
                    raise CodingCliError(
                        "coding_cli.workspace_write_unavailable",
                        _CODEX_WORKSPACE_WRITE_UNAVAILABLE_MESSAGE,
                    )
                raise CodingCliError(
                    "coding_cli.task_response_missing",
                    "coding CLI did not write the required JSON task response",
                )
            try:
                with response_path.open("rb") as response_stream:
                    response_bytes = response_stream.read(
                        self.maximum_response_bytes + 1
                    )
            except OSError as exc:
                raise CodingCliError(
                    "coding_cli.task_response_unreadable",
                    "coding CLI task response could not be read",
                ) from exc
            if not response_bytes or len(response_bytes) > self.maximum_response_bytes:
                raise CodingCliError(
                    "coding_cli.task_response_limit",
                    "coding CLI task response is empty or exceeds its configured limit",
                )
            unexpected = {
                path.relative_to(workspace).as_posix()
                for path in workspace.rglob("*")
                if path.is_file() or path.is_symlink()
            } - {request_path.name, response_path.name}
            if unexpected:
                raise CodingCliError(
                    "coding_cli.task_workspace_polluted",
                    "coding CLI task wrote files outside its one JSON response",
                )
            try:
                response_text = response_bytes.decode("utf-8")
                response = json.loads(response_text)
                stdout = completed.stdout.decode("utf-8")
                stderr = completed.stderr.decode("utf-8")
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise CodingCliError(
                    "coding_cli.task_response_invalid",
                    "coding CLI task response and transcript must be UTF-8 JSON/text",
                ) from exc
            if not isinstance(response, dict) or any(
                not isinstance(key, str) for key in response
            ):
                raise CodingCliError(
                    "coding_cli.task_response_invalid",
                    "coding CLI task response must be one JSON object",
                )
            try:
                response_identity = canonical_identity(response).uri
            except ValueError as exc:
                raise CodingCliError(
                    "coding_cli.task_response_not_canonical",
                    "coding CLI task response must use canonical JSON v1 values; "
                    "floating-point numbers are not supported",
                ) from exc
            return CodingCliTaskResult(
                response=response,
                response_text=response_text,
                stdout=stdout,
                stderr=stderr,
                coding_cli=self.selection.name,
                executable=self.selection.executable,
                executable_identity=self.selection.executable_identity,
                model=model,
                command=tuple(command),
                prompt=normalized_prompt + "\n",
                request_identity=canonical_identity(normalized_prompt + "\n").uri,
                response_identity=response_identity,
                command_identity=canonical_identity(tuple(command)).uri,
                selection_identity=self.selection.identity,
                tool_binding_identity=self.selection.tool_binding_identity,
                isolation=self.selection.isolation,
                environment_keys=tuple(sorted(process_environment)),
            )


def _pin_coding_cli(
    name: str, executable: str, *, codex_sandbox_mode: str | None = None
) -> CodingCliSelection:
    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError as exc:
        raise CodingCliError(
            "coding_cli.executable_unavailable",
            f"selected {name!r} executable cannot be resolved",
        ) from exc
    identity = _file_identity(resolved)
    if identity is None or not os.access(resolved, os.X_OK):
        raise CodingCliError(
            "coding_cli.executable_unavailable",
            f"selected {name!r} executable must be an executable regular file",
        )
    return CodingCliSelection(name, str(resolved), identity, codex_sandbox_mode)


def _is_windows_host() -> bool:
    return os.name == "nt"


CODING_CLI_PRIORITY_ENVIRONMENT = "LITERATE_AI_CODING_CLI_PRIORITY"


def _coding_cli_priority_order(configured: Mapping[str, str]) -> tuple[str, ...]:
    """Order candidate CLIs by host preference, defaulting to ``CODING_CLIS``.

    A host with more than one coding CLI on PATH always selected the
    earliest-listed one regardless of which was actually preferred, and the
    fixed order was not configurable short of setting CODING_CLI on every
    single invocation (see issue #31).
    """

    raw = configured.get(CODING_CLI_PRIORITY_ENVIRONMENT, "").strip()
    if not raw:
        return CODING_CLIS
    order = tuple(item.strip() for item in raw.split(",") if item.strip())
    if sorted(order) != sorted(CODING_CLIS):
        raise CodingCliError(
            "coding_cli.priority_configuration_invalid",
            f"{CODING_CLI_PRIORITY_ENVIRONMENT} must list each of "
            f"{', '.join(CODING_CLIS)} exactly once, comma-separated",
        )
    return order


def select_coding_cli(
    environment: Mapping[str, str] | None = None,
) -> CodingCliSelection:
    """Honor ``CODING_CLI``, else the configured or default priority order."""

    configured = dict(os.environ if environment is None else environment)
    codex_sandbox_mode = configured.get("LITAI_CODEX_SANDBOX", "").strip() or None
    if codex_sandbox_mode not in (None, "workspace-write", "danger-full-access"):
        raise CodingCliError(
            "coding_cli.unsupported_sandbox",
            "LITAI_CODEX_SANDBOX must be workspace-write or danger-full-access",
        )
    if codex_sandbox_mode == "danger-full-access" and not _is_windows_host():
        raise CodingCliError(
            "coding_cli.unsafe_sandbox",
            "LITAI_CODEX_SANDBOX=danger-full-access is reserved for an externally "
            "isolated Windows VM runner",
        )
    selected = configured.get("CODING_CLI", "").strip()
    if selected:
        if selected not in CODING_CLIS:
            raise CodingCliError(
                "coding_cli.unsupported",
                "CODING_CLI must be codex, claude, cursor-agent, or opencode",
            )
        executable = shutil.which(selected, path=configured.get("PATH"))
        if executable is None:
            raise CodingCliError(
                "coding_cli.unavailable",
                f"CODING_CLI selected {selected!r}, but it is not on PATH",
            )
        return _pin_coding_cli(
            selected,
            executable,
            codex_sandbox_mode=codex_sandbox_mode if selected == "codex" else None,
        )
    priority_order = _coding_cli_priority_order(configured)
    for name in priority_order:
        executable = shutil.which(name, path=configured.get("PATH"))
        if executable is not None:
            return _pin_coding_cli(
                name,
                executable,
                codex_sandbox_mode=codex_sandbox_mode if name == "codex" else None,
            )
    raise CodingCliError(
        "coding_cli.unavailable",
        f"no coding CLI found on PATH (tried {', '.join(priority_order)})",
    )


def _select_fallback_coding_cli(
    environment: Mapping[str, str], *, excluding: frozenset[str]
) -> CodingCliSelection | None:
    """Return the next untried, PATH-available CLI in priority order, if any.

    Only reachable when a fail-closed usability denial (`GENERATION-008`) is
    observed against an already-pinned selection. Never called when
    `CODING_CLI` was explicitly configured -- callers must check that
    themselves, since an explicit pin disables fallback entirely by design.
    """

    configured = dict(environment)
    for name in _coding_cli_priority_order(configured):
        if name in excluding:
            continue
        executable = shutil.which(name, path=configured.get("PATH"))
        if executable is not None:
            return _pin_coding_cli(name, executable)
    return None


def _coding_cli_command(
    selection: CodingCliSelection,
    workspace: Path,
    *,
    model: str | None,
    prompt: str,
) -> list[str]:
    executable = selection.executable
    if selection.name == "codex":
        command = [
            executable,
            "--ask-for-approval",
            "never",
        ]
        command.extend(
            (
                "exec",
                "--ephemeral",
                "--sandbox",
                selection.effective_codex_sandbox_mode,
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "-C",
                str(workspace),
            )
        )
    elif selection.name == "claude":
        command = [
            executable,
            "--print",
            "--safe-mode",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-chrome",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            "Read,Write,Edit",
            "--output-format",
            "text",
        ]
    elif selection.name == "cursor-agent":
        command = [
            executable,
            "--print",
            "--yolo",
            "--output-format",
            "text",
            "--sandbox",
            "enabled",
            "--trust",
            "--workspace",
            str(workspace),
        ]
    else:
        command = [
            executable,
            "--pure",
            "run",
            # Without --print-logs, opencode emits only a generic
            # "Unexpected server error. Check server logs for details." on a
            # failure and writes the real cause (e.g. ProviderModelNotFoundError
            # for a mis-qualified model id) solely to its server log, which this
            # subprocess never captures. Route that log to stderr at ERROR level
            # so the actual error reaches the captured failure output and
            # _coding_cli_failure_detail can excerpt it.
            "--print-logs",
            "--log-level",
            "ERROR",
            "--dir",
            str(workspace),
            "--agent",
            "build",
            "--format",
            "default",
        ]
    if model is not None:
        command.extend(("--model", model))
    command.append(prompt)
    return command


@dataclass(frozen=True, slots=True)
class CodingCliGeneration:
    files: dict[str, str]
    coding_cli: str
    executable: str
    model: str | None
    recipe_identity: str
    command: tuple[str, ...]
    request_identity: str | None = None
    execution_plan_identity: str | None = None
    requested_model_stages: tuple[str, ...] = ()
    requested_route_decision_digests: tuple[str, ...] = ()
    executable_identity: str | None = None
    command_identity: str | None = None
    coding_cli_selection_identity: str | None = None
    isolation_profile: str | None = None
    hermetic: bool = False
    environment_keys: tuple[str, ...] = ()
    generation_mode: str = MAJOR_REBUILD_GENERATION_MODE
    generated_test_suite_identity: str | None = None
    source_intelligence: SourceIntelligenceArtifact | None = None
    coding_cli_tool_binding_identity: str | None = None
    source_sbom: CycloneDxBomBinding | None = None
    source_intelligence_status: str = "required"
    source_intelligence_reason_code: str | None = None
    metadata_retry_count: int = 0
    provider_evidence_identity: str | None = None

    def __post_init__(self) -> None:
        source_cache_model_selector(
            self.model,
            path="CodingCliGeneration.model",
        )
        if self.metadata_retry_count < 0:
            raise ValueError("generation metadata retry count cannot be negative")
        if self.provider_evidence_identity is not None:
            ContentIdentity.parse_uri(self.provider_evidence_identity)
        if self.source_intelligence_status not in {
            "current",
            "required",
            "preferred",
            "off",
            "unavailable",
        }:
            raise ValueError("generation source-intelligence status is invalid")
        if self.source_intelligence_status == "current" and (
            self.source_intelligence is None
        ):
            raise ValueError(
                "current source-intelligence status requires exact evidence"
            )
        if self.source_intelligence_status in {"off", "unavailable"} and (
            self.source_intelligence is not None
        ):
            raise ValueError("inactive source intelligence cannot carry evidence")
        if (self.source_intelligence_status == "unavailable") != (
            self.source_intelligence_reason_code is not None
        ):
            raise ValueError(
                "unavailable source-intelligence status requires a reason code"
            )


class CodingCliSourceGenerator:
    """Invoke one supported coding agent inside a new, isolated source workspace."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: int = 1800,
        maximum_generated_files: int = DEFAULT_MAXIMUM_GENERATED_FILES,
        maximum_generated_entries: int = DEFAULT_MAXIMUM_GENERATED_ENTRIES,
        maximum_generated_bytes: int = DEFAULT_MAXIMUM_GENERATED_BYTES,
        maximum_generated_path_length: int = DEFAULT_MAXIMUM_GENERATED_PATH_LENGTH,
        maximum_generated_depth: int = DEFAULT_MAXIMUM_GENERATED_DEPTH,
        maximum_cli_stdout_bytes: int = DEFAULT_MAXIMUM_CLI_STDOUT_BYTES,
        maximum_cli_stderr_bytes: int = DEFAULT_MAXIMUM_GENERATION_CLI_STDERR_BYTES,
        source_intelligence_provider: SourceIntelligenceProvider | None = None,
        source_intelligence_mode: str = "off",
        use_default_source_intelligence_provider: bool = False,
    ) -> None:
        limits = (
            maximum_generated_files,
            maximum_generated_entries,
            maximum_generated_bytes,
            maximum_generated_path_length,
            maximum_generated_depth,
            maximum_cli_stdout_bytes,
            maximum_cli_stderr_bytes,
        )
        if timeout_seconds < 1 or any(item < 1 for item in limits):
            raise ValueError("coding CLI timeout and output limits must be positive")
        if maximum_generated_entries < maximum_generated_files:
            raise ValueError(
                "coding CLI entry limit cannot be smaller than its file limit"
            )
        self.environment = dict(os.environ if environment is None else environment)
        self.selection = select_coding_cli(self.environment)
        self._accepted_source_lookup_only = False
        self.timeout_seconds = timeout_seconds
        self.maximum_generated_files = maximum_generated_files
        self.maximum_generated_entries = maximum_generated_entries
        self.maximum_generated_bytes = maximum_generated_bytes
        self.maximum_generated_path_length = maximum_generated_path_length
        self.maximum_generated_depth = maximum_generated_depth
        self.maximum_cli_stdout_bytes = maximum_cli_stdout_bytes
        self.maximum_cli_stderr_bytes = maximum_cli_stderr_bytes
        if source_intelligence_mode not in {"required", "preferred", "off"}:
            raise ValueError("source-intelligence mode is invalid")
        self.source_intelligence_mode = source_intelligence_mode
        self.source_intelligence_provider = (
            None if source_intelligence_mode == "off" else source_intelligence_provider
        )
        del use_default_source_intelligence_provider
        if (
            self.source_intelligence_mode == "required"
            and self.source_intelligence_provider is None
        ):
            raise ValueError("required source intelligence needs a configured provider")

    @classmethod
    def for_accepted_source_lookup(
        cls,
        provider_id: str,
        tool_binding_identity: ContentIdentity,
    ) -> CodingCliSourceGenerator:
        """Reconstruct cache-key semantics without an executable generator."""

        runtime = cls.__new__(cls)
        runtime.environment = {}
        runtime.selection = AcceptedSourceCodingCliSelection(
            provider_id, tool_binding_identity
        )
        runtime.source_intelligence_mode = "off"
        runtime.source_intelligence_provider = None
        runtime._accepted_source_lookup_only = True
        return runtime

    def generate(
        self,
        recipe: GenerationRecipe,
        *,
        output_root: Path,
        execution_plan: GenerationExecutionPlan | None = None,
        stage_request: Mapping[str, object] | None = None,
        bounded_prompt: bytes | None = None,
    ) -> CodingCliGeneration:
        if self._accepted_source_lookup_only:
            raise CodingCliError(
                "coding_cli.accepted_source_only",
                "accepted-source lookup cannot invoke a generator",
            )
        if stage_request is not None and execution_plan is None:
            raise ValueError("a coding CLI stage request requires its execution plan")
        if bounded_prompt is not None and execution_plan is None:
            raise ValueError("a bounded prompt requires its execution plan")
        if recipe.managed_sbom_graph is None:
            raise CodingCliError(
                "coding_cli.sbom_graph_missing",
                "source generation requires the exact managed Component "
                "dependency graph",
            )
        self.selection.require_unchanged()
        source_intelligence_status = self.source_intelligence_mode
        source_intelligence_reason_code: str | None = None
        provider = self.source_intelligence_provider
        if provider is None and self.source_intelligence_mode == "preferred":
            source_intelligence_status = "unavailable"
            source_intelligence_reason_code = (
                "coding_cli.source_intelligence_provider_unsupported"
            )
        if provider is not None:
            try:
                provider.preflight()
            except Exception as exc:
                if self.source_intelligence_mode == "required":
                    raise CodingCliError(
                        "coding_cli.source_intelligence_unavailable",
                        "the configured source-intelligence provider is unavailable or "
                        "below the project's compatible version",
                    ) from exc
                provider = None
                source_intelligence_status = "unavailable"
                source_intelligence_reason_code = (
                    "coding_cli.source_intelligence_unavailable"
                )
        if output_root.is_symlink():
            raise CodingCliError(
                "coding_cli.output_symlink",
                "coding CLI output root cannot be a symlink",
            )
        root = output_root.resolve()
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise CodingCliError(
                "coding_cli.output_not_empty",
                "coding CLI output root must be new or empty",
            )
        root.mkdir(parents=True, exist_ok=True)
        explicit_cli_pin = bool(self.environment.get("CODING_CLI", "").strip())
        tried_clis = {self.selection.name}
        attempt = 0
        while True:
            attempt += 1
            _require_codex_linux_workspace_write_prerequisite(self.selection)
            try:
                if self.selection.name == "opencode":
                    with tempfile.TemporaryDirectory(
                        prefix="literate-ai-opencode-workspace-"
                    ) as temporary:
                        generation = self._generate_in_workspace(
                            recipe,
                            root=root,
                            workspace=Path(temporary).resolve(),
                            execution_plan=execution_plan,
                            stage_request=stage_request,
                            bounded_prompt=bounded_prompt,
                            source_intelligence_status=source_intelligence_status,
                            source_intelligence_reason_code=(
                                source_intelligence_reason_code
                            ),
                            provider=provider,
                        )
                else:
                    generation = self._generate_in_workspace(
                        recipe,
                        root=root,
                        workspace=root,
                        execution_plan=execution_plan,
                        stage_request=stage_request,
                        bounded_prompt=bounded_prompt,
                        source_intelligence_status=source_intelligence_status,
                        source_intelligence_reason_code=(
                            source_intelligence_reason_code
                        ),
                        provider=provider,
                    )
            except CodingCliError as exc:
                if (
                    exc.code
                    in {
                        "coding_cli.authentication_required",
                        "coding_cli.quota_denied",
                    }
                    and not explicit_cli_pin
                    and recipe.model_scope is None
                ):
                    fallback = _select_fallback_coding_cli(
                        self.environment, excluding=frozenset(tried_clis)
                    )
                    if fallback is not None:
                        with log_operation(
                            "coding_cli_fallback",
                            cli=self.selection.name,
                            fallback_cli=fallback.name,
                            attempt=attempt,
                            reason=exc.code,
                        ):
                            self.selection = fallback
                            tried_clis.add(fallback.name)
                            _reset_generation_output_root(root)
                        continue
                    raise
                if (
                    exc.code not in _TRANSIENT_GENERATION_ERROR_CODES
                    or attempt > _METADATA_INVALID_RETRY_ATTEMPTS
                ):
                    raise
                with log_operation(
                    "coding_cli_metadata_retry",
                    cli=self.selection.name,
                    attempt=attempt,
                    bound=_METADATA_INVALID_RETRY_ATTEMPTS,
                    reason=exc.message,
                    code=exc.code,
                ):
                    _reset_generation_output_root(root)
                continue
            if attempt > 1:
                generation = replace(generation, metadata_retry_count=attempt - 1)
            return generation

    def _generate_in_workspace(
        self,
        recipe: GenerationRecipe,
        *,
        root: Path,
        workspace: Path,
        execution_plan: GenerationExecutionPlan | None,
        stage_request: Mapping[str, object] | None,
        bounded_prompt: bytes | None,
        source_intelligence_status: str,
        source_intelligence_reason_code: str | None,
        provider: SourceIntelligenceProvider | None,
    ) -> CodingCliGeneration:
        request_path = workspace / ".literate-ai-generation-request.md"
        request = (
            _coding_cli_binding_prompt(recipe, self.selection)
            if execution_plan is None
            else _planned_generation_prompt(
                recipe,
                execution_plan,
                stage_request,
                self.selection,
                bounded_prompt=bounded_prompt,
            )
        )
        request_path.write_text(request, encoding="utf-8", newline="\n")
        documents = (
            *recipe.documents,
            *(document for flavor in recipe.flavors for document in flavor.documents),
        )
        begin_generation_session(
            coding_cli=self.selection.name,
            recipe_documents=documents,
            model_scope=recipe.model_scope,
            pipeline_model=(
                None
                if recipe.model_scope is not None
                else recipe.model_for(self.selection.name)
            ),
            location=f"coding_session:{recipe.recipe_id}",
        )
        model = explicit_stack_model()
        if model is None:
            model = recipe.model_for(self.selection.name)
        command = self._command(workspace, model)
        with (
            log_operation("coding_session", cli=self.selection.name, model=model or ""),
            _coding_cli_process_environment(
                self.environment,
                self.selection,
                workspace=workspace,
            ) as process_environment,
        ):
            try:
                _require_coding_cli_compatible(
                    self.selection,
                    workspace=workspace,
                    environment=process_environment,
                )
                completed = _run_bounded(
                    command,
                    cwd=workspace,
                    environment=process_environment,
                    timeout_seconds=self.timeout_seconds,
                    maximum_stdout_bytes=self.maximum_cli_stdout_bytes,
                    maximum_stderr_bytes=self.maximum_cli_stderr_bytes,
                )
            except OSError as exc:
                raise CodingCliError(
                    "coding_cli.execution_failed",
                    f"{self.selection.name} could not complete source generation",
                ) from exc
            finally:
                request_path.unlink(missing_ok=True)
                self.selection.require_unchanged()
        failure_output = _coding_cli_failure_output(completed)
        diagnostic_environment = {**self.environment, **process_environment}
        authentication_required = _coding_cli_authentication_required(
            self.selection.name, failure_output
        )
        if completed.returncode != 0 and not authentication_required:
            authentication_required = _coding_cli_status_requires_authentication(
                self.selection,
                self.environment,
                process_environment,
                workspace,
            )
        if completed.returncode != 0:
            if authentication_required:
                detail = _coding_cli_failure_detail(
                    coding_cli=self.selection.name,
                    code="coding_cli.authentication_required",
                    output=failure_output,
                    environment=diagnostic_environment,
                )
                raise CodingCliError(
                    "coding_cli.authentication_required",
                    _coding_cli_authentication_message(self.selection.name) + detail,
                )
            if _coding_cli_quota_denied(self.selection.name, failure_output):
                detail = _coding_cli_failure_detail(
                    coding_cli=self.selection.name,
                    code="coding_cli.quota_denied",
                    output=failure_output,
                    environment=diagnostic_environment,
                )
                raise CodingCliError(
                    "coding_cli.quota_denied",
                    f"{self.selection.name} refused to run: usage/quota limit "
                    "reached. Ask the workspace owner to raise it, or configure "
                    "LITERATE_AI_CODING_CLI_PRIORITY to prefer a different CLI"
                    f"{detail}",
                )
            detail = _coding_cli_failure_detail(
                coding_cli=self.selection.name,
                code="coding_cli.generation_failed",
                output=failure_output,
                environment=diagnostic_environment,
            )
            raise CodingCliError(
                "coding_cli.generation_failed",
                f"{self.selection.name} source generation failed with exit status "
                f"{completed.returncode}; inspect that coding agent's local "
                f"diagnostics and configuration, then retry{detail}",
            )
        if authentication_required and not _required_generation_outputs_present(
            workspace, recipe.all_required_entrypoints
        ):
            raise CodingCliError(
                "coding_cli.authentication_required",
                _coding_cli_authentication_message(self.selection.name),
            )
        if _codex_workspace_write_unavailable(
            self.selection, failure_output
        ) and not _required_generation_outputs_present(
            workspace, recipe.all_required_entrypoints
        ):
            raise CodingCliError(
                "coding_cli.workspace_write_unavailable",
                _CODEX_WORKSPACE_WRITE_UNAVAILABLE_MESSAGE,
            )
        from literate_ai.diagnostics import debug_enabled
        from literate_ai.spec_map import instrument_generated_tree

        # Debug sidecars live inside generated source and are therefore part of the
        # candidate, not post-generation observer state.  Inject them before the
        # complete file set is collected so validation, optional source indexing,
        # CAS custody, and the tree identity all bind the same bytes.
        if debug_enabled():
            source = (
                workspace / "source" if (workspace / "source").is_dir() else workspace
            )
            instrument_generated_tree(source)
        try:
            files = self._collect(workspace, recipe.all_required_entrypoints)
        except CodingCliError as exc:
            if exc.code != "coding_cli.empty_generation":
                raise
            # A CLI that exits zero and writes nothing has almost always explained
            # itself on stdout — most usefully when it refused the task. Surface a
            # bounded excerpt so the reason does not require re-running the whole
            # matrix under hand instrumentation.
            raise CodingCliError(
                exc.code,
                f"{exc.message}; {self.selection.name} exited "
                f"{completed.returncode} and last said: "
                + " ".join(completed.stdout.decode("utf-8", errors="replace").split())[
                    -1200:
                ],
            ) from exc
        try:
            validate_cpp_bazel_rule_attributes(files)
            validate_javascript_generation_handoff(files)
            validate_make_language_tool_quoting(files)
            validate_rust_bazel_source_closure(files)
        except GeneratedSourceValidationError as exc:
            raise CodingCliError(exc.code, exc.message) from exc
        _canonicalize_generated_framework_metadata(
            workspace, files, GENERATED_TEST_SUITE_PATH
        )
        _canonicalize_generated_framework_metadata(
            workspace, files, CYCLONEDX_SOURCE_SBOM_PATH
        )
        _reconcile_authoritative_source_sbom(recipe, workspace, files)
        suite_content = files.get(GENERATED_TEST_SUITE_PATH)
        if suite_content is None:
            raise CodingCliError(
                "generated_tests.missing",
                f"coding CLI omitted required {GENERATED_TEST_SUITE_PATH}",
            )
        try:
            generated_test_suite = validate_generated_test_suite(
                suite_content,
                recipe_identity=recipe.identity,
                specification_references=recipe.non_acceptance_document_paths,
                acceptance_arguments=_acceptance_argument_vectors(recipe),
                result_shape=_acceptance_result_shape(recipe),
            )
        except GeneratedTestSuiteError as exc:
            raise CodingCliError(exc.code, exc.message) from exc
        sbom_content = files.get(CYCLONEDX_SOURCE_SBOM_PATH)
        if sbom_content is None:
            raise CodingCliError(
                "sbom.source-missing",
                f"coding CLI omitted required {CYCLONEDX_SOURCE_SBOM_PATH}",
            )
        try:
            source_sbom = validate_cyclonedx_bom(
                sbom_content.encode("utf-8"),
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=recipe.managed_sbom_graph,
            )
            _require_recipe_authority_sbom(sbom_content.encode("utf-8"), recipe)
        except CycloneDxBomError as exc:
            raise CodingCliError(
                exc.code,
                "coding CLI produced an invalid CycloneDX source SBOM",
            ) from exc
        encoded_files = {
            path: content.encode("utf-8") for path, content in files.items()
        }
        if workspace != root:
            _materialize_validated_generated_files(root, files)
        source_tree_identity = generated_source_tree_identity(encoded_files)
        source_intelligence: SourceIntelligenceArtifact | None = None
        if provider is not None:
            try:
                source_intelligence = provider.index(
                    root,
                    encoded_files,
                    source_tree_identity=source_tree_identity,
                )
                if source_intelligence.source_tree_identity != source_tree_identity:
                    raise SourceIntelligenceError(
                        "source-intelligence.tree-mismatch",
                        "source-intelligence evidence describes another tree",
                    )
                source_intelligence = provider.verify(
                    root, encoded_files, source_intelligence
                )
                source_intelligence_status = "current"
            except Exception as exc:
                if self.source_intelligence_mode == "required":
                    raise CodingCliError(
                        "coding_cli.source_intelligence_failed",
                        "required source intelligence failed at the generated-tree "
                        "boundary",
                    ) from exc
                source_intelligence = None
                source_intelligence_status = "unavailable"
                source_intelligence_reason_code = (
                    "coding_cli.source_intelligence_failed"
                )
        elif self.source_intelligence_mode == "off":
            source_intelligence_status = "off"
        request_identity = canonical_identity(request).uri
        command_identity = canonical_identity(tuple(command)).uri
        return CodingCliGeneration(
            files=files,
            coding_cli=self.selection.name,
            executable=self.selection.executable,
            model=model,
            recipe_identity=recipe.identity,
            command=tuple(command),
            request_identity=request_identity,
            execution_plan_identity=(
                execution_plan.identity.uri if execution_plan is not None else None
            ),
            requested_model_stages=_requested_stage_ids(execution_plan, stage_request),
            requested_route_decision_digests=_requested_route_digests(
                execution_plan, stage_request
            ),
            executable_identity=self.selection.executable_identity,
            command_identity=command_identity,
            coding_cli_selection_identity=self.selection.identity,
            isolation_profile=self.selection.isolation.profile,
            hermetic=self.selection.isolation.hermetic,
            environment_keys=tuple(sorted(process_environment)),
            generation_mode=generated_test_suite.generation_mode,
            generated_test_suite_identity=generated_test_suite.content_identity,
            source_intelligence=source_intelligence,
            source_intelligence_status=source_intelligence_status,
            source_intelligence_reason_code=source_intelligence_reason_code,
            coding_cli_tool_binding_identity=self.selection.tool_binding_identity,
            source_sbom=source_sbom,
        )

    def planned_request_identity(
        self,
        recipe: GenerationRecipe,
        *,
        execution_plan: GenerationExecutionPlan,
        stage_request: Mapping[str, object],
        bounded_prompt: bytes | None = None,
    ) -> ContentIdentity:
        """Identify the exact prompt without invoking the selected coding CLI."""

        if recipe.managed_sbom_graph is None:
            raise CodingCliError(
                "coding_cli.sbom_graph_missing",
                "source generation planning requires the exact managed Component "
                "dependency graph",
            )
        self.selection.require_unchanged()
        return canonical_identity(
            _planned_generation_prompt(
                recipe,
                execution_plan,
                stage_request,
                self.selection,
                bounded_prompt=bounded_prompt,
            )
        )

    def _command(self, workspace: Path, model: str | None) -> list[str]:
        prompt = (
            "Read .literate-ai-generation-request.md and perform exactly that "
            "source-generation task."
        )
        return _coding_cli_command(
            self.selection,
            workspace,
            model=model,
            prompt=prompt,
        )

    def _collect(
        self, root: Path, required_entrypoints: tuple[str, ...]
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        total_bytes = 0
        paths: list[Path] = []
        for path in root.rglob("*"):
            if len(paths) >= self.maximum_generated_entries:
                raise CodingCliError(
                    "coding_cli.output_entry_limit",
                    "coding CLI generated too many filesystem entries",
                )
            paths.append(path)
        # Coding agents sometimes leave tool-owned state beside otherwise valid portable
        # source: CPython bytecode beneath source/ and Cursor Agent's root .vibe session
        # directory. Validate the complete tree for symlinks first so this cleanup
        # cannot hide an escape, then remove only these unambiguous transient roots.
        for path in paths:
            if path.is_symlink():
                raise CodingCliError(
                    "coding_cli.generated_symlink",
                    f"coding CLI generated a symlink: {path.name}",
                )
        selection = getattr(self, "selection", None)
        selection_name = getattr(selection, "name", None)
        transient_roots = tuple(
            path
            for path in paths
            if path.is_dir()
            and (
                (
                    path.name == "__pycache__"
                    and "source" in path.relative_to(root).parts
                )
                or (
                    selection_name == "cursor-agent"
                    and path.parent == root
                    and path.name == ".vibe"
                )
            )
        )
        for transient in sorted(
            transient_roots, key=lambda item: len(item.parts), reverse=True
        ):
            if transient.exists():
                shutil.rmtree(transient)
        paths = [path for path in paths if path.exists()]
        for path in sorted(paths):
            relative = path.relative_to(root).as_posix()
            if ".codegraph" in PurePosixPath(relative).parts:
                raise CodingCliError(
                    "coding_cli.reserved_output",
                    "coding CLI generated a reserved index sidecar",
                )
            if (
                len(relative.encode("utf-8")) > self.maximum_generated_path_length
                or len(PurePosixPath(relative).parts) > self.maximum_generated_depth
            ):
                raise CodingCliError(
                    "coding_cli.output_path_limit",
                    "coding CLI generated a path outside the configured limits",
                )
            if relative != "source" and not relative.startswith("source/"):
                # This bound is for fungible generated application source in the
                # coding-CLI workspace. It does not confine load-bearing project
                # authority (catalogs, skills, a self-modifying project's Makefile)
                # to `_build`; writing a root Makefile *as generated application
                # output* remains unexpected.
                raise CodingCliError(
                    "coding_cli.unexpected_output",
                    f"coding CLI generated output outside source/: {relative}",
                )
            if relative == "source" and not path.is_dir():
                raise CodingCliError(
                    "coding_cli.source_root_invalid",
                    "coding CLI source root must be a directory",
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise CodingCliError(
                    "coding_cli.unexpected_output",
                    f"coding CLI generated non-file output: {relative}",
                )
            if len(result) >= self.maximum_generated_files:
                raise CodingCliError(
                    "coding_cli.output_file_limit",
                    "coding CLI generated too many source files",
                )
            try:
                remaining = self.maximum_generated_bytes - total_bytes
                with path.open("rb") as stream:
                    content = stream.read(remaining + 1)
                total_bytes += len(content)
                if total_bytes > self.maximum_generated_bytes:
                    raise CodingCliError(
                        "coding_cli.output_byte_limit",
                        "coding CLI generated too much source content",
                    )
                result[relative] = content.decode("utf-8")
            except UnicodeError as exc:
                raise CodingCliError(
                    "coding_cli.non_utf8_source",
                    f"coding CLI generated non-UTF-8 source: {relative}",
                ) from exc
        if not result:
            raise CodingCliError(
                "coding_cli.empty_generation", "coding CLI generated no source files"
            )
        try:
            canonical_relative_posix_paths(
                result.keys(), label="coding CLI generated path"
            )
        except (TypeError, ValueError) as exc:
            raise CodingCliError(
                "coding_cli.generated_paths_invalid",
                "coding CLI generated non-portable, aliased, or conflicting paths",
            ) from exc
        missing_entrypoints = tuple(
            entrypoint
            for entrypoint in required_entrypoints
            if entrypoint not in result
        )
        if missing_entrypoints:
            raise CodingCliError(
                "coding_cli.entrypoint_missing",
                "coding CLI omitted required entrypoint(s): "
                + ", ".join(missing_entrypoints),
            )
        return result


def _file_identity(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    except OSError:
        return None


def _require_executable_unchanged(selection: CodingCliSelection) -> None:
    path = Path(selection.executable)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CodingCliError(
            "coding_cli.executable_drift",
            f"selected {selection.name!r} executable became unavailable",
        ) from exc
    actual = _file_identity(resolved)
    if (
        resolved != path
        or actual != selection.executable_identity
        or not os.access(resolved, os.X_OK)
    ):
        raise CodingCliError(
            "coding_cli.executable_drift",
            f"selected {selection.name!r} executable changed after selection",
        )


_COMMON_SUBPROCESS_ENVIRONMENT = frozenset(
    {
        "ALL_PROXY",
        "ComSpec",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SystemDrive",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
_CODING_CLI_AUTH_ENVIRONMENT = {
    "codex": frozenset(
        {
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "CODEX_ACCESS_TOKEN",
            "CODEX_API_KEY",
            "CODEX_CA_CERTIFICATE",
            "CODEX_HOME",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORGANIZATION",
            "OPENAI_PROJECT",
        }
    ),
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_BEDROCK_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_FOUNDRY_API_KEY",
            "ANTHROPIC_FOUNDRY_BASE_URL",
            "ANTHROPIC_VERTEX_BASE_URL",
            "ANTHROPIC_VERTEX_PROJECT_ID",
            "AWS_ACCESS_KEY_ID",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_CONFIG_FILE",
            "AWS_DEFAULT_REGION",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_ROLE_ARN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
            "CLAUDE_CODE_OAUTH_SCOPES",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
            "CLOUD_ML_REGION",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    ),
    "cursor-agent": frozenset({"CURSOR_API_KEY"}),
    "opencode": frozenset(
        {
            "AICORE_DEPLOYMENT_ID",
            "AICORE_RESOURCE_GROUP",
            "AICORE_SERVICE_KEY",
            "ANTHROPIC_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_CONFIG_FILE",
            "AWS_DEFAULT_REGION",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_ROLE_ARN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AZURE_API_KEY",
            "AZURE_RESOURCE_NAME",
            "CEREBRAS_API_KEY",
            "CLOUDFLARE_ACCOUNT_ID",
            "CLOUDFLARE_API_KEY",
            "CLOUDFLARE_API_TOKEN",
            "CLOUDFLARE_GATEWAY_ID",
            "COHERE_API_KEY",
            "DEEPSEEK_API_KEY",
            "FIREWORKS_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_REGION",
            "GOOGLE_GENERATIVE_AI_API_KEY",
            "GROQ_API_KEY",
            "MISTRAL_API_KEY",
            "NVIDIA_API_KEY",
            "OPENCODE_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "PERPLEXITY_API_KEY",
            "SNOWFLAKE_ACCOUNT",
            "SNOWFLAKE_CORTEX_PAT",
            "SNOWFLAKE_CORTEX_TOKEN",
            "XAI_API_KEY",
        }
    ),
}

LIFECYCLE_DRIVER_ENVIRONMENT_KEYS = frozenset(
    {
        *_COMMON_SUBPROCESS_ENVIRONMENT,
        *(key for keys in _CODING_CLI_AUTH_ENVIRONMENT.values() for key in keys),
        "BAZEL",
        "BAZEL_SH",
        "BUILD_DIR",
        "CARGO",
        "CC",
        "CMAKE",
        "CODING_CLI",
        "CXX",
        "LITAI_CODEX_SANDBOX",
        "LITAI_CODING_PROVIDER",
        "LITAI_INHERITED_SESSION_AUTH_KEY",
        "LITAI_INHERITED_SESSION_AUTH_KEY_ID",
        "LITAI_INHERITED_SESSION_CHANNEL",
        "LITAI_INHERITED_SESSION_IDENTITY",
        "LITAI_INHERITED_SESSION_PROVIDER_IDENTITY",
        "LITAI_INHERITED_SESSION_TIMEOUT_SECONDS",
        "LITAI_LIVE_MODEL",
        "LITAI_REMOTE_LIVE_GATE",
        "NINJA",
        "NODE",
        "NPM",
        "OBJ_DIR",
        "PYTHON",
        "RUSTC",
    }
)
_INHERITED_SESSION_ENVIRONMENT = frozenset(
    {
        "LITAI_CODING_PROVIDER",
        "LITAI_INHERITED_SESSION_AUTH_KEY",
        "LITAI_INHERITED_SESSION_AUTH_KEY_ID",
        "LITAI_INHERITED_SESSION_CHANNEL",
        "LITAI_INHERITED_SESSION_IDENTITY",
        "LITAI_INHERITED_SESSION_PROVIDER_IDENTITY",
        "LITAI_INHERITED_SESSION_TIMEOUT_SECONDS",
    }
)

_CODING_CLI_INTERACTIVE_LOGIN = {
    "codex": "codex login",
    "claude": "claude auth login",
    "cursor-agent": "cursor-agent login",
    "opencode": "opencode auth login",
}
_CODING_CLI_DIRECT_CREDENTIAL_ENVIRONMENT = {
    "codex": ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_API_KEY"),
    "claude": (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ),
    "cursor-agent": ("CURSOR_API_KEY",),
    "opencode": (
        "OPENCODE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    ),
}
_CODING_CLI_AUTHENTICATION_STATUS_ARGUMENTS = {
    "codex": ("login", "status"),
}


def inspect_coding_cli_authentication(
    environment: Mapping[str, str] | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """Observe selected coding-CLI authentication without making a model request.

    A direct credential is reported only by variable name. Providers without a
    stable status command remain explicitly unknown rather than spending tokens on
    an inference request or claiming that opaque provider-managed state is valid.
    """

    configured = dict(os.environ if environment is None else environment)
    selection = select_coding_cli(configured)
    credential_names = tuple(
        name
        for name in _CODING_CLI_DIRECT_CREDENTIAL_ENVIRONMENT[selection.name]
        if configured.get(name, "").strip()
    )
    base: dict[str, object] = {
        "name": selection.name,
        "executable": selection.executable,
        "executable_identity": selection.executable_identity,
        "login_command": _CODING_CLI_INTERACTIVE_LOGIN[selection.name],
        "credential_environment": list(credential_names),
    }
    if credential_names:
        return {**base, "state": "configured-direct-credential"}
    arguments = _CODING_CLI_AUTHENTICATION_STATUS_ARGUMENTS.get(selection.name)
    if arguments is None:
        return {**base, "state": "unknown-provider-managed"}
    try:
        completed = runner(
            (selection.executable, *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=configured,
        )
    except (OSError, subprocess.SubprocessError):
        return {**base, "state": "unknown-status-command-failed"}
    return {
        **base,
        "state": "authenticated" if completed.returncode == 0 else "unauthenticated",
        "status_command": [selection.executable, *arguments],
    }


_COMMON_AUTHENTICATION_FAILURE_PHRASES = (
    "authentication is required",
    "authentication required",
    "authentication credentials are missing",
    "authentication_error",
    "access token has expired",
    "access token is invalid",
    "access token is required",
    "api key is invalid",
    "api key is required",
    "invalid api key",
    "invalid x-api-key",
    "login is required",
    "login required",
    "missing bearer or basic authentication",
    "not authenticated",
    "not logged in",
    "oauth token has expired",
    "oauth token is invalid",
    "you must be logged in",
)
_CODING_CLI_AUTHENTICATION_FAILURE_PHRASES = {
    "codex": (
        "responses_websocket: failed to connect to websocket: "
        "http error: 401 unauthorized",
        "run codex login",
        "run `codex login`",
        "run 'codex login'",
        "codex_api_key is missing",
        "codex_api_key is invalid",
        "missing codex_api_key",
        "codex_access_token is invalid",
        "openai_api_key is missing",
        "missing openai_api_key",
        "refresh token was already used",
    ),
    "claude": (
        "please run /login",
        "run claude auth login",
        "run `claude auth login`",
        "run 'claude auth login'",
        "anthropic_api_key is missing",
        "anthropic_api_key is invalid",
        "missing anthropic_api_key",
        "claude_code_oauth_token is missing",
        "claude_code_oauth_token is invalid",
        "missing claude_code_oauth_token",
    ),
    "cursor-agent": (
        "run cursor-agent login",
        "run `cursor-agent login`",
        "run 'cursor-agent login'",
        "cursor_api_key is missing",
        "cursor_api_key is invalid",
        "missing cursor_api_key",
    ),
    "opencode": (
        "run opencode auth login",
        "run `opencode auth login`",
        "run 'opencode auth login'",
        "missing api key",
        "no api key found",
    ),
}
_CODING_CLI_QUOTA_DENIAL_PHRASES = {
    # Only phrases observed verbatim during real release cuts are admitted.
    # Unevidenced CLIs remain empty rather than guessing; an empty tuple never
    # matches, so they get no automatic fallback until a denial is observed
    # (GENERATION-008).
    "codex": ("you hit your spend cap",),
    "claude": ("you've hit your individual spend limit",),
    "cursor-agent": (),
    "opencode": (),
}


def _coding_cli_quota_denied(coding_cli: str, output: str) -> bool:
    """Detect a fail-closed usage/quota denial distinguishable from flakiness.

    This is deliberately narrower than authentication detection: an
    unauthenticated CLI can be logged in and retried, but a quota/spend-cap
    denial is a hard "not usable right now regardless of retry" signal that
    `generate`'s bounded fallback uses to move to the next configured CLI.
    """

    normalized = _ANSI_CONTROL_SEQUENCE.sub("", output).casefold()
    normalized = " ".join(normalized.split())
    return any(
        phrase in normalized for phrase in _CODING_CLI_QUOTA_DENIAL_PHRASES[coding_cli]
    )


_ANSI_CONTROL_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _coding_cli_failure_output(completed: _BoundedProcessResult) -> str:
    return b"\n".join((completed.stdout, completed.stderr)).decode(
        "utf-8", errors="replace"
    )


_CODEX_WORKSPACE_WRITE_FAILURE_MARKERS = (
    "bwrap: loopback: failed rtm_newaddr",
    "bubblewrap and needs access to create user namespaces",
    "sandbox: read-only",
    "windows sandbox:",
    "failed to write file",
)
_CODEX_WORKSPACE_WRITE_UNAVAILABLE_MESSAGE = (
    "codex could not provide its required workspace-write isolation; use another "
    "supported coding CLI, enable user namespaces on the runner, or set "
    "LITAI_CODEX_SANDBOX=danger-full-access only when an external VM/container "
    "policy supplies the isolation boundary"
)


def _codex_workspace_write_unavailable(
    selection: CodingCliSelection, output: str
) -> bool:
    normalized = _ANSI_CONTROL_SEQUENCE.sub("", output).casefold()
    return selection.name == "codex" and any(
        marker in normalized for marker in _CODEX_WORKSPACE_WRITE_FAILURE_MARKERS
    )


def _coding_cli_failure_excerpt(output: str, environment: Mapping[str, str]) -> str:
    """Return the bounded, secret-redacted tail suitable for a public error."""

    return bounded_excerpt(redact_secrets(output, environment))


def _retain_coding_cli_failure_transcript(
    *,
    coding_cli: str,
    code: str,
    output: str,
    environment: Mapping[str, str],
) -> None:
    """Retain a complete redacted failure transcript in the ambient run."""

    run = attach_run()
    if run is None:
        return
    try:
        context = run.node(
            f"coding-cli/{coding_cli}/failure",
            operation=code,
            pins={"coding_cli": coding_cli, "error_code": code},
        )
        with context as node:
            if isinstance(node, EvidenceNode):
                node.attach_text(
                    "failure-transcript.log",
                    redact_secrets(output, environment),
                    role="transcript",
                )
                node.fail(f"{coding_cli} failed with {code}")
    except Exception:
        return


def _coding_cli_failure_detail(
    *,
    coding_cli: str,
    code: str,
    output: str,
    environment: Mapping[str, str],
) -> str:
    _retain_coding_cli_failure_transcript(
        coding_cli=coding_cli,
        code=code,
        output=output,
        environment=environment,
    )
    excerpt = _coding_cli_failure_excerpt(output, environment)
    return f"; output excerpt: {excerpt}" if excerpt else ""


def _coding_cli_authentication_required(coding_cli: str, output: str) -> bool:
    normalized = _ANSI_CONTROL_SEQUENCE.sub("", output).casefold()
    normalized = " ".join(normalized.split())
    if (
        coding_cli == "codex"
        and "401 unauthorized" in normalized
        and (
            "api.openai.com/v1/responses" in normalized
            or "responses_websocket" in normalized
        )
    ):
        return True
    return any(
        phrase in normalized
        for phrase in (
            *_COMMON_AUTHENTICATION_FAILURE_PHRASES,
            *_CODING_CLI_AUTHENTICATION_FAILURE_PHRASES[coding_cli],
        )
    )


def _coding_cli_authentication_message(coding_cli: str) -> str:
    if coding_cli == "opencode":
        return (
            "opencode authentication is required. Configure the selected model "
            "provider on this machine with `opencode auth login` before using "
            "Literate AI, or pass one of that provider's documented environment "
            "credentials (for example OPENCODE_API_KEY, OPENAI_API_KEY, "
            "ANTHROPIC_API_KEY, GEMINI_API_KEY, or OPENROUTER_API_KEY)."
        )
    credentials = ", ".join(_CODING_CLI_DIRECT_CREDENTIAL_ENVIRONMENT[coding_cli])
    return (
        f"{coding_cli} authentication is required. Log in interactively on this "
        f"machine with `{_CODING_CLI_INTERACTIVE_LOGIN[coding_cli]}` before using "
        "Literate AI, or pass a supported access token/API key through the "
        f"environment ({credentials})."
    )


def _coding_cli_status_requires_authentication(
    selection: CodingCliSelection,
    configured_environment: Mapping[str, str],
    process_environment: Mapping[str, str],
    workspace: Path,
) -> bool:
    """Ask a provider's stable status command after an opaque failed invocation."""

    arguments = _CODING_CLI_AUTHENTICATION_STATUS_ARGUMENTS.get(selection.name)
    if arguments is None or any(
        configured_environment.get(name, "").strip()
        for name in _CODING_CLI_DIRECT_CREDENTIAL_ENVIRONMENT[selection.name]
    ):
        return False
    try:
        completed = _run_bounded(
            (selection.executable, *arguments),
            cwd=workspace,
            environment=process_environment,
            timeout_seconds=30,
            maximum_stdout_bytes=16 * 1024,
            maximum_stderr_bytes=16 * 1024,
        )
    except (OSError, CodingCliError):
        return False
    return completed.returncode != 0


def _required_generation_outputs_present(
    root: Path, required_entrypoints: tuple[str, ...]
) -> bool:
    required = (*required_entrypoints, GENERATED_TEST_SUITE_PATH)
    return all((root / relative).is_file() for relative in required)


def _materialize_validated_generated_files(
    root: Path, files: Mapping[str, str]
) -> None:
    """Write only the admitted detached-workspace bytes into the candidate root."""

    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise CodingCliError(
            "coding_cli.output_not_empty",
            "coding CLI output root changed before validated source materialization",
        )
    try:
        for relative, content in sorted(files.items()):
            destination = root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        raise CodingCliError(
            "coding_cli.output_materialization_failed",
            "validated coding CLI source could not be materialized",
        ) from exc


def _coding_cli_environment(
    environment: Mapping[str, str],
    selection: CodingCliSelection,
    *,
    workspace: Path,
) -> dict[str, str]:
    """Retain only launch, transport, certificate, and documented auth inputs."""

    allowed = (
        _COMMON_SUBPROCESS_ENVIRONMENT | _CODING_CLI_AUTH_ENVIRONMENT[selection.name]
    )
    scrubbed = _selected_environment(environment, allowed)
    scrubbed["PWD"] = str(workspace)
    return scrubbed


@contextmanager
def _coding_cli_process_environment(
    environment: Mapping[str, str],
    selection: CodingCliSelection,
    *,
    workspace: Path,
) -> Iterator[dict[str, str]]:
    """Provide one provider-scoped process environment for an exact invocation."""

    selected = _coding_cli_environment(
        environment,
        selection,
        workspace=workspace,
    )
    if selection.name != "opencode":
        yield selected
        return
    with tempfile.TemporaryDirectory(
        prefix="literate-ai-opencode-config-"
    ) as configuration_directory:
        selected.update(
            {
                "OPENCODE_AUTO_SHARE": "false",
                "OPENCODE_CLIENT": "literate-ai",
                "OPENCODE_CONFIG_CONTENT": _OPENCODE_CONFIG_CONTENT,
                "OPENCODE_CONFIG_DIR": str(Path(configuration_directory).resolve()),
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_CLAUDE_CODE": "true",
                "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "true",
                "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "true",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
                "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
                "OPENCODE_PERMISSION": _OPENCODE_PERMISSION,
            }
        )
        yield selected


def _selected_environment(
    environment: Mapping[str, str], allowed: Collection[str]
) -> dict[str, str]:
    """Select allowed variables using the host's environment-key semantics."""

    if os.name != "nt":
        return {key: value for key, value in environment.items() if key in allowed}
    # Windows environment names are case-insensitive.  Emit the documented spelling so
    # child-process launchers reliably receive required keys such as SystemRoot even
    # when Python enumerates the host key as SYSTEMROOT.
    by_folded_name = {key.casefold(): value for key, value in environment.items()}
    return {
        key: by_folded_name[key.casefold()]
        for key in sorted(allowed)
        if key.casefold() in by_folded_name
    }


def lifecycle_driver_environment(
    environment: Mapping[str, str],
    requested_keys: Sequence[str],
    *,
    workspace: Path,
) -> dict[str, str]:
    """Apply the coding-CLI credential and host-tool policy to a lifecycle driver."""

    unknown = set(requested_keys) - LIFECYCLE_DRIVER_ENVIRONMENT_KEYS
    if unknown:
        raise CodingCliError(
            "coding_cli.environment_key_unauthorized",
            "lifecycle driver requests environment keys outside the controlled "
            "coding-agent and host-tool policy: " + ", ".join(sorted(unknown)),
        )
    coding_provider = environment.get("LITAI_CODING_PROVIDER", "").strip()
    if coding_provider not in {"", "coding-cli", "inherited-session"}:
        raise CodingCliError(
            "coding_provider.unsupported",
            "LITAI_CODING_PROVIDER must be coding-cli or inherited-session",
        )
    selected = environment.get("CODING_CLI", "").strip()
    if selected and selected not in CODING_CLIS:
        raise CodingCliError(
            "coding_cli.unsupported",
            "CODING_CLI must be codex, claude, cursor-agent, or opencode",
        )
    if not selected and coding_provider != "inherited-session":
        for name in CODING_CLIS:
            if shutil.which(name, path=environment.get("PATH")) is not None:
                selected = name
                break
    provider_credentials = (
        frozenset(key for keys in _CODING_CLI_AUTH_ENVIRONMENT.values() for key in keys)
        | _INHERITED_SESSION_ENVIRONMENT
    )
    allowed = set(requested_keys) - provider_credentials
    if coding_provider == "inherited-session":
        allowed.update(_INHERITED_SESSION_ENVIRONMENT)
        allowed.discard("CODING_CLI")
    elif selected:
        allowed.update(_CODING_CLI_AUTH_ENVIRONMENT[selected])
    scrubbed = _selected_environment(environment, allowed)
    if selected and coding_provider != "inherited-session":
        scrubbed["CODING_CLI"] = selected
    scrubbed["PWD"] = str(workspace)
    return scrubbed


@dataclass(frozen=True, slots=True)
class _BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _terminate_process(
    process: subprocess.Popen[bytes],
    ownership: ProcessTreeOwnership | None = None,
) -> None:
    terminate_process_tree(process, ownership=ownership)


def _run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
    maximum_stdout_bytes: int,
    maximum_stderr_bytes: int,
) -> _BoundedProcessResult:
    """Drain both streams concurrently and kill the process on the first overflow."""

    child_environment = inherited_verbose_environment(environment)
    _started = datetime.now(UTC)
    trace_subprocess(command, cwd=cwd, environment=child_environment)
    ownership = create_process_tree_ownership()
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **ownership.popen_options,
        )
    except OSError:
        ownership.release()
        raise
    ownership.bind(process.pid)
    assert process.stdout is not None
    assert process.stderr is not None

    streams = {
        "stdout": (process.stdout, maximum_stdout_bytes),
        "stderr": (process.stderr, maximum_stderr_bytes),
    }
    buffers = {name: bytearray() for name in streams}
    overflows: list[str] = []
    read_errors: list[Exception] = []
    state_lock = threading.Lock()

    def read_stream(name: str) -> None:
        stream, limit = streams[name]
        try:
            while True:
                remaining = limit - len(buffers[name])
                content = stream.read(min(_PIPE_READ_BYTES, remaining + 1))
                if not content:
                    return
                if len(content) > remaining:
                    buffers[name].extend(content[:remaining])
                    with state_lock:
                        overflows.append(name)
                    _terminate_process(process, ownership)
                    return
                buffers[name].extend(content)
        except (OSError, ValueError) as exc:
            with state_lock:
                read_errors.append(exc)
            _terminate_process(process, ownership)
        finally:
            stream.close()

    readers = [
        threading.Thread(
            target=read_stream,
            args=(name,),
            name=f"literate-ai-{name}-reader",
            daemon=True,
        )
        for name in streams
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process(process, ownership)
        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            returncode = -1
    finally:
        for reader in readers:
            reader.join(timeout=5)
        for _name, (stream, _limit) in streams.items():
            if any(reader.is_alive() for reader in readers):
                try:
                    stream.close()
                except OSError:
                    pass
        for reader in readers:
            reader.join(timeout=1)

    if timed_out:
        ownership.release()
        raise CodingCliError(
            "coding_cli.timeout",
            "coding CLI exceeded the configured execution timeout",
        )
    if overflows:
        names = " and ".join(sorted(set(overflows)))
        ownership.release()
        raise CodingCliError(
            "coding_cli.output_limit",
            f"coding CLI exceeded the configured {names} byte limit",
        )
    if read_errors or any(reader.is_alive() for reader in readers):
        ownership.release()
        raise CodingCliError(
            "coding_cli.execution_failed",
            "coding CLI output streams could not be read safely",
        )
    result = _BoundedProcessResult(
        returncode,
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
    )
    trace_subprocess(
        command,
        cwd=cwd,
        environment=child_environment,
        status=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        started_at=_started,
    )
    ownership.release()
    return result


def _require_coding_cli_compatible(
    selection: CodingCliSelection,
    *,
    workspace: Path,
    environment: Mapping[str, str],
) -> None:
    """Reject OpenCode command surfaces that cannot honor LitAI's isolation profile."""

    if selection.name != "opencode":
        return
    command = [selection.executable, *_OPENCODE_PREFLIGHT_COMMAND]
    credential_names = {
        key.casefold() for key in _CODING_CLI_AUTH_ENVIRONMENT["opencode"]
    }
    probe_environment = {
        key: value
        for key, value in environment.items()
        if key.casefold() not in credential_names
    }
    selection.require_unchanged()
    try:
        try:
            completed = _run_bounded(
                command,
                cwd=workspace,
                environment=probe_environment,
                timeout_seconds=_OPENCODE_PREFLIGHT_TIMEOUT_SECONDS,
                maximum_stdout_bytes=_OPENCODE_PREFLIGHT_OUTPUT_BYTES,
                maximum_stderr_bytes=_OPENCODE_PREFLIGHT_OUTPUT_BYTES,
            )
        except (CodingCliError, OSError) as exc:
            raise _opencode_incompatible_error() from exc
    finally:
        selection.require_unchanged()
    help_output = completed.stdout + b"\n" + completed.stderr
    if completed.returncode != 0 or any(
        re.search(
            rb"(?m)^[ \t]*" + re.escape(option) + rb"(?:[ =\t,]|$)",
            help_output,
        )
        is None
        for option in _OPENCODE_REQUIRED_HELP_OPTIONS
    ):
        raise _opencode_incompatible_error()


def _opencode_incompatible_error() -> CodingCliError:
    return CodingCliError(
        "coding_cli.incompatible",
        "selected opencode executable does not provide Literate AI's required "
        "`--pure run` non-interactive interface; upgrade OpenCode and retry",
    )


def _coding_cli_binding_prompt(
    recipe: GenerationRecipe,
    selection: CodingCliSelection,
    *,
    prompt_override: str | None = None,
) -> str:
    isolation = selection.isolation
    limitations = "\n".join(f"- {item}" for item in isolation.limitations)
    return (
        "# Exact coding CLI transport binding\n\n"
        f"Coding CLI: `{selection.name}`\n"
        f"Executable identity: `{selection.executable_identity}`\n"
        f"Portable tool binding: `{selection.tool_binding_identity}`\n"
        f"Isolation profile: `{isolation.profile}`\n"
        f"Hermetic: `{'yes' if isolation.hermetic else 'no'}`\n"
        "Isolation limitations:\n"
        f"{limitations}\n\n"
        + (recipe.prompt() if prompt_override is None else prompt_override)
    )


def _planned_generation_prompt(
    recipe: GenerationRecipe,
    execution_plan: GenerationExecutionPlan,
    stage_request: Mapping[str, object] | None,
    selection: CodingCliSelection,
    *,
    bounded_prompt: bytes | None = None,
) -> str:
    """Bind the exact executable model-stage prefix into one coding-agent request.

    Coding CLIs expose one agent subprocess rather than a provider-neutral structured
    completion API. The adapter therefore asks that one agent to perform the pinned
    model stages in dependency order, keeping non-tree stage output internal and
    materializing only the final source tree. The report identifies this as one
    composite coding-agent invocation; guarded lifecycle stages remain outside it.
    """

    recipe_prompt = recipe.prompt()
    if bounded_prompt is not None:
        if not isinstance(bounded_prompt, bytes) or not bounded_prompt:
            raise CodingCliError(
                "coding_cli.bounded_prompt_invalid",
                "bounded Component prompt must be non-empty exact bytes",
            )
        try:
            prepared_prompt = bounded_prompt.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodingCliError(
                "coding_cli.bounded_prompt_invalid",
                "bounded Component prompt must be canonical UTF-8 text",
            ) from exc
    else:
        prepared_prompt = recipe_prompt
    selected_stage = (
        stage_request.get("stage_id") if stage_request is not None else None
    )
    stages = tuple(
        item
        for item in execution_plan.model_stages
        if selected_stage is None or item.stage_id == selected_stage
    )
    if not stages:
        raise CodingCliError(
            "coding_cli.stage_unavailable",
            "coding CLI stage request is absent from the exact execution plan",
        )
    sections = [
        "# Pinned Literate AI model-stage execution",
        "",
        f"Execution plan identity: `{execution_plan.identity.uri}`",
        f"Workflow identity: `{execution_plan.workflow_reference.identity.uri}`",
        f"Routing identity: `{execution_plan.routing_reference.identity.uri}`",
        "",
        (
            "Perform the model stages below in dependency order within this one coding"
            if selected_stage is None
            else (
                "Perform only orchestrated model stage "
                f"`{selected_stage}` within this coding"
            )
        ),
        "agent invocation. Use exact prior-stage outputs supplied below when present.",
        "Only the final tree-producing stage may write files.",
        "The validate/classify/authorize/build/accept lifecycle is outside this model",
        "boundary and cannot be weakened or simulated by generated output.",
    ]
    suffix = "\n\n" + recipe_prompt
    routes = {
        stage.stage_id: route
        for stage, route in zip(
            execution_plan.model_stages, execution_plan.route_decisions, strict=True
        )
    }
    for stage in stages:
        route = routes[stage.stage_id]
        instructions = stage.instructions
        if instructions.endswith(suffix):
            instructions = instructions[: -len(suffix)]
        sections.extend(
            [
                "",
                f"## Stage `{stage.stage_id}`",
                f"Dependencies: `{', '.join(stage.dependencies) or 'none'}`",
                f"Content kind: `{stage.content_kind}`",
                f"Response schema: `{stage.response_schema_name}`",
                f"Route decision: `{route.digest}`",
                "",
                instructions.strip(),
            ]
        )
    if stage_request is not None:
        prior = stage_request.get("prior_stage_outputs", {})
        input_identity = stage_request.get("input_identity")
        sections.extend(
            [
                "",
                "## Exact orchestrated stage input",
                "",
                f"Input identity: `{canonical_identity(input_identity).uri}`",
                "Prior-stage outputs:",
                "```json",
                canonical_json_bytes(prior).decode("utf-8"),
                "```",
            ]
        )
    binding = _coding_cli_binding_prompt(
        recipe,
        selection,
        prompt_override=prepared_prompt if bounded_prompt is not None else None,
    )
    sections.extend(["", binding.rstrip("\n")])
    return "\n".join(sections) + "\n"


def _requested_stage_ids(
    execution_plan: GenerationExecutionPlan | None,
    stage_request: Mapping[str, object] | None,
) -> tuple[str, ...]:
    if execution_plan is None:
        return ()
    if stage_request is None:
        return tuple(item.stage_id for item in execution_plan.model_stages)
    stage_id = stage_request.get("stage_id")
    return (stage_id,) if isinstance(stage_id, str) else ()


def _requested_route_digests(
    execution_plan: GenerationExecutionPlan | None,
    stage_request: Mapping[str, object] | None,
) -> tuple[str, ...]:
    if execution_plan is None:
        return ()
    stage_ids = _requested_stage_ids(execution_plan, stage_request)
    return tuple(
        route.digest
        for stage, route in zip(
            execution_plan.model_stages, execution_plan.route_decisions, strict=True
        )
        if stage.stage_id in stage_ids
    )


def apply_flavor_selectors(
    catalog: Sequence[RecipeFlavor],
    selectors: Sequence[str],
    *,
    initial: Sequence[RecipeFlavor] = (),
    initial_is_preferences: bool = False,
    multi_value_axes: frozenset[str] = frozenset(),
    slot_axes: Mapping[str, str] | None = None,
    multi_value_slots: frozenset[str] = frozenset(),
) -> tuple[RecipeFlavor, ...]:
    """Compatibility wrapper for provider-neutral ordered Flavor selection."""

    def selection_candidate(flavor: RecipeFlavor) -> FlavorSelectionCandidate:
        candidate_identity = flavor.revision_identity
        if candidate_identity is None:
            candidate_identity = canonical_identity(
                {
                    "flavor_id": flavor.flavor_id,
                    "axis": flavor.axis,
                    "value": flavor.value,
                    "documents": [item.identity for item in flavor.documents],
                    "models": list(flavor.models),
                    "conflicts": list(flavor.conflicts),
                    "coordinate_uri": flavor.coordinate_uri,
                    "skills": [item.to_dict() for item in flavor.skills],
                    "specification_set_identity": flavor.specification_set_identity,
                }
            ).uri
        return FlavorSelectionCandidate(
            candidate_identity=candidate_identity,
            flavor_id=flavor.flavor_id,
            axis=flavor.axis,
            value=flavor.value,
            conflicts=flavor.conflicts,
            coordinate_uri=flavor.coordinate_uri,
            slot_ids=flavor.slot_ids,
        )

    catalog_candidates = tuple(selection_candidate(flavor) for flavor in catalog)
    initial_candidates = tuple(selection_candidate(flavor) for flavor in initial)
    source_by_identity = {
        selection_candidate(flavor).candidate_identity: flavor
        for flavor in (*catalog, *initial)
    }
    try:
        selected = apply_ordered_flavor_selectors(
            catalog_candidates,
            selectors,
            initial=initial_candidates,
            initial_is_preferences=initial_is_preferences,
            multi_value_axes=multi_value_axes,
            slot_axes=slot_axes,
            multi_value_slots=multi_value_slots,
        )
    except FlavorSelectionError as exc:
        suffix = exc.code.removeprefix("flavor_selection.")
        raise CodingCliError(f"coding_cli.{suffix}", exc.message) from exc
    return tuple(
        replace(source_by_identity[item.candidate_identity], slot_ids=item.slot_ids)
        for item in selected
    )


__all__ = [
    "CODING_CLIS",
    "CodingCliError",
    "CodingCliGeneration",
    "CodingCliIsolation",
    "CodingCliSelection",
    "CodingCliSourceGenerator",
    "CodingCliTaskResult",
    "CodingCliTaskRunner",
    "GenerationRecipe",
    "RecipeDocument",
    "RecipeDeploymentUnit",
    "RecipeFlavor",
    "RecipeLibraryDependency",
    "RecipeSkill",
    "apply_flavor_selectors",
    "inspect_coding_cli_authentication",
    "select_coding_cli",
]
