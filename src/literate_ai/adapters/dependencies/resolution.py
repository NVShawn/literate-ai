"""Post-build CycloneDX resolution, including Bzlmod evidence."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from univers.version_range import VersionRange

from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    CycloneDxRepositorySourceResolution,
    canonical_identity,
    canonical_json_bytes,
    canonical_relative_posix_path,
)
from literate_ai.ports.contracts import (
    BuildDependencyObservation,
    BuildInputConsumption,
    BzlmodModule,
    BzlmodRootModule,
    PortContractError,
)

from .acquisition import (
    _generated_lock_projection,
    _lock_coordinate,
    _LockedPackage,
    _LockProjection,
    _matching_locked_package,
    _require_lock_graph_covered,
    _source_package_identity,
)
from .admission import _cargo_dependencies, reconcile_generated_dependencies
from .cyclonedx import (
    CycloneDxBomError,
    build_cyclonedx_bom,
    validate_cyclonedx_bom,
    validate_resolved_cyclonedx_bom,
)
from .observation import (
    _MAX_OBSERVED_FILES,
    _MAX_TOOL_OUTPUT,
    PortableHostDependencyObserver,
    _stable_file_bytes,
)
from .python_resolution import PythonDependencyEvidence, project_python_evidence
from .python_source import PythonSourceAuthority
from .types import DependencyObservationError, HostDependencyObservation

_BAZEL_MODULE_NAME = re.compile(r"^[a-z][a-z0-9._-]*$")

_BZLMOD_EVIDENCE_LAYOUT = {
    "bazel-module-lock": ".literate/bazel/MODULE.bazel.lock",
    "bazel-module-graph": ".literate/bazel/module-graph.json",
    "bazel-repository-definitions": ".literate/bazel/repositories.ndjson",
}

_BZLMOD_BUILDFILES_PATH = ".literate/bazel/buildfiles.txt"

_BZLMOD_SOURCE_INPUTS_PATH = ".literate/bazel/source-inputs.txt"

_BZLMOD_BUILD_INPUT_CONSUMPTION_PATH = ".literate/bazel/build-input-consumption.json"

_STANDARD_BAZEL_EVIDENCE_MANIFEST = ".literate/bazel/evidence-manifest.json"

_BAZEL_GRAPH_NODE_FIELDS = frozenset(
    {
        "key",
        "name",
        "version",
        "apparentName",
        "dependencies",
        "indirectDependencies",
        "cycles",
        "root",
        "unexpanded",
    }
)

_BAZEL_REPOSITORY_FIELDS = frozenset(
    {
        "canonicalName",
        "repoRuleName",
        "repoRuleBzlLabel",
        "apparentName",
        "moduleKey",
        "originalName",
        "attribute",
    }
)


@dataclass(frozen=True, slots=True)
class _BazelDirectDependency:
    name: str
    requested_version: str


@dataclass(frozen=True, slots=True)
class _BazelModuleIntent:
    path: str
    name: str
    version: str
    dependencies: tuple[_BazelDirectDependency, ...]


@dataclass(frozen=True, slots=True)
class _BzlmodResolution:
    components: tuple[dict[str, object], ...]
    edges: tuple[tuple[str, str], ...]
    authoritative_versions: tuple[tuple[str, str], ...]


class CycloneDxLifecycleResolver:
    """Build and verify post-build CycloneDX evidence before any tests execute."""

    resolver_id = "literate-ai/cyclonedx-host-resolution@1"

    def __init__(
        self,
        *,
        managed_graph: CycloneDxManagedGraph,
        observer: PortableHostDependencyObserver,
        evidence_path: Path,
        additional_components: Sequence[Mapping[str, object]] = (),
        additional_edges: Sequence[tuple[str, str]] = (),
        repository_resolutions: Sequence[CycloneDxRepositorySourceResolution] = (),
        allow_missing_cargo_lock: bool = False,
        runtime_python_imports: frozenset[str] = frozenset(),
        python_source_authority: PythonSourceAuthority | None = None,
        python_dependency_observer: Callable[[], PythonDependencyEvidence]
        | None = None,
    ) -> None:
        self.managed_graph = managed_graph
        self.observer = observer
        self.evidence_path = evidence_path
        self.additional_components = tuple(dict(item) for item in additional_components)
        self.additional_edges = tuple(additional_edges)
        self.repository_resolutions = tuple(repository_resolutions)
        self.allow_missing_cargo_lock = allow_missing_cargo_lock
        self.runtime_python_imports = runtime_python_imports
        self.python_source_authority = python_source_authority
        self.python_dependency_observer = python_dependency_observer
        if python_dependency_observer is not None and python_source_authority is None:
            raise DependencyObservationError(
                "dependencies.python-source-invalid",
                "Python installed observation requires explicit source authority",
            )

    def validate_source(self, artifact: Mapping[str, object]) -> Mapping[str, object]:
        files, source_content = self._source_inputs(artifact)
        source_binding = validate_cyclonedx_bom(
            source_content,
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=self.managed_graph,
        )
        bazel_intent = _bazel_module_intent(files)
        if bazel_intent is not None and bazel_intent.dependencies:
            _reconcile_bazel_source_intent(
                bazel_intent,
                source_content,
                root_ref=self.managed_graph.root_ref,
            )
        elif not (
            self.allow_missing_cargo_lock
            and any(
                PurePosixPath(path).name == "Cargo.toml"
                and _cargo_dependencies(content)
                for path, content in files.items()
                if isinstance(path, str) and isinstance(content, str)
            )
        ):
            _require_complete_source_composition(
                source_content, root_ref=self.managed_graph.root_ref
            )
        reconcile_generated_dependencies(
            files,
            source_content,
            allow_missing_cargo_lock=self.allow_missing_cargo_lock,
            runtime_python_imports=self.runtime_python_imports,
        )
        lock_projection = _generated_lock_projection(
            files,
            allow_missing_cargo_lock=self.allow_missing_cargo_lock,
            python_source_authority=self.python_source_authority,
        )
        if any(
            package.ecosystem in {"npm", "pypi"} and not package.root
            for package in lock_projection.packages
        ):
            # Package acquisition is network-capable. Prove source BOM ranges and
            # dependency edges cover the exact lock graph before that authority is
            # granted, rather than discovering the mismatch after package download.
            _resolved_source_projection(
                source_content,
                managed_graph=self.managed_graph,
                resolution_candidates=(),
                lock_projection=lock_projection,
                require_package_dependency_kind=True,
            )
        identity_material: dict[str, object] = {
            "resolver_id": self.resolver_id,
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "source_bom": source_binding.to_dict(),
        }
        return {
            **identity_material,
            "validation_identity": canonical_identity(identity_material).uri,
        }

    def resolve(
        self, artifact: Mapping[str, object], build: Mapping[str, object]
    ) -> Mapping[str, object]:
        files, source_content = self._source_inputs(artifact)
        source_validation = self.validate_source(artifact)
        source_binding = CycloneDxBomBinding.from_dict(source_validation["source_bom"])
        resolution_files = files
        cargo_lock = build.get("cargo_lock")
        cargo_lifecycle_profile = build.get("cargo_lifecycle_profile") is True
        if (
            self.allow_missing_cargo_lock
            and cargo_lock is None
            and any(
                PurePosixPath(path).name == "Cargo.toml"
                for path in files
                if isinstance(path, str)
            )
        ):
            raise DependencyObservationError(
                "dependencies.rust-build-lock-missing",
                "Cargo resolution requires its post-authorization derived lock",
            )
        if cargo_lock is not None:
            if not (self.allow_missing_cargo_lock or cargo_lifecycle_profile):
                raise DependencyObservationError(
                    "dependencies.rust-build-lock-unexpected",
                    "derived Cargo lock evidence requires the Cargo lifecycle profile",
                )
            if not isinstance(cargo_lock, Mapping):
                raise DependencyObservationError(
                    "dependencies.rust-build-lock-invalid",
                    "Cargo build evidence must contain one derived lock record",
                )
            path = cargo_lock.get("path")
            content = cargo_lock.get("content")
            if (
                not isinstance(path, str)
                or PurePosixPath(path).name != "Cargo.lock"
                or not isinstance(content, str)
                or path in files
            ):
                raise DependencyObservationError(
                    "dependencies.rust-build-lock-invalid",
                    "Cargo build evidence contains an invalid derived lock",
                )
            resolution_files = {**files, path: content}
        observed = self.observer.observe(build, root_ref=self.managed_graph.root_ref)
        python_observed = HostDependencyObservation((), ())
        python_evidence = None
        if self.python_source_authority is not None:
            if self.python_dependency_observer is None:
                raise DependencyObservationError(
                    "dependencies.python-acquisition-evidence-missing",
                    "Python resolution requires fresh lifecycle-owned "
                    "payload verification",
                )
            python_evidence = self.python_dependency_observer()
            python_observed = project_python_evidence(
                source_content,
                root_ref=self.managed_graph.root_ref,
                source=self.python_source_authority,
                evidence=python_evidence,
            )
            if any(
                (_source_package_identity(component) or (None,))[0] == "pypi"
                for component in (*observed.components, *self.additional_components)
            ):
                raise DependencyObservationError(
                    "dependencies.python-resolution-invalid",
                    "Python packages must come only from the lifecycle "
                    "payload verifier",
                )
            reconcile_generated_dependencies(
                files,
                source_content,
                allow_missing_cargo_lock=self.allow_missing_cargo_lock,
                observed_python_imports=frozenset(
                    alias
                    for component in python_observed.components
                    for alias in _dependency_property_values(component).get(
                        "literate-ai:python-top-level-import", ()
                    )
                ),
            )
        bazel_intent = _bazel_module_intent(files)
        bazel_resolution = (
            _bzlmod_resolution_observation(
                build,
                intent=bazel_intent,
                source_content=source_content,
                root_ref=self.managed_graph.root_ref,
            )
            if bazel_intent is not None
            else _BzlmodResolution((), (), ())
        )
        components, source_edges = _resolved_source_projection(
            source_content,
            managed_graph=self.managed_graph,
            resolution_candidates=(
                *self.additional_components,
                *bazel_resolution.components,
                *observed.components,
                *python_observed.components,
            ),
            lock_projection=_generated_lock_projection(
                resolution_files, python_source_authority=self.python_source_authority
            ),
            authoritative_bzlmod_versions=dict(bazel_resolution.authoritative_versions),
        )
        edges = (
            *source_edges,
            *self.additional_edges,
            *bazel_resolution.edges,
            *observed.edges,
            *python_observed.edges,
        )
        resolved_content, resolved_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=self.managed_graph,
            additional_components=components,
            additional_edges=edges,
            source_bom=source_content,
            source_managed_graph=self.managed_graph,
            repository_resolutions=self.repository_resolutions,
        )
        continuity_source, continuity_resolved = validate_resolved_cyclonedx_bom(
            resolved_content,
            source_content=source_content,
            source_managed_graph=self.managed_graph,
            repository_resolutions=self.repository_resolutions,
        )
        if (
            continuity_source != source_binding
            or continuity_resolved != resolved_binding
        ):
            raise CycloneDxBomError(
                "sbom.transition-binding-mismatch",
                "source-to-resolved validation returned different CycloneDX bindings",
            )
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.evidence_path.name}.", dir=self.evidence_path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_bytes(resolved_content)
            os.replace(temporary, self.evidence_path)
        finally:
            temporary.unlink(missing_ok=True)
        identity_material: dict[str, object] = {
            "resolver_id": self.resolver_id,
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": build["source_bundle_digest"],
            "artifact_digest": build["artifact_digest"],
            "source_validation_identity": source_validation["validation_identity"],
            "source_bom": source_binding.to_dict(),
            "resolved_bom": resolved_binding.to_dict(),
            "repository_resolutions": [
                item.to_dict() for item in self.repository_resolutions
            ],
        }
        result: dict[str, object] = {
            **identity_material,
            "resolved_bom_path": str(self.evidence_path),
        }
        if python_evidence is not None:
            identity_material["python_dependency_evidence_identity"] = (
                python_evidence.identity.uri
            )
            result["python_dependency_evidence_identity"] = python_evidence.identity.uri
        result["resolution_identity"] = canonical_identity(identity_material).uri
        return result

    @staticmethod
    def _source_inputs(
        artifact: Mapping[str, object],
    ) -> tuple[Mapping[str, object], bytes]:
        files = artifact.get("files")
        if not isinstance(files, Mapping) or any(
            not isinstance(path, str) or not isinstance(content, str)
            for path, content in files.items()
        ):
            raise DependencyObservationError(
                "dependencies.generated-files-invalid",
                "dependency resolution requires the exact generated source files",
            )
        source_text = files.get(CYCLONEDX_SOURCE_SBOM_PATH)
        if not isinstance(source_text, str):
            raise DependencyObservationError(
                "dependencies.source-bom-missing",
                f"generated source omits {CYCLONEDX_SOURCE_SBOM_PATH}",
            )
        return files, source_text.encode("utf-8")


def _bazel_module_intent(files: Mapping[str, object]) -> _BazelModuleIntent | None:
    """Parse the deliberately small, inert Bzlmod declaration subset."""

    text_files = {
        path: content
        for path, content in files.items()
        if isinstance(path, str) and isinstance(content, str)
    }
    module_lock_paths = sorted(
        path for path in text_files if PurePosixPath(path).name == "MODULE.bazel.lock"
    )
    if module_lock_paths:
        raise DependencyObservationError(
            "dependencies.bzlmod-generated-lock-forbidden",
            "generated source must not cache Bazel's resolved module lock: "
            + ", ".join(module_lock_paths),
        )
    workspace_paths = sorted(
        path
        for path in text_files
        if PurePosixPath(path).name in {"WORKSPACE", "WORKSPACE.bazel"}
    )
    if workspace_paths:
        raise DependencyObservationError(
            "dependencies.bzlmod-workspace-unsupported",
            "generated source uses legacy WORKSPACE dependency authority: "
            + ", ".join(workspace_paths),
        )
    module_paths = sorted(
        path for path in text_files if PurePosixPath(path).name == "MODULE.bazel"
    )
    if not module_paths:
        return None
    if len(module_paths) != 1:
        raise DependencyObservationError(
            "dependencies.bzlmod-module-ambiguous",
            "generated source must contain at most one MODULE.bazel authority",
        )
    path = module_paths[0]
    if path != "source/MODULE.bazel":
        raise DependencyObservationError(
            "dependencies.bzlmod-module-location-invalid",
            "MODULE.bazel must be at the generated source root",
        )
    try:
        document = ast.parse(text_files[path], filename=path, mode="exec")
    except SyntaxError as exc:
        raise DependencyObservationError(
            "dependencies.bzlmod-module-invalid",
            f"generated {path} is not in the supported static MODULE.bazel subset",
        ) from exc

    module_call: Mapping[str, object] | None = None
    direct: dict[str, _BazelDirectDependency] = {}
    for statement in document.body:
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id in {"module", "bazel_dep"}
        ):
            raise DependencyObservationError(
                "dependencies.bzlmod-authority-unsupported",
                f"generated {path} contains dynamic or unsupported Bzlmod authority",
            )
        call = statement.value
        if call.args:
            raise DependencyObservationError(
                "dependencies.bzlmod-authority-unsupported",
                f"generated {path} must use keyword-only literal declarations",
            )
        values = _bazel_literal_keywords(call, path=path)
        if call.func.id == "module":
            if module_call is not None:
                raise DependencyObservationError(
                    "dependencies.bzlmod-module-invalid",
                    f"generated {path} contains more than one module() declaration",
                )
            _require_bazel_keyword_shape(
                values,
                required={"name", "version"},
                optional=set(),
                path=path,
                call="module",
            )
            module_call = values
            continue

        _require_bazel_keyword_shape(
            values,
            required={"name", "version"},
            optional=set(),
            path=path,
            call="bazel_dep",
        )
        if module_call is None:
            raise DependencyObservationError(
                "dependencies.bzlmod-module-invalid",
                f"generated {path} must declare module() before bazel_dep()",
            )
        name = _bazel_text(values.get("name"), path=path, field="bazel_dep.name")
        version = _bazel_text(
            values.get("version"), path=path, field="bazel_dep.version"
        )
        if not _BAZEL_MODULE_NAME.fullmatch(name):
            raise DependencyObservationError(
                "dependencies.bzlmod-module-name-invalid",
                f"generated {path} contains invalid Bazel module name {name!r}",
            )
        if name in direct:
            raise DependencyObservationError(
                "dependencies.bzlmod-dependency-duplicate",
                f"generated {path} declares Bazel module {name!r} more than once",
            )
        direct[name] = _BazelDirectDependency(name, version)

    if module_call is None:
        raise DependencyObservationError(
            "dependencies.bzlmod-module-invalid",
            f"generated {path} omits its literal module() declaration",
        )
    name = _bazel_text(module_call.get("name"), path=path, field="module.name")
    version = _bazel_text(module_call.get("version"), path=path, field="module.version")
    if not _BAZEL_MODULE_NAME.fullmatch(name) or name in direct:
        raise DependencyObservationError(
            "dependencies.bzlmod-module-name-invalid",
            f"generated {path} contains an invalid or self-referential module name",
        )
    dependencies = tuple(sorted(direct.values(), key=lambda item: item.name))
    return _BazelModuleIntent(path, name, version, dependencies)


def _bazel_literal_keywords(call: ast.Call, *, path: str) -> dict[str, object]:
    values: dict[str, object] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in values:
            raise DependencyObservationError(
                "dependencies.bzlmod-authority-unsupported",
                f"generated {path} contains expanded or duplicate Bzlmod arguments",
            )
        try:
            value = ast.literal_eval(keyword.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
            raise DependencyObservationError(
                "dependencies.bzlmod-authority-unsupported",
                f"generated {path} contains a non-literal Bzlmod argument",
            ) from exc
        if not isinstance(value, (str, int, bool, list, tuple)):
            raise DependencyObservationError(
                "dependencies.bzlmod-literal-invalid",
                f"generated {path} contains an unsupported Bzlmod literal",
            )
        values[keyword.arg] = value
    return values


def _require_bazel_keyword_shape(
    values: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str],
    path: str,
    call: str,
) -> None:
    if not required <= set(values) or set(values) - required - optional:
        raise DependencyObservationError(
            "dependencies.bzlmod-authority-unsupported",
            f"generated {path} uses unsupported or incomplete {call}() arguments",
        )


def _bazel_text(value: object, *, path: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DependencyObservationError(
            "dependencies.bzlmod-literal-invalid",
            f"generated {path} has an invalid literal {field}",
        )
    return value


def _require_complete_source_composition(
    source_content: bytes, *, root_ref: str
) -> None:
    try:
        document = json.loads(source_content)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise DependencyObservationError(
            "dependencies.source-bom-invalid", "source BOM is not valid JSON"
        ) from exc
    if not isinstance(document, Mapping) or document.get("compositions") != [
        {"aggregate": "complete", "dependencies": [root_ref]}
    ]:
        raise DependencyObservationError(
            "dependencies.source-composition-unjustified",
            "source BOM may be incomplete only for supported deferred "
            "Bzlmod dependencies",
        )


def _reconcile_bazel_source_intent(
    intent: _BazelModuleIntent,
    source_content: bytes,
    *,
    root_ref: str,
) -> dict[str, str]:
    try:
        document = json.loads(source_content)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise DependencyObservationError(
            "dependencies.source-bom-invalid", "source BOM is not valid JSON"
        ) from exc
    if not isinstance(document, Mapping):
        raise DependencyObservationError(
            "dependencies.source-bom-invalid", "source BOM root is invalid"
        )
    compositions = document.get("compositions")
    if compositions != [
        {
            "aggregate": "incomplete_third_party_only",
            "dependencies": [root_ref],
        }
    ]:
        raise DependencyObservationError(
            "dependencies.bzlmod-source-composition-invalid",
            "Bzlmod requests require incomplete_third_party_only source composition",
        )
    raw_components = document.get("components")
    raw_dependencies = document.get("dependencies")
    if not isinstance(raw_components, list) or not isinstance(raw_dependencies, list):
        raise DependencyObservationError(
            "dependencies.source-bom-invalid",
            "source BOM inventory or dependency graph is invalid",
        )
    direct_by_name = {item.name: item for item in intent.dependencies}
    source_ref_by_name: dict[str, str] = {}
    for raw in raw_components:
        if not isinstance(raw, Mapping):
            continue
        package = _source_package_identity(raw)
        if package is None or package[0] != "generic":
            continue
        properties = _dependency_property_values(raw)
        if properties.get("literate-ai:dependency-kind") != ("build",):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or name not in direct_by_name:
            continue
        declaration = direct_by_name[name]
        if package[1] != _lock_coordinate("generic", declaration.name):
            raise DependencyObservationError(
                "dependencies.bzlmod-source-intent-invalid",
                f"source BOM purl does not identify Bazel module {name!r}",
            )
        ref = raw.get("bom-ref")
        if not isinstance(ref, str) or not ref or name in source_ref_by_name:
            raise DependencyObservationError(
                "dependencies.bzlmod-source-intent-invalid",
                f"source BOM does not identify Bazel module {name!r} exactly once",
            )
        if (
            raw.get("type") != "library"
            or raw.get("isExternal") is not True
            or "version" in raw
            or "hashes" in raw
            or raw.get("purl") != f"pkg:generic/{declaration.name}"
            or raw.get("versionRange")
            != f"vers:generic/>={declaration.requested_version}"
            or properties.get("literate-ai:dependency-scope") != ("build",)
            or properties.get("literate-ai:bzlmod-requested-version")
            != (declaration.requested_version,)
        ):
            raise DependencyObservationError(
                "dependencies.bzlmod-source-intent-invalid",
                "source BOM must preserve Bazel request "
                f"{name}@{declaration.requested_version} as unresolved build intent",
            )
        source_ref_by_name[name] = ref
    missing = sorted(set(direct_by_name) - set(source_ref_by_name))
    if missing:
        raise DependencyObservationError(
            "dependencies.bzlmod-source-intent-invalid",
            "source BOM omits direct Bazel module requests: " + ", ".join(missing),
        )
    root_entries = [
        raw
        for raw in raw_dependencies
        if isinstance(raw, Mapping) and raw.get("ref") == root_ref
    ]
    if len(root_entries) != 1 or not isinstance(root_entries[0].get("dependsOn"), list):
        raise DependencyObservationError(
            "dependencies.bzlmod-source-intent-invalid",
            "source BOM lacks its exact root dependency entry",
        )
    targets = root_entries[0]["dependsOn"]
    if any(ref not in targets for ref in source_ref_by_name.values()):
        raise DependencyObservationError(
            "dependencies.bzlmod-source-intent-invalid",
            "source BOM root omits a direct Bazel module request edge",
        )
    return source_ref_by_name


def _dependency_property_values(
    component: Mapping[str, object],
) -> dict[str, tuple[str, ...]]:
    raw_properties = component.get("properties")
    if not isinstance(raw_properties, list):
        return {}
    values: dict[str, list[str]] = {}
    for raw in raw_properties:
        if not isinstance(raw, Mapping):
            return {}
        name, value = raw.get("name"), raw.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return {}
        values.setdefault(name, []).append(value)
    return {name: tuple(items) for name, items in values.items()}


def _bzlmod_resolution_observation(
    build: Mapping[str, object],
    *,
    intent: _BazelModuleIntent,
    source_content: bytes,
    root_ref: str,
) -> _BzlmodResolution:
    raw_observation = build.get("dependency_observation")
    if raw_observation is None:
        raise DependencyObservationError(
            "dependencies.bzlmod-observation-missing",
            "Bzlmod dependencies require strict post-build "
            "dependency_observation evidence",
        )
    try:
        observation = BuildDependencyObservation.from_dict(raw_observation)
    except (PortContractError, TypeError, ValueError) as exc:
        raise DependencyObservationError(
            "dependencies.bzlmod-observation-invalid",
            "Bzlmod dependency observation violates its strict wire contract",
        ) from exc
    if observation.resolver_id != "bazel/bzlmod@1":
        raise DependencyObservationError(
            "dependencies.bzlmod-observation-resolver-mismatch",
            "Bzlmod dependency observation came from another resolver",
        )
    if observation.source_bundle_digest != build.get("source_bundle_digest"):
        raise DependencyObservationError(
            "dependencies.bzlmod-observation-source-mismatch",
            "Bzlmod dependency observation describes another source bundle",
        )
    if observation.build_toolchain_identity != build.get("toolchain_identity"):
        raise DependencyObservationError(
            "dependencies.bzlmod-observation-toolchain-mismatch",
            "Bzlmod dependency observation describes another build toolchain",
        )
    evidence = _verify_bzlmod_evidence_artifacts(build, observation)
    _require_raw_bazel_graph(evidence["bazel-module-graph"], observation=observation)
    _require_raw_bazel_lock(evidence["bazel-module-lock"], modules=observation.modules)
    _require_raw_bazel_repositories(
        evidence["bazel-repository-definitions"], modules=observation.modules
    )
    root_module = observation.root_module
    if (root_module.name, root_module.version) != (
        intent.name,
        intent.version,
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-observation-root-mismatch",
            "Bzlmod dependency observation describes another root module",
        )
    modules = observation.modules
    module_by_key = {item.key: item for item in modules}
    module_names: set[str] = set()
    for module in modules:
        if (
            not _BAZEL_MODULE_NAME.fullmatch(module.name)
            or module.key != f"{module.name}@{module.version}"
            or module.name in module_names
        ):
            raise DependencyObservationError(
                "dependencies.bzlmod-observation-module-invalid",
                "Bzlmod observation has a non-canonical module key or duplicate name",
            )
        module_names.add(module.name)
    root_dependencies = root_module.dependencies
    declarations = {item.name: item for item in intent.dependencies}
    selected_direct: dict[str, BzlmodModule] = {}
    for key in root_dependencies:
        selected = module_by_key[key]
        name = selected.name
        declaration = declarations.get(name)
        if declaration is None or name in selected_direct:
            raise DependencyObservationError(
                "dependencies.bzlmod-observation-direct-mismatch",
                "resolved Bzlmod root edges do not match direct declarations",
            )
        selected_direct[name] = selected
    if set(selected_direct) != set(declarations):
        raise DependencyObservationError(
            "dependencies.bzlmod-observation-direct-mismatch",
            "resolved Bzlmod graph omits a direct module declaration",
        )

    reachable: set[str] = set()
    pending = list(root_dependencies)
    while pending:
        key = pending.pop()
        if key in reachable:
            continue
        reachable.add(key)
        pending.extend(module_by_key[key].dependencies)
    if reachable != set(module_by_key):
        raise DependencyObservationError(
            "dependencies.bzlmod-observation-graph-incomplete",
            "Bzlmod observation contains modules outside the complete reachable graph",
        )

    source_ref_by_name = (
        _reconcile_bazel_source_intent(intent, source_content, root_ref=root_ref)
        if intent.dependencies
        else {}
    )
    ref_by_key: dict[str, str] = {}
    for key, module in module_by_key.items():
        name, version = module.name, module.version
        ref_by_key[key] = source_ref_by_name.get(name, f"pkg:generic/{name}@{version}")
    properties = (
        {"name": "literate-ai:dependency-kind", "value": "build"},
        {"name": "literate-ai:dependency-scope", "value": "build"},
        {
            "name": "literate-ai:bzlmod-graph-identity",
            "value": observation.graph_identity,
        },
        {
            "name": "literate-ai:bzlmod-evidence-identity",
            "value": observation.evidence_identity,
        },
        {
            "name": "literate-ai:bzlmod-observation-identity",
            "value": observation.observation_identity,
        },
        {
            "name": "literate-ai:bzlmod-resolver-toolchain-identity",
            "value": observation.resolver_toolchain_identity,
        },
    )
    components: list[dict[str, object]] = []
    for key, module in module_by_key.items():
        name, version = module.name, module.version
        components.append(
            {
                "type": "library",
                "bom-ref": ref_by_key[key],
                "name": name,
                "version": version,
                "purl": f"pkg:generic/{name}@{version}",
                "isExternal": True,
                "properties": [
                    *properties,
                    {"name": "literate-ai:bzlmod-module-key", "value": key},
                    {
                        "name": "literate-ai:bzlmod-selected-version",
                        "value": version,
                    },
                ],
            }
        )
    edges = {(root_ref, ref_by_key[key]) for key in root_dependencies}
    edges.update(
        (ref_by_key[module.key], ref_by_key[target])
        for module in modules
        for target in module.dependencies
    )
    return _BzlmodResolution(
        tuple(sorted(components, key=lambda item: str(item["bom-ref"]))),
        tuple(sorted(edges)),
        tuple(
            sorted(
                (source_ref_by_name[name], module.version)
                for name, module in selected_direct.items()
            )
        ),
    )


def _verify_bzlmod_evidence_artifacts(
    build: Mapping[str, object], observation: BuildDependencyObservation
) -> dict[str, bytes]:
    if build.get("standard_bazel_evidence_manifest") is not None:
        return _verify_standard_bzlmod_evidence_artifacts(build, observation)
    raw_root = build.get("artifact_path")
    if not isinstance(raw_root, str) or not raw_root:
        raise DependencyObservationError(
            "dependencies.bzlmod-evidence-root-invalid",
            "Bzlmod dependency observation requires its exact build artifact directory",
        )
    root = Path(raw_root)
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.bzlmod-evidence-root-invalid",
            "Bzlmod build artifact directory is unavailable",
        ) from exc
    root_attributes = getattr(root_metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_attributes & 0x0400
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-evidence-root-invalid",
            "Bzlmod build artifact root must be a non-link directory",
        )
    layout = {item.kind: item.logical_name for item in observation.evidence_artifacts}
    if layout != _BZLMOD_EVIDENCE_LAYOUT:
        raise DependencyObservationError(
            "dependencies.bzlmod-evidence-layout-invalid",
            "Bzlmod observation must contain the exact public evidence triplet",
        )

    manifest_path = root / "build-manifest.json"
    manifest_content = _stable_file_bytes(manifest_path, limit=_MAX_TOOL_OUTPUT + 1)
    if len(manifest_content) > _MAX_TOOL_OUTPUT:
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-size-invalid",
            "Bzlmod composite build manifest exceeds the evidence limit",
        )
    manifest = _strict_json_document(
        manifest_content,
        code="dependencies.bzlmod-manifest-invalid",
        label="Bzlmod composite build manifest",
    )
    if canonical_json_bytes(manifest) != manifest_content:
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-noncanonical",
            "Bzlmod composite build manifest is not canonical JSON",
        )
    if "sha256:" + hashlib.sha256(manifest_content).hexdigest() != build.get(
        "artifact_digest"
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-identity-mismatch",
            "Bzlmod composite build manifest does not bind the build artifact",
        )
    consumption = _require_bzlmod_manifest_authority(
        manifest, build=build, observation=observation
    )
    _require_bzlmod_build_input_evidence(root, consumption)

    evidence_by_path = {
        item.logical_name: item for item in observation.evidence_artifacts
    }
    regular_files: list[tuple[str, Path]] = []
    observed_entries = 0
    for path in root.rglob("*"):
        observed_entries += 1
        if observed_entries > _MAX_OBSERVED_FILES:
            raise DependencyObservationError(
                "dependencies.bzlmod-artifact-limit",
                "Bzlmod composite artifact contains too many filesystem entries",
            )
        metadata = path.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x0400:
            raise DependencyObservationError(
                "dependencies.bzlmod-evidence-unsafe",
                "Bzlmod composite artifact contains a link or junction",
            )
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise DependencyObservationError(
                "dependencies.bzlmod-evidence-unsafe",
                "Bzlmod composite artifact contains a special file",
            )
        relative = path.relative_to(root).as_posix()
        if relative == "build-manifest.json":
            continue
        regular_files.append((relative, path))

    actual_records: list[dict[str, str]] = []
    evidence_contents: dict[str, bytes] = {}
    # Filesystem Path ordering is host-specific: Windows compares paths without
    # case, while manifest authority uses canonical text ordering. Validate safety
    # above, then read regular files in portable POSIX-path order on every host.
    for relative, path in sorted(regular_files, key=lambda item: item[0]):
        content = _stable_file_bytes(path)
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        actual_records.append({"path": relative, "digest": digest})
        descriptor = evidence_by_path.get(relative)
        if descriptor is None:
            continue
        if len(content) > _MAX_TOOL_OUTPUT:
            raise DependencyObservationError(
                "dependencies.bzlmod-evidence-size-invalid",
                f"Bzlmod evidence artifact is too large: {relative}",
            )
        if digest != descriptor.content_identity:
            raise DependencyObservationError(
                "dependencies.bzlmod-evidence-content-mismatch",
                f"Bzlmod evidence artifact changed: {relative}",
            )
        evidence_contents[descriptor.kind] = content
    if set(evidence_contents) != set(_BZLMOD_EVIDENCE_LAYOUT):
        raise DependencyObservationError(
            "dependencies.bzlmod-evidence-missing",
            "Bzlmod composite artifact omits required public evidence",
        )
    if manifest.get("files") != actual_records:
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-files-mismatch",
            "Bzlmod composite manifest does not cover its exact regular files",
        )
    return evidence_contents


def _verify_standard_bzlmod_evidence_artifacts(
    build: Mapping[str, object], observation: BuildDependencyObservation
) -> dict[str, bytes]:
    """Verify Standard's detached Bazel evidence before resolved-SBOM mutation."""

    raw_root = build.get("artifact_path")
    if not isinstance(raw_root, str) or not raw_root:
        raise DependencyObservationError(
            "dependencies.bzlmod-evidence-root-invalid",
            "Standard Bzlmod evidence requires its exact artifact directory",
        )
    root = Path(raw_root)
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.bzlmod-evidence-root-invalid",
            "Standard Bzlmod artifact directory is unavailable",
        ) from exc
    root_attributes = getattr(root_metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_attributes & 0x0400
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-evidence-root-invalid",
            "Standard Bzlmod artifact root must be a non-link directory",
        )
    if build.get("standard_bazel_evidence_manifest") != (
        _STANDARD_BAZEL_EVIDENCE_MANIFEST
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-invalid",
            "Standard Bzlmod evidence manifest uses a noncanonical path",
        )
    manifest_path = root.joinpath(
        *PurePosixPath(_STANDARD_BAZEL_EVIDENCE_MANIFEST).parts
    )
    manifest_content = _stable_file_bytes(manifest_path, limit=_MAX_TOOL_OUTPUT + 1)
    if len(manifest_content) > _MAX_TOOL_OUTPUT:
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-size-invalid",
            "Standard Bzlmod evidence manifest exceeds the evidence limit",
        )
    manifest = _strict_json_document(
        manifest_content,
        code="dependencies.bzlmod-manifest-invalid",
        label="Standard Bzlmod evidence manifest",
    )
    if canonical_json_bytes(manifest) != manifest_content:
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-noncanonical",
            "Standard Bzlmod evidence manifest is not canonical JSON",
        )
    manifest_digest = "sha256:" + hashlib.sha256(manifest_content).hexdigest()
    if manifest_digest != build.get("artifact_digest") or manifest_digest != build.get(
        "evidence_manifest_digest"
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-identity-mismatch",
            "Standard Bzlmod evidence manifest differs from its bound identity",
        )
    required = {
        "schema",
        "authorization_id",
        "builder_id",
        "component_revision",
        "build_plan_identity",
        "source_bundle_digest",
        "build_toolchain_identity",
        "resolver_toolchain_identity",
        "graph_identity",
        "evidence_identity",
        "observation_identity",
        "build_input_consumption",
        "build_input_consumption_identity",
        "files",
    }
    if (
        not isinstance(manifest, Mapping)
        or set(manifest) != required
        or manifest.get("schema")
        != "urn:literate-ai:schema:v1:standard-bazel-dependency-evidence"
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-invalid",
            "Standard Bzlmod evidence manifest fields are incomplete or unknown",
        )
    expected = {
        "authorization_id": build.get("authorization_id"),
        "builder_id": build.get("builder_id"),
        "component_revision": build.get("component_revision"),
        "build_plan_identity": build.get("build_plan_identity"),
        "source_bundle_digest": observation.source_bundle_digest,
        "build_toolchain_identity": observation.build_toolchain_identity,
        "resolver_toolchain_identity": observation.resolver_toolchain_identity,
        "graph_identity": observation.graph_identity,
        "evidence_identity": observation.evidence_identity,
        "observation_identity": observation.observation_identity,
    }
    if any(manifest.get(name) != value for name, value in expected.items()):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-authority-mismatch",
            "Standard Bzlmod evidence does not bind its exact build authority",
        )
    try:
        consumption = BuildInputConsumption.from_dict(
            manifest.get("build_input_consumption")
        )
    except PortContractError as exc:
        raise DependencyObservationError(
            "dependencies.bzlmod-build-inputs-invalid",
            "Standard Bzlmod evidence has invalid build-input consumption",
        ) from exc
    if (
        consumption.consumer_id != observation.resolver_id
        or consumption.source_bundle_digest != observation.source_bundle_digest
        or manifest.get("build_input_consumption_identity") != consumption.identity
        or build.get("build_input_consumption") != consumption.to_dict()
        or build.get("build_input_consumption_identity") != consumption.identity
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-build-inputs-authority-mismatch",
            "Standard Bzlmod inputs do not bind their resolver and source",
        )

    files = manifest.get("files")
    if not isinstance(files, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"path", "digest"}
        or not isinstance(item.get("path"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("digest")))
        for item in files
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-invalid",
            "Standard Bzlmod evidence file records are malformed",
        )
    evidence_root = manifest_path.parent
    actual_records: list[dict[str, str]] = []
    contents_by_path: dict[str, bytes] = {}
    observed_entries = 0
    for path in evidence_root.rglob("*"):
        observed_entries += 1
        if observed_entries > _MAX_OBSERVED_FILES:
            raise DependencyObservationError(
                "dependencies.bzlmod-artifact-limit",
                "Standard Bzlmod evidence contains too many entries",
            )
        metadata = path.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x0400:
            raise DependencyObservationError(
                "dependencies.bzlmod-evidence-unsafe",
                "Standard Bzlmod evidence contains a link or junction",
            )
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise DependencyObservationError(
                "dependencies.bzlmod-evidence-unsafe",
                "Standard Bzlmod evidence contains a special file",
            )
        relative = path.relative_to(root).as_posix()
        if relative == _STANDARD_BAZEL_EVIDENCE_MANIFEST:
            continue
        content = _stable_file_bytes(path, limit=_MAX_TOOL_OUTPUT + 1)
        if len(content) > _MAX_TOOL_OUTPUT:
            raise DependencyObservationError(
                "dependencies.bzlmod-evidence-size-invalid",
                f"Standard Bzlmod evidence is too large: {relative}",
            )
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        actual_records.append({"path": relative, "digest": digest})
        contents_by_path[relative] = content
    actual_records.sort(key=lambda item: item["path"])
    if files != actual_records:
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-files-mismatch",
            "Standard Bzlmod manifest does not cover its exact evidence files",
        )
    evidence_contents: dict[str, bytes] = {}
    for descriptor in observation.evidence_artifacts:
        content = contents_by_path.get(descriptor.logical_name)
        if content is None or (
            "sha256:" + hashlib.sha256(content).hexdigest()
            != descriptor.content_identity
        ):
            raise DependencyObservationError(
                "dependencies.bzlmod-evidence-content-mismatch",
                f"Standard Bzlmod evidence changed: {descriptor.logical_name}",
            )
        evidence_contents[descriptor.kind] = content
    if set(evidence_contents) != set(_BZLMOD_EVIDENCE_LAYOUT):
        raise DependencyObservationError(
            "dependencies.bzlmod-evidence-missing",
            "Standard Bzlmod evidence omits the required public triplet",
        )
    _require_bzlmod_build_input_evidence(root, consumption)
    return evidence_contents


def _require_bzlmod_manifest_authority(
    manifest: object,
    *,
    build: Mapping[str, object],
    observation: BuildDependencyObservation,
) -> BuildInputConsumption:
    required = {
        "schema",
        "authorization_id",
        "builder_id",
        "delegate_artifact_digest",
        "source_bundle_digest",
        "build_toolchain_identity",
        "resolver_toolchain_identity",
        "graph_identity",
        "evidence_identity",
        "observation_identity",
        "build_input_consumption",
        "build_input_consumption_identity",
        "files",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != required:
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-invalid",
            "Bzlmod composite manifest fields are incomplete or unknown",
        )
    if manifest.get("schema") != (
        "urn:literate-ai:schema:v1:bazel-conformance-artifact"
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-invalid",
            "Bzlmod composite manifest has an unsupported schema",
        )
    expected = {
        "authorization_id": build.get("authorization_id"),
        "source_bundle_digest": observation.source_bundle_digest,
        "build_toolchain_identity": observation.build_toolchain_identity,
        "resolver_toolchain_identity": observation.resolver_toolchain_identity,
        "graph_identity": observation.graph_identity,
        "evidence_identity": observation.evidence_identity,
        "observation_identity": observation.observation_identity,
    }
    if any(manifest.get(name) != value for name, value in expected.items()):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-authority-mismatch",
            "Bzlmod composite manifest does not bind its build observation authority",
        )
    try:
        consumption = BuildInputConsumption.from_dict(
            manifest.get("build_input_consumption")
        )
    except PortContractError as exc:
        raise DependencyObservationError(
            "dependencies.bzlmod-build-inputs-invalid",
            "Bzlmod composite manifest has invalid build-input consumption",
        ) from exc
    if (
        consumption.consumer_id != observation.resolver_id
        or consumption.source_bundle_digest != observation.source_bundle_digest
        or manifest.get("build_input_consumption_identity") != consumption.identity
        or build.get("build_input_consumption") != consumption.to_dict()
        or build.get("build_input_consumption_identity") != consumption.identity
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-build-inputs-authority-mismatch",
            "Bzlmod build-input consumption does not bind its resolver and source",
        )
    authorization_id = manifest.get("authorization_id")
    builder_id = manifest.get("builder_id")
    delegate_digest = manifest.get("delegate_artifact_digest")
    if (
        not isinstance(authorization_id, str)
        or not authorization_id
        or not isinstance(builder_id, str)
        or not builder_id
        or not isinstance(delegate_digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", delegate_digest)
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-invalid",
            "Bzlmod composite manifest identifiers are invalid",
        )
    files = manifest.get("files")
    if not isinstance(files, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"path", "digest"}
        or not isinstance(item.get("path"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("digest")))
        for item in files
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-invalid",
            "Bzlmod composite manifest file records are malformed",
        )
    paths = [str(item["path"]) for item in files]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise DependencyObservationError(
            "dependencies.bzlmod-manifest-invalid",
            "Bzlmod composite manifest file records are not unique and sorted",
        )
    return consumption


def _require_bzlmod_build_input_evidence(
    root: Path, consumption: BuildInputConsumption
) -> None:
    label_evidence: list[tuple[str, str]] = []
    for evidence_name, relative_path in (
        ("build-file", _BZLMOD_BUILDFILES_PATH),
        ("target-source", _BZLMOD_SOURCE_INPUTS_PATH),
    ):
        raw_path = root.joinpath(*PurePosixPath(relative_path).parts)
        raw_content = _stable_file_bytes(raw_path, limit=_MAX_TOOL_OUTPUT + 1)
        if len(raw_content) > _MAX_TOOL_OUTPUT:
            raise DependencyObservationError(
                "dependencies.bzlmod-build-inputs-size-invalid",
                f"Bzlmod {evidence_name} labels exceed the evidence limit",
            )
        try:
            label_evidence.append((evidence_name, raw_content.decode("utf-8")))
        except UnicodeDecodeError as exc:
            raise DependencyObservationError(
                "dependencies.bzlmod-build-inputs-invalid",
                f"Bzlmod {evidence_name} labels are not UTF-8",
            ) from exc

    consumption_path = root.joinpath(
        *PurePosixPath(_BZLMOD_BUILD_INPUT_CONSUMPTION_PATH).parts
    )
    consumption_content = _stable_file_bytes(
        consumption_path, limit=_MAX_TOOL_OUTPUT + 1
    )
    if consumption_content != canonical_json_bytes(consumption.to_dict()):
        raise DependencyObservationError(
            "dependencies.bzlmod-build-inputs-content-mismatch",
            "Bzlmod canonical build-input consumption changed",
        )

    module_paths = tuple(
        path for path in consumption.files if PurePosixPath(path).name == "MODULE.bazel"
    )
    if len(module_paths) != 1:
        raise DependencyObservationError(
            "dependencies.bzlmod-build-inputs-invalid",
            "Bzlmod build-input consumption requires exactly one root MODULE.bazel",
        )
    workspace = PurePosixPath(module_paths[0]).parent
    expected = {module_paths[0]}
    for evidence_name, text in label_evidence:
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            label = raw_line.strip()
            if not label:
                continue
            if label.startswith("@"):
                continue
            if not label.startswith("//") or label.count(":") != 1:
                raise DependencyObservationError(
                    "dependencies.bzlmod-build-inputs-invalid",
                    f"Bzlmod {evidence_name} line {line_number} is not an exact label",
                )
            package, name = label[2:].split(":", 1)
            raw_relative = f"{package}/{name}" if package else name
            try:
                relative = canonical_relative_posix_path(
                    raw_relative, label=f"Bzlmod local {evidence_name} input"
                )
            except (TypeError, ValueError) as exc:
                raise DependencyObservationError(
                    "dependencies.bzlmod-build-inputs-invalid",
                    f"Bzlmod {evidence_name} line {line_number} has an unsafe path",
                ) from exc
            expected.add((workspace / relative).as_posix())
    bazelrc = (workspace / ".bazelrc").as_posix()
    if bazelrc in consumption.files:
        expected.add(bazelrc)
    if expected != set(consumption.files):
        raise DependencyObservationError(
            "dependencies.bzlmod-build-inputs-mismatch",
            "Bzlmod raw build-input labels and canonical consumption disagree",
        )


def _strict_json_document(content: bytes, *, code: str, label: str) -> object:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise DependencyObservationError(code, f"{label} is not UTF-8") from exc

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise DependencyObservationError(code, f"{label} is not strict JSON") from exc


def _require_raw_bazel_graph(
    content: bytes, *, observation: BuildDependencyObservation
) -> None:
    document = _strict_json_document(
        content,
        code="dependencies.bzlmod-graph-invalid",
        label="raw Bazel module graph",
    )
    if not isinstance(document, Mapping):
        raise DependencyObservationError(
            "dependencies.bzlmod-graph-invalid",
            "raw Bazel module graph root is not an object",
        )
    root, modules = _derive_raw_bazel_graph(document)
    if root != observation.root_module or modules != observation.modules:
        raise DependencyObservationError(
            "dependencies.bzlmod-graph-observation-mismatch",
            "raw Bazel graph does not derive the claimed module observation",
        )


def _derive_raw_bazel_graph(
    document: Mapping[str, object],
) -> tuple[BzlmodRootModule, tuple[BzlmodModule, ...]]:
    queue: list[tuple[Mapping[str, object], bool]] = [(document, True)]
    definitions: dict[str, BzlmodModule] = {}
    metadata: dict[str, tuple[str, str]] = {}
    referenced: set[str] = set()
    root_module: BzlmodRootModule | None = None
    visited = 0
    while queue:
        node, is_root = queue.pop()
        visited += 1
        if visited > _MAX_OBSERVED_FILES or set(node) - _BAZEL_GRAPH_NODE_FIELDS:
            raise DependencyObservationError(
                "dependencies.bzlmod-graph-invalid",
                "raw Bazel module graph is too large or uses unknown fields",
            )
        key = node.get("key")
        name = node.get("name")
        version = node.get("version")
        apparent_name = node.get("apparentName")
        if any(
            not isinstance(value, str) or not value
            for value in (key, name, version, apparent_name)
        ):
            raise DependencyObservationError(
                "dependencies.bzlmod-graph-invalid",
                "raw Bazel graph contains invalid module metadata",
            )
        assert isinstance(key, str) and isinstance(name, str)
        assert isinstance(version, str)
        unexpanded = node.get("unexpanded", False)
        if type(unexpanded) is not bool:
            raise DependencyObservationError(
                "dependencies.bzlmod-graph-invalid",
                "raw Bazel graph has an invalid unexpanded flag",
            )
        if is_root:
            valid_identity = key == "<root>" and node.get("root") is True
        else:
            valid_identity = key == f"{name}@{version}" and key != "<root>"
        if not valid_identity or (is_root and unexpanded):
            raise DependencyObservationError(
                "dependencies.bzlmod-graph-invalid",
                "raw Bazel graph contains an invalid module identity",
            )
        if metadata.setdefault(key, (name, version)) != (name, version):
            raise DependencyObservationError(
                "dependencies.bzlmod-graph-invalid",
                "raw Bazel graph contains conflicting module metadata",
            )
        if unexpanded:
            if set(node) != {"key", "name", "version", "apparentName", "unexpanded"}:
                raise DependencyObservationError(
                    "dependencies.bzlmod-graph-invalid",
                    "raw Bazel graph unexpanded reference has extra fields",
                )
            referenced.add(key)
            continue
        children_by_field: list[list[object]] = []
        for field in ("dependencies", "indirectDependencies", "cycles"):
            children = node.get(field)
            if not isinstance(children, list) or any(
                not isinstance(child, Mapping) for child in children
            ):
                raise DependencyObservationError(
                    "dependencies.bzlmod-graph-invalid",
                    f"raw Bazel graph has invalid {field}",
                )
            children_by_field.append(children)
        dependencies, indirect, cycles = children_by_field
        if indirect:
            raise DependencyObservationError(
                "dependencies.bzlmod-graph-invalid",
                "raw full Bazel graph contains indirect display edges",
            )
        children = [*dependencies, *cycles]
        raw_child_keys = [child.get("key") for child in children]
        if any(
            not isinstance(child_key, str) or not child_key
            for child_key in raw_child_keys
        ):
            raise DependencyObservationError(
                "dependencies.bzlmod-graph-invalid",
                "raw Bazel graph contains an invalid child key",
            )
        child_keys = tuple(sorted(set(raw_child_keys)))
        referenced.update(child_keys)
        queue.extend((child, False) for child in children)
        if is_root:
            root_module = BzlmodRootModule(name, version, child_keys)
        else:
            module = BzlmodModule(key, name, version, child_keys)
            prior = definitions.get(key)
            if prior is not None and prior != module:
                raise DependencyObservationError(
                    "dependencies.bzlmod-graph-invalid",
                    "raw Bazel graph contains conflicting module definitions",
                )
            definitions[key] = module
    if root_module is None or referenced - set(definitions):
        raise DependencyObservationError(
            "dependencies.bzlmod-graph-incomplete",
            "raw Bazel graph has unresolved module references",
        )
    return root_module, tuple(definitions[key] for key in sorted(definitions))


def _require_raw_bazel_lock(content: bytes, *, modules: Sequence[BzlmodModule]) -> None:
    document = _strict_json_document(
        content,
        code="dependencies.bzlmod-lock-invalid",
        label="raw Bazel module lock",
    )
    required = {
        "lockFileVersion",
        "registryFileHashes",
        "selectedYankedVersions",
        "moduleExtensions",
        "facts",
        "factsVersions",
    }
    if (
        not isinstance(document, Mapping)
        or set(document) != required
        or type(document.get("lockFileVersion")) is not int
        or document["lockFileVersion"] <= 0
        or not isinstance(document.get("registryFileHashes"), Mapping)
        or not isinstance(document.get("selectedYankedVersions"), Mapping)
        or not isinstance(document.get("moduleExtensions"), Mapping)
        or not isinstance(document.get("facts"), Mapping)
        or not isinstance(document.get("factsVersions"), Mapping)
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-lock-invalid",
            "raw Bazel lock lacks its supported public fields",
        )
    hashes = document["registryFileHashes"]
    assert isinstance(hashes, Mapping)
    if any(
        not isinstance(path, str)
        or not isinstance(digest, str)
        or (digest != "not found" and not re.fullmatch(r"[0-9a-f]{64}", digest))
        for path, digest in hashes.items()
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-lock-invalid",
            "raw Bazel lock contains invalid registry hashes",
        )
    yanked = document["selectedYankedVersions"]
    assert isinstance(yanked, Mapping)
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in yanked.items()
    ):
        raise DependencyObservationError(
            "dependencies.bzlmod-lock-invalid",
            "raw Bazel lock contains invalid selected yanked versions",
        )
    for module in modules:
        marker = f"/modules/{module.name}/{module.version}/"
        module_hashes = {
            path: digest
            for path, digest in hashes.items()
            if marker in path and digest != "not found"
        }
        if not any(path.endswith("/MODULE.bazel") for path in module_hashes) or not any(
            path.endswith("/source.json") for path in module_hashes
        ):
            raise DependencyObservationError(
                "dependencies.bzlmod-lock-module-missing",
                f"raw Bazel lock lacks immutable registry inputs for {module.key}",
            )


def _require_raw_bazel_repositories(
    content: bytes, *, modules: Sequence[BzlmodModule]
) -> None:
    if not content and not modules:
        return
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise DependencyObservationError(
            "dependencies.bzlmod-repositories-invalid",
            "raw Bazel repository evidence is not UTF-8",
        ) from exc
    if not text.endswith("\n"):
        raise DependencyObservationError(
            "dependencies.bzlmod-repositories-invalid",
            "raw Bazel repository evidence must end at a complete NDJSON record",
        )
    records_by_name: dict[str, Mapping[str, object]] = {}
    canonical_names: set[str] = set()
    lines = text.splitlines()
    if not lines or len(lines) > _MAX_OBSERVED_FILES:
        raise DependencyObservationError(
            "dependencies.bzlmod-repositories-invalid",
            "raw Bazel repository evidence is empty or too large",
        )
    for index, line in enumerate(lines, start=1):
        if not line:
            raise DependencyObservationError(
                "dependencies.bzlmod-repositories-invalid",
                "raw Bazel repository evidence contains an empty record",
            )
        record = _strict_json_document(
            line.encode("utf-8"),
            code="dependencies.bzlmod-repositories-invalid",
            label=f"raw Bazel repository record {index}",
        )
        if not isinstance(record, Mapping) or set(record) - _BAZEL_REPOSITORY_FIELDS:
            raise DependencyObservationError(
                "dependencies.bzlmod-repositories-invalid",
                "raw Bazel repository record has unsupported fields",
            )
        canonical_name = record.get("canonicalName")
        if (
            not isinstance(canonical_name, str)
            or not canonical_name
            or canonical_name in canonical_names
        ):
            raise DependencyObservationError(
                "dependencies.bzlmod-repositories-invalid",
                "raw Bazel repository record has a duplicate or invalid name",
            )
        canonical_names.add(canonical_name)
        records_by_name[canonical_name] = record
    for module in modules:
        canonical_name = (
            module.name if module.name == "platforms" else (f"{module.name}+")
        )
        record = records_by_name.get(canonical_name)
        if record is None or not _repository_record_is_immutable(record, module=module):
            raise DependencyObservationError(
                "dependencies.bzlmod-repository-module-missing",
                "raw Bazel repository evidence lacks one immutable source for "
                f"{module.key}",
            )


def _repository_record_is_immutable(
    record: Mapping[str, object], *, module: BzlmodModule
) -> bool:
    rule = record.get("repoRuleName")
    label = record.get("repoRuleBzlLabel")
    attributes = record.get("attribute")
    if (
        not isinstance(rule, str)
        or not rule
        or not isinstance(label, str)
        or not label
        or not isinstance(attributes, list)
    ):
        return False
    by_name: dict[str, Mapping[str, object]] = {}
    for item in attributes:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("type"), str)
            or item["name"] in by_name
        ):
            return False
        by_name[str(item["name"])] = item

    def string(name: str) -> str:
        value = by_name.get(name, {}).get("stringValue")
        return value if isinstance(value, str) else ""

    def strings(name: str) -> tuple[str, ...]:
        value = by_name.get(name, {}).get("stringListValue")
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            return ()
        return tuple(value)

    if rule == "http_archive":
        urls = (*strings("urls"), string("url"))
        sources = tuple(value for value in urls if value)
        module_urls = strings("remote_module_file_urls")
        expected_module_url = (
            f"https://bcr.bazel.build/modules/{module.name}/{module.version}/"
            "MODULE.bazel"
        )
        integrity = string("integrity")
        sha256 = string("sha256")
        module_integrity = string("remote_module_file_integrity")
        return (
            bool(sources)
            and all(source.startswith(("https://", "http://")) for source in sources)
            and expected_module_url in module_urls
            and bool(
                re.fullmatch(r"sha256-[A-Za-z0-9+/]+={0,2}", integrity)
                or re.fullmatch(r"[0-9a-f]{64}", sha256)
            )
            and bool(re.fullmatch(r"sha256-[A-Za-z0-9+/]+={0,2}", module_integrity))
        )
    return False


def _resolved_source_projection(
    source_content: bytes,
    *,
    managed_graph: CycloneDxManagedGraph,
    resolution_candidates: Sequence[Mapping[str, object]],
    lock_projection: _LockProjection,
    authoritative_bzlmod_versions: Mapping[str, str] | None = None,
    require_package_dependency_kind: bool = False,
) -> tuple[tuple[dict[str, object], ...], tuple[tuple[str, str], ...]]:
    """Preserve source inventory/edges and replace ranges with exact evidence."""

    document = json.loads(source_content)
    if not isinstance(document, Mapping):
        raise DependencyObservationError(
            "dependencies.source-bom-invalid", "source BOM root is invalid"
        )
    raw_components = document.get("components", [])
    raw_dependencies = document.get("dependencies", [])
    if not isinstance(raw_components, list) or not isinstance(raw_dependencies, list):
        raise DependencyObservationError(
            "dependencies.source-bom-invalid",
            "source BOM inventory or dependency graph is invalid",
        )
    managed_refs = {item.bom_ref for item in managed_graph.components}
    candidate_by_ref: dict[str, dict[str, object]] = {}
    for raw in resolution_candidates:
        candidate = dict(raw)
        ref = candidate.get("bom-ref")
        if not isinstance(ref, str) or not ref:
            raise DependencyObservationError(
                "dependencies.resolution-candidate-invalid",
                "resolved dependency observation lacks a BOM reference",
            )
        existing = candidate_by_ref.get(ref)
        if existing is not None and existing != candidate:
            raise DependencyObservationError(
                "dependencies.resolution-candidate-conflict",
                f"resolved dependency observations conflict for {ref}",
            )
        candidate_by_ref[ref] = candidate

    projected: list[dict[str, object]] = []
    consumed_candidates: set[str] = set()
    package_ref_by_lock_key: dict[str, str] = {}
    for raw in raw_components:
        if not isinstance(raw, Mapping):
            raise DependencyObservationError(
                "dependencies.source-bom-invalid",
                "source BOM contains an invalid component",
            )
        source = dict(raw)
        ref = source.get("bom-ref")
        if not isinstance(ref, str) or not ref:
            raise DependencyObservationError(
                "dependencies.source-bom-invalid",
                "source BOM component lacks a BOM reference",
            )
        if ref in managed_refs:
            continue
        candidate = candidate_by_ref.get(ref)
        locked_package = _matching_locked_package(source, lock_projection)
        if (
            require_package_dependency_kind
            and candidate is None
            and locked_package is None
            and _dependency_property_values(source).get("literate-ai:dependency-kind")
            != ("package",)
        ):
            # A component that is not an ordinary package dependency (for
            # example toolchain/runtime compatibility metadata such as a
            # package.json `engines.node` constraint) never enumerates a
            # package-manager lock coordinate, so it is never required to
            # resolve against the exact npm lock graph here. Toolchain
            # compatibility remains enforced by target/toolchain selection.
            projected.append(dict(source))
            continue
        exact_version = _source_component_exact_version(
            source,
            candidate=candidate,
            locked_package=locked_package,
            authoritative_bzlmod_version=(authoritative_bzlmod_versions or {}).get(ref),
        )
        resolved = dict(source)
        if "versionRange" in resolved:
            resolved.pop("versionRange")
            resolved["version"] = exact_version
        elif candidate is not None and candidate.get("version") != exact_version:
            raise DependencyObservationError(
                "dependencies.source-exact-version-conflict",
                f"resolved observation conflicts with source exact version for {ref}",
            )
        if candidate is not None:
            consumed_candidates.add(ref)
            _merge_resolution_evidence(resolved, candidate)
        if locked_package is not None:
            package_ref_by_lock_key[locked_package.key] = ref
            resolved.setdefault(
                "hashes",
                [
                    {
                        "alg": locked_package.integrity_algorithm,
                        "content": locked_package.integrity_digest,
                    }
                ],
            )
        projected.append(resolved)

    for ref, candidate in sorted(candidate_by_ref.items()):
        if ref not in consumed_candidates and ref not in managed_refs:
            projected.append(candidate)

    for package in lock_projection.packages:
        if package.root or package.key in package_ref_by_lock_key:
            continue
        if package.ecosystem != "cargo":
            continue
        ref = f"pkg:cargo/{quote(package.coordinate, safe='')}@{package.version}"
        if ref in managed_refs or any(item.get("bom-ref") == ref for item in projected):
            raise DependencyObservationError(
                "dependencies.lock-resolution-ambiguous",
                f"derived Cargo package has a conflicting BOM reference: {ref}",
            )
        package_ref_by_lock_key[package.key] = ref
        projected.append(
            {
                "type": "library",
                "bom-ref": ref,
                "name": package.coordinate,
                "version": package.version,
                "purl": ref,
                "hashes": [
                    {
                        "alg": package.integrity_algorithm,
                        "content": package.integrity_digest,
                    }
                ],
                "properties": [
                    {"name": "literate-ai:dependency-kind", "value": "runtime"},
                    {"name": "literate-ai:dependency-scope", "value": "runtime"},
                ],
            }
        )

    edges: set[tuple[str, str]] = set()
    for raw in raw_dependencies:
        if not isinstance(raw, Mapping):
            raise DependencyObservationError(
                "dependencies.source-bom-invalid",
                "source BOM contains an invalid dependency entry",
            )
        source = raw.get("ref")
        targets = raw.get("dependsOn")
        if not isinstance(source, str) or not isinstance(targets, list):
            raise DependencyObservationError(
                "dependencies.source-bom-invalid",
                "source BOM contains an invalid dependency entry",
            )
        for target in targets:
            if not isinstance(target, str):
                raise DependencyObservationError(
                    "dependencies.source-bom-invalid",
                    "source BOM dependency target is invalid",
                )
            edges.add((source, target))
    locked_packages_by_key = {
        package.key: package for package in lock_projection.packages
    }
    for source_key, target_key in lock_projection.edges:
        source_package = locked_packages_by_key[source_key]
        target_package = locked_packages_by_key[target_key]
        source_ref = (
            managed_graph.root_ref
            if source_package.root
            else package_ref_by_lock_key.get(source_key)
        )
        target_ref = (
            managed_graph.root_ref
            if target_package.root
            else package_ref_by_lock_key.get(target_key)
        )
        if (
            source_ref is not None
            and target_ref is not None
            and source_package.ecosystem == "cargo"
            and target_package.ecosystem == "cargo"
        ):
            edges.add((source_ref, target_ref))
    _require_lock_graph_covered(
        lock_projection,
        package_ref_by_lock_key=package_ref_by_lock_key,
        root_ref=managed_graph.root_ref,
        source_edges=edges,
    )
    return tuple(projected), tuple(sorted(edges))


def _source_component_exact_version(
    source: Mapping[str, object],
    *,
    candidate: Mapping[str, object] | None,
    locked_package: _LockedPackage | None,
    authoritative_bzlmod_version: str | None = None,
) -> str:
    source_version = source.get("version")
    if isinstance(source_version, str) and source_version:
        return source_version
    if "versionRange" not in source:
        raise DependencyObservationError(
            "dependencies.source-version-invalid",
            "source dependency has neither an exact version nor a version range",
        )
    candidate_version = candidate.get("version") if candidate is not None else None
    if candidate_version is not None and (
        not isinstance(candidate_version, str) or not candidate_version
    ):
        raise DependencyObservationError(
            "dependencies.resolution-version-invalid",
            "resolved dependency observation lacks an exact version",
        )
    name = source.get("name")
    locked_version = locked_package.version if locked_package is not None else None
    if (
        isinstance(candidate_version, str)
        and locked_version is not None
        and candidate_version != locked_version
    ):
        raise DependencyObservationError(
            "dependencies.resolution-version-conflict",
            f"lock and observed versions conflict for source dependency {name!r}",
        )
    exact = candidate_version or locked_version
    if not isinstance(exact, str) or not exact:
        raise DependencyObservationError(
            "dependencies.source-range-unresolved",
            f"source dependency {name!r} has no exact lock or observation",
        )
    if authoritative_bzlmod_version is not None:
        properties = _dependency_property_values(source)
        if (
            exact != authoritative_bzlmod_version
            or properties.get("literate-ai:bzlmod-requested-version") is None
        ):
            raise DependencyObservationError(
                "dependencies.bzlmod-authoritative-selection-mismatch",
                "authoritative raw Bzlmod selection does not match its source intent",
            )
        return exact
    _require_version_in_range(source.get("versionRange"), exact)
    return exact


def _require_version_in_range(raw_range: object, version: str) -> None:
    if not isinstance(raw_range, str) or not raw_range:
        raise DependencyObservationError(
            "dependencies.source-range-invalid",
            "source dependency versionRange is invalid",
        )
    scheme = raw_range.partition("/")[0].removeprefix("vers:")
    if scheme == "cargo" and "|" not in raw_range and "," in raw_range:
        raw_range = raw_range.replace(",", "|")
    if scheme not in {"cargo", "generic", "npm", "pypi"}:
        raise DependencyObservationError(
            "dependencies.source-range-unsupported",
            f"source dependency uses unsupported VERS scheme {scheme!r}",
        )
    try:
        version_range = VersionRange.from_string(raw_range)
        parsed_version = version_range.version_class(version)
        matches = parsed_version in version_range
    except Exception as exc:
        raise DependencyObservationError(
            "dependencies.source-range-unsupported",
            "source dependency VERS expression cannot be soundly evaluated",
        ) from exc
    if not matches:
        raise DependencyObservationError(
            "dependencies.source-range-mismatch",
            f"resolved version {version!r} is outside source range {raw_range!r}",
        )


def _merge_resolution_evidence(
    target: dict[str, object], candidate: Mapping[str, object]
) -> None:
    for key in ("type", "name"):
        if key in candidate and candidate[key] != target.get(key):
            raise DependencyObservationError(
                "dependencies.resolution-candidate-mismatch",
                "resolved observation names another source dependency",
            )
    source_properties = target.get("properties", [])
    candidate_properties = candidate.get("properties", [])
    if isinstance(source_properties, list) and isinstance(candidate_properties, list):
        additions = [
            item for item in candidate_properties if item not in source_properties
        ]
        if additions:
            target["properties"] = [*source_properties, *additions]
    for key, value in candidate.items():
        if key in {"bom-ref", "name", "properties", "type", "version", "versionRange"}:
            continue
        target.setdefault(key, value)
