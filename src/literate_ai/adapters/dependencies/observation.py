"""Portable host closure observation and inspector-tool parsers."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import semantic_version
from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    name_or_magic_is_native_or_wasm,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.linux_loader_paths import LinuxLoaderPaths
from literate_ai.adapters.macos_loader_paths import MacOsLoaderPaths
from literate_ai.adapters.user_paths import resolve_host_system_paths
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

from .javascript_imports import (
    NODE_BUILTIN_MODULES,
    has_computed_dynamic_import,
    javascript_dynamic_import_specifiers,
    javascript_esm_import_specifiers,
    javascript_import_specifiers,
    javascript_package_name,
)
from .types import (
    DependencyObservationError,
    HostDependencyObservation,
    _normalized_package_name,
)

_MACHO_MAGICS = {
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}

_UUID = re.compile(r"^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$")

_MAX_OBSERVED_FILES = 100_000

_MAX_TOOL_OUTPUT = 16 * 1024 * 1024

_MAX_NPM_CMD_SHIM_BYTES = 64 * 1024

_MAX_NPM_CMD_SHIM_LINES = 64

_MAX_NPM_CMD_SHIM_LINE_BYTES = 4 * 1024

_MAX_NPM_OWNERSHIP_MANIFEST_BYTES = 1024 * 1024

_MAX_NPM_GRAPH_PACKAGES = 4_096

_MAX_NPM_GRAPH_FILES = 200_000

_MAX_NPM_GRAPH_BYTES = 2 * 1024 * 1024 * 1024

_MAX_NPM_GRAPH_MANIFEST_BYTES = 64 * 1024 * 1024

_MAX_NPM_FILE_BYTES = 256 * 1024 * 1024

_NPM_PACKAGE_NAME = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$", re.IGNORECASE
)

_WINDOWS_NPM_CMD_SHIM_PREFIX = (
    "@ECHO off",
    "GOTO start",
    ":find_dp0",
    "SET dp0=%~dp0",
    "EXIT /b",
    ":start",
    "SETLOCAL",
    "CALL :find_dp0",
    "",
    'IF EXIST "%dp0%\\node.exe" (',
    '  SET "_prog=%dp0%\\node.exe"',
    ") ELSE (",
    '  SET "_prog=node"',
    "  SET PATHEXT=%PATHEXT:;.JS;=;%",
    ")",
    "",
)

_WINDOWS_NPM_CMD_SHIM_DISPATCH = re.compile(
    r'^endLocal & goto #_undefined_# 2>NUL \|\| title %COMSPEC% & "%_prog%"  '
    r'"(?P<target>%dp0%\\[^"]+)" %\*$'
)

_WINDOWS_NPM_CMD_SHIM_SEGMENT = re.compile(r"^[A-Za-z0-9@._~-]+$")


def observe_installed_python_distributions(
    requirements: Sequence[str], *, root_ref: str
) -> HostDependencyObservation:
    """Resolve exact installed Python distributions, files, and parent edges.

    This is intentionally an installed-environment observation rather than a package
    index query.  It is usable before a build to populate a SOURCE BOM and again at
    resolution time to prove that the exact package closure did not drift.
    """

    environment = default_environment()
    pending: deque[tuple[str, Requirement]] = deque()
    direct_requirements: dict[str, set[str]] = {}
    for raw in requirements:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            raise DependencyObservationError(
                "dependencies.python-requirement-invalid",
                f"Python dependency requirement is invalid: {raw!r}",
            ) from exc
        if requirement.marker is not None and not requirement.marker.evaluate(
            {**environment, "extra": ""}
        ):
            continue
        direct_requirements.setdefault(
            _normalized_package_name(requirement.name), set()
        ).add(str(requirement))
        pending.append((root_ref, requirement))

    components: dict[str, dict[str, object]] = {}
    edges: set[tuple[str, str]] = set()
    activated_extras: dict[str, set[str]] = {}
    processed_states: set[tuple[str, tuple[str, ...]]] = set()
    distributions: dict[str, importlib.metadata.Distribution] = {}
    while pending:
        parent_ref, requirement = pending.popleft()
        coordinate = _normalized_package_name(requirement.name)
        try:
            distribution = distributions.setdefault(
                coordinate, importlib.metadata.distribution(requirement.name)
            )
        except importlib.metadata.PackageNotFoundError as exc:
            raise DependencyObservationError(
                "dependencies.python-distribution-missing",
                f"required Python distribution {requirement.name!r} is not installed",
            ) from exc
        if requirement.specifier and not requirement.specifier.contains(
            distribution.version, prereleases=True
        ):
            raise DependencyObservationError(
                "dependencies.python-distribution-version-mismatch",
                f"installed Python distribution {requirement.name!r} "
                f"at {distribution.version} does not satisfy {requirement.specifier}",
            )
        metadata_name = distribution.metadata.get("Name")
        if not isinstance(metadata_name, str) or not metadata_name.strip():
            raise DependencyObservationError(
                "dependencies.python-distribution-name-missing",
                "installed Python distribution metadata lacks its package name",
            )
        canonical_name = _normalized_package_name(metadata_name)
        if canonical_name != coordinate:
            raise DependencyObservationError(
                "dependencies.python-distribution-name-mismatch",
                "installed Python distribution metadata names another package",
            )
        ref = f"pkg:pypi/{canonical_name}"
        edges.add((parent_ref, ref))
        component = _python_distribution_component(distribution, ref=ref)
        existing = components.get(ref)
        if existing is not None and existing != component:
            raise DependencyObservationError(
                "dependencies.python-distribution-drift",
                f"Python distribution {canonical_name!r} changed during observation",
            )
        components[ref] = component

        extras = activated_extras.setdefault(canonical_name, set())
        extras.update(str(item) for item in requirement.extras)
        state = (canonical_name, tuple(sorted(extras)))
        if state in processed_states:
            continue
        processed_states.add(state)
        marker_extras = tuple(sorted(extras)) or ("",)
        for raw_child in distribution.requires or ():
            try:
                child = Requirement(raw_child)
            except InvalidRequirement as exc:
                raise DependencyObservationError(
                    "dependencies.python-metadata-invalid",
                    f"installed Python distribution {canonical_name!r} has an "
                    "invalid dependency requirement",
                ) from exc
            if child.marker is not None and not any(
                child.marker.evaluate({**environment, "extra": extra})
                for extra in marker_extras
            ):
                continue
            pending.append((ref, child))

    for coordinate, declared in direct_requirements.items():
        ref = f"pkg:pypi/{coordinate}"
        component = components.get(ref)
        if component is None:
            raise DependencyObservationError(
                "dependencies.python-direct-distribution-missing",
                "declared Python dependency is absent from the installed closure",
            )
        properties = component.get("properties")
        if not isinstance(properties, list):
            raise DependencyObservationError(
                "dependencies.python-distribution-evidence-invalid",
                "installed Python dependency lacks typed evidence properties",
            )
        component["properties"] = [
            *properties,
            *(
                {
                    "name": "literate-ai:python-declared-requirement",
                    "value": requirement,
                }
                for requirement in sorted(declared)
            ),
        ]

    return _normalized_observation(tuple(components.values()), tuple(edges))


def declare_optional_python_distributions(
    requirements_by_group: Mapping[str, Sequence[str]],
    *,
    imported_names: Sequence[str],
    root_ref: str,
    import_names_by_distribution: Mapping[str, Sequence[str]] | None = None,
) -> HostDependencyObservation:
    """Describe exact optional distributions imported by a source tree.

    Optional imports belong in SOURCE authority even when the corresponding extra is
    not installed in the core verification environment. Only exact requirements can
    make that claim without querying an index or inventing a resolved version.
    """

    environment = default_environment()
    imported = {_normalized_package_name(item) for item in imported_names}
    declared_aliases: dict[str, set[str]] = {}
    for raw_coordinate, raw_aliases in (import_names_by_distribution or {}).items():
        if (
            not isinstance(raw_coordinate, str)
            or not raw_coordinate.strip()
            or isinstance(raw_aliases, (str, bytes))
            or any(
                not isinstance(item, str) or not item.strip() for item in raw_aliases
            )
        ):
            raise DependencyObservationError(
                "dependencies.python-optional-import-alias-invalid",
                "optional Python distribution import aliases are invalid",
            )
        declared_aliases[_normalized_package_name(raw_coordinate)] = {
            _normalized_package_name(item) for item in raw_aliases
        }
    selected: dict[str, dict[str, object]] = {}
    declarations: dict[str, set[tuple[str, str]]] = {}
    import_aliases: dict[str, set[str]] = {}
    installed_aliases: dict[str, tuple[importlib.metadata.Distribution, set[str]]] = {}
    for group, raw_requirements in sorted(requirements_by_group.items()):
        if not isinstance(group, str) or not group.strip():
            raise DependencyObservationError(
                "dependencies.python-optional-group-invalid",
                "Python optional dependency group name is invalid",
            )
        if isinstance(raw_requirements, (str, bytes)):
            raise DependencyObservationError(
                "dependencies.python-optional-group-invalid",
                f"Python optional dependency group {group!r} is not a list",
            )
        for raw in raw_requirements:
            if not isinstance(raw, str) or not raw.strip():
                raise DependencyObservationError(
                    "dependencies.python-optional-requirement-invalid",
                    f"Python optional dependency group {group!r} contains an "
                    "invalid requirement",
                )
            try:
                requirement = Requirement(raw)
            except InvalidRequirement as exc:
                raise DependencyObservationError(
                    "dependencies.python-optional-requirement-invalid",
                    f"Python optional dependency requirement is invalid: {raw!r}",
                ) from exc
            if requirement.marker is not None and not requirement.marker.evaluate(
                {**environment, "extra": group}
            ):
                continue
            coordinate = _normalized_package_name(requirement.name)
            distribution: importlib.metadata.Distribution | None = None
            matched_imports = (
                {coordinate} | declared_aliases.get(coordinate, set())
            ).intersection(imported)
            if not matched_imports:
                installed = installed_aliases.get(coordinate)
                if installed is None:
                    try:
                        distribution = importlib.metadata.distribution(requirement.name)
                    except importlib.metadata.PackageNotFoundError:
                        distribution = None
                    aliases = (
                        set()
                        if distribution is None
                        else _installed_python_import_aliases(distribution, imported)
                    )
                    if distribution is not None:
                        installed_aliases[coordinate] = (distribution, aliases)
                else:
                    distribution, aliases = installed
                if distribution is not None:
                    metadata_name = distribution.metadata.get("Name")
                    if (
                        not isinstance(metadata_name, str)
                        or _normalized_package_name(metadata_name) != coordinate
                    ):
                        raise DependencyObservationError(
                            "dependencies.python-distribution-name-mismatch",
                            "installed optional Python distribution metadata names "
                            "another package",
                        )
                    matched_imports.update(
                        _normalized_package_name(item) for item in aliases
                    )
                    matched_imports.intersection_update(imported)
            if not matched_imports:
                continue
            specifiers = tuple(requirement.specifier)
            if (
                len(specifiers) != 1
                or specifiers[0].operator != "=="
                or "*" in specifiers[0].version
            ):
                raise DependencyObservationError(
                    "dependencies.python-optional-requirement-unpinned",
                    f"imported optional Python distribution {requirement.name!r} "
                    "must have one exact == version",
                )
            version = specifiers[0].version
            if distribution is not None and distribution.version != version:
                raise DependencyObservationError(
                    "dependencies.python-distribution-version-mismatch",
                    f"installed optional Python distribution {requirement.name!r} "
                    f"at {distribution.version} does not satisfy "
                    f"{requirement.specifier}",
                )
            ref = f"pkg:pypi/{coordinate}"
            existing = selected.get(ref)
            if existing is not None and existing["version"] != version:
                raise DependencyObservationError(
                    "dependencies.python-optional-requirement-conflict",
                    f"optional Python distribution {requirement.name!r} has "
                    "conflicting exact versions",
                )
            selected.setdefault(
                ref,
                {
                    "type": "library",
                    "bom-ref": ref,
                    "name": coordinate,
                    "version": version,
                    "purl": f"pkg:pypi/{coordinate}@{version}",
                    "isExternal": True,
                    "scope": "optional",
                },
            )
            declarations.setdefault(ref, set()).add((group, str(requirement)))
            import_aliases.setdefault(ref, set()).update(matched_imports)

    components: list[dict[str, object]] = []
    for ref, component in sorted(selected.items()):
        records = sorted(declarations[ref])
        component["properties"] = [
            {"name": "literate-ai:dependency-kind", "value": "package"},
            {"name": "literate-ai:dependency-scope", "value": "runtime"},
            *(
                {
                    "name": "literate-ai:python-declared-requirement",
                    "value": requirement,
                }
                for _group, requirement in records
            ),
            *(
                {
                    "name": "literate-ai:python-optional-requirement",
                    "value": canonical_json_bytes(
                        {"group": group, "requirement": requirement}
                    ).decode("utf-8"),
                }
                for group, requirement in records
            ),
            *(
                {
                    "name": "literate-ai:python-top-level-import",
                    "value": alias,
                }
                for alias in sorted(import_aliases[ref])
            ),
        ]
        components.append(component)
    return _normalized_observation(
        components, tuple((root_ref, str(item["bom-ref"])) for item in components)
    )


def installed_python_distribution_payload(
    distribution: importlib.metadata.Distribution,
) -> tuple[tuple[PurePosixPath, Path], ...]:
    """Return the exact installed package payload, excluding derived bytecode caches.

    ``pip`` can add interpreter-generated ``.pyc`` entries to ``RECORD``.  When
    ``PYTHONPYCACHEPREFIX`` is outside the environment those entries legitimately
    traverse beyond ``site-packages`` and are not portable installation payload.
    Source files and ``RECORD`` remain authoritative, so bytecode is regenerated by
    the selected interpreter.  Every other recorded path remains exact and bounded by
    the projection consumer; an unrelated escaping path is never silently admitted.
    """

    metadata_name = distribution.metadata.get("Name")
    if not isinstance(metadata_name, str) or not metadata_name.strip():
        raise DependencyObservationError(
            "dependencies.python-distribution-name-missing",
            "installed Python distribution metadata lacks its package name",
        )
    name = _normalized_package_name(metadata_name)
    recorded = tuple(
        (raw_entry, PurePosixPath(str(raw_entry)))
        for raw_entry in distribution.files or ()
    )
    source_entries = tuple(
        entry for _raw_entry, entry in recorded if entry.suffix.casefold() == ".py"
    )
    paths: list[tuple[PurePosixPath, Path]] = []
    for raw_entry, entry in recorded:
        if _is_derived_python_bytecode(entry, source_entries):
            continue
        if entry.is_absolute() or not entry.parts:
            raise DependencyObservationError(
                "dependencies.python-distribution-path-invalid",
                f"installed Python distribution {name!r} contains an invalid path",
            )
        path = Path(distribution.locate_file(raw_entry))
        if path.is_symlink() or not path.is_file():
            raise DependencyObservationError(
                "dependencies.python-distribution-file-unsafe",
                f"installed Python distribution {name!r} contains an unsafe file",
            )
        paths.append((entry, path.resolve(strict=True)))
    if not paths or len(paths) > _MAX_OBSERVED_FILES:
        raise DependencyObservationError(
            "dependencies.python-distribution-files-invalid",
            f"installed Python distribution {name!r} has no bounded file inventory",
        )
    return tuple(sorted(paths, key=lambda item: item[0].as_posix()))


def _installed_python_import_aliases(
    distribution: importlib.metadata.Distribution, imported: set[str]
) -> set[str]:
    """Derive only relevant aliases from recorded, present distribution files."""

    aliases: set[str] = set()
    for raw_entry in distribution.files or ():
        entry = PurePosixPath(str(raw_entry))
        if entry.is_absolute() or not entry.parts:
            raise DependencyObservationError(
                "dependencies.python-distribution-path-invalid",
                "installed optional Python distribution contains an invalid path",
            )
        if entry.suffix not in {".py", ".so", ".pyd"}:
            continue
        candidate = (
            entry.parts[0] if len(entry.parts) > 1 else entry.name.split(".", 1)[0]
        )
        normalized = _normalized_package_name(candidate)
        if not candidate.isidentifier() or normalized not in imported:
            continue
        path = Path(distribution.locate_file(raw_entry))
        if path.is_symlink() or not path.is_file():
            raise DependencyObservationError(
                "dependencies.python-distribution-file-unsafe",
                "installed optional Python distribution contains an unsafe file",
            )
        aliases.add(normalized)
    return aliases


def _is_derived_python_bytecode(
    entry: PurePosixPath, source_entries: Sequence[PurePosixPath]
) -> bool:
    """Recognize cache bytecode only when its corresponding source is recorded."""

    if entry.suffix.casefold() not in {".pyc", ".pyo"}:
        return False
    filename = entry.name
    tagged = re.fullmatch(
        r"(?P<stem>.+)\.[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*"
        r"(?:\.opt-[0-9]+)?\.py[co]",
        filename,
    )
    source_name = f"{tagged.group('stem')}.py" if tagged is not None else filename[:-1]
    bytecode_parts = tuple(
        part for part in (*entry.parts[:-1], source_name) if part != "__pycache__"
    )
    return any(
        len(source.parts) <= len(bytecode_parts)
        and bytecode_parts[-len(source.parts) :] == source.parts
        for source in source_entries
    )


def _python_import_names_from_payload(
    paths: Sequence[tuple[PurePosixPath, Path]],
) -> set[str]:
    import_names: set[str] = set()
    for relative, _path in paths:
        if relative.suffix not in {".py", ".so", ".pyd"}:
            continue
        candidate = (
            relative.parts[0]
            if len(relative.parts) > 1
            else relative.name.split(".", 1)[0]
        )
        if candidate.isidentifier():
            import_names.add(candidate)
    return import_names


def _python_distribution_component(
    distribution: importlib.metadata.Distribution, *, ref: str
) -> dict[str, object]:
    metadata_name = distribution.metadata.get("Name")
    if not isinstance(metadata_name, str) or not metadata_name.strip():
        raise DependencyObservationError(
            "dependencies.python-distribution-name-missing",
            "installed Python distribution metadata lacks its package name",
        )
    name = _normalized_package_name(metadata_name)
    paths = installed_python_distribution_payload(distribution)
    files = [(relative.as_posix(), _file_digest(path)) for relative, path in paths]
    # Derive import aliases from the exact captured payload, never from ambient
    # import lookup or unchecked top_level.txt claims. Namespace packages count;
    # metadata, data-only directories and type stubs do not establish imports.
    import_names = _python_import_names_from_payload(paths)
    tree_identity = canonical_identity(
        {"name": name, "version": distribution.version, "files": files}
    )
    for (relative, path), (recorded_relative, digest) in zip(paths, files, strict=True):
        if relative.as_posix() != recorded_relative or _file_digest(path) != digest:
            raise DependencyObservationError(
                "dependencies.python-distribution-changed",
                f"installed Python distribution {name!r} changed during observation",
            )
    return {
        "type": "library",
        "bom-ref": ref,
        "name": name,
        "version": distribution.version,
        "purl": f"pkg:pypi/{name}@{distribution.version}",
        "hashes": [{"alg": "SHA-256", "content": tree_identity.digest}],
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "package"},
            {"name": "literate-ai:dependency-scope", "value": "runtime"},
            {
                "name": "literate-ai:python-installed-tree-identity",
                "value": tree_identity.uri,
            },
            *(
                {"name": "literate-ai:python-top-level-import", "value": alias}
                for alias in sorted(import_names)
            ),
        ],
    }


@dataclass(frozen=True, slots=True)
class _MachOMaterializedFile:
    resolved_path: str
    content_identity: str
    symlink_chain: tuple[tuple[str, str, tuple[int, int, int, int, int]], ...] = ()


@dataclass(frozen=True, slots=True)
class _MachOImage:
    path: str
    uuids: tuple[tuple[str, str], ...]
    linked_paths: tuple[tuple[str, bool], ...]
    rpaths: tuple[str, ...]
    materialized_file: _MachOMaterializedFile | None = None


class PortableHostDependencyObserver:
    """Select a complete host-native observer or fail closed."""

    def __init__(
        self,
        *,
        toolchain_commands: Sequence[Sequence[str]] = (),
        lifecycle_commands: Sequence[str] = (),
        python_requirements: Sequence[str] = (),
        library_roots: Sequence[Path] = (),
        windows_environment: Mapping[str, str] | None = None,
        macos_loader_paths: MacOsLoaderPaths | None = None,
        linux_loader_paths: LinuxLoaderPaths | None = None,
        artifact_files: Sequence[Path] | None = None,
    ) -> None:
        self.toolchain_commands = tuple(tuple(item) for item in toolchain_commands)
        self.lifecycle_commands = tuple(lifecycle_commands)
        self.python_requirements = tuple(python_requirements)
        self.library_roots = _explicit_library_roots(library_roots)
        self.windows_environment = _windows_environment_snapshot(windows_environment)
        if macos_loader_paths is not None and not isinstance(
            macos_loader_paths, MacOsLoaderPaths
        ):
            raise ValueError("macOS loader paths must be a typed projection")
        self.macos_loader_paths = macos_loader_paths
        if linux_loader_paths is not None and not isinstance(
            linux_loader_paths, LinuxLoaderPaths
        ):
            raise ValueError("Linux loader paths must be a typed projection")
        self.linux_loader_paths = linux_loader_paths
        self.artifact_files = _explicit_artifact_files(artifact_files)

    def observe(
        self, build: Mapping[str, object], *, root_ref: str
    ) -> HostDependencyObservation:
        if sys.platform == "darwin":
            native = MacOsMachODependencyObserver(
                toolchain_commands=self.toolchain_commands,
                lifecycle_commands=self.lifecycle_commands,
                library_roots=self.library_roots,
                loader_paths=self.macos_loader_paths,
                artifact_files=self.artifact_files,
            ).observe(build, root_ref=root_ref)
        elif sys.platform.startswith("linux"):
            native = LinuxElfDependencyObserver(
                toolchain_commands=self.toolchain_commands,
                lifecycle_commands=self.lifecycle_commands,
                library_roots=self.library_roots,
                loader_paths=self.linux_loader_paths,
                artifact_files=self.artifact_files,
            ).observe(build, root_ref=root_ref)
        elif sys.platform in {"win32", "cygwin"}:
            native = WindowsPeDependencyObserver(
                toolchain_commands=self.toolchain_commands,
                lifecycle_commands=self.lifecycle_commands,
                library_roots=self.library_roots,
                environment=self.windows_environment,
                artifact_files=self.artifact_files,
            ).observe(build, root_ref=root_ref)
        else:
            raise DependencyObservationError(
                "dependencies.host-unsupported",
                f"no complete host dependency observer exists for {sys.platform!r}",
            )
        if not self.python_requirements:
            return native
        packages = observe_installed_python_distributions(
            self.python_requirements, root_ref=root_ref
        )
        return _normalized_observation(
            (*native.components, *packages.components),
            (*native.edges, *packages.edges),
        )


def _explicit_library_roots(roots):
    selected = tuple(Path(root) for root in roots)
    if len(selected) > 128 or any(
        not root.is_absolute() or ".." in root.parts for root in selected
    ):
        raise ValueError("native library roots must be bounded absolute paths")
    return tuple(dict.fromkeys(selected))


def _windows_environment_snapshot(environment):
    if environment is None:
        return None
    if not isinstance(environment, Mapping) or len(environment) > 2048:
        raise ValueError("Windows dependency environment must be a bounded mapping")
    result = {}
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 128
            or not isinstance(value, str)
            or len(value) > 65536
            or "\x00" in key + value
            or "=" in key
            or key.upper() in result
        ):
            raise ValueError("Windows dependency environment is invalid or ambiguous")
        result[key.upper()] = value
    return result


class MacOsMachODependencyObserver:
    """Observe the exact recursive Mach-O and npm closure used on macOS."""

    def __init__(
        self,
        *,
        toolchain_commands: Sequence[Sequence[str]] = (),
        lifecycle_commands: Sequence[str] = (),
        xcrun_command: Sequence[str] = ("xcrun",),
        library_roots: Sequence[Path] = (),
        artifact_files: Sequence[Path] | None = None,
        loader_paths: MacOsLoaderPaths | None = None,
    ) -> None:
        self.toolchain_commands = tuple(tuple(item) for item in toolchain_commands)
        self.lifecycle_commands = tuple(lifecycle_commands)
        self.xcrun_command = tuple(xcrun_command)
        self.library_roots = _explicit_library_roots(library_roots)
        self.artifact_files = _explicit_artifact_files(artifact_files)
        if loader_paths is not None and not isinstance(loader_paths, MacOsLoaderPaths):
            raise ValueError("macOS loader paths must be a typed projection")
        self.loader_paths = loader_paths
        if not self.xcrun_command:
            raise ValueError("xcrun command cannot be empty")

    def observe(
        self, build: Mapping[str, object], *, root_ref: str
    ) -> HostDependencyObservation:
        if sys.platform != "darwin":
            raise DependencyObservationError(
                "dependencies.macos-host-required",
                "Mach-O dependency observation requires a macOS host",
            )
        xcrun = _resolve_non_symlink_tool(self.xcrun_command[0])
        xcrun_digest = _file_digest(xcrun)
        dyld_output = self._run_tool(xcrun, ("--find", "dyld_info"))
        dyld_info = Path(dyld_output.strip())
        if (
            not dyld_info.is_absolute()
            or dyld_info.is_symlink()
            or not dyld_info.is_file()
        ):
            raise DependencyObservationError(
                "dependencies.macos-inspector-unsafe",
                "xcrun selected an unsafe dyld_info executable",
            )
        dyld_digest = _file_digest(dyld_info)
        self._dyld_info = dyld_info
        self._validated_paths: dict[str, bool] = {}
        artifact_root = _artifact_root(build)
        seed_paths: dict[str, set[str]] = {}
        direct_seed_paths: set[str] = set()
        for inspector in (xcrun, dyld_info):
            if not _is_macho(inspector):
                raise DependencyObservationError(
                    "dependencies.macos-inspector-format-invalid",
                    f"dependency inspector {inspector} is not a Mach-O executable",
                )
            inspector_path = str(inspector.resolve())
            seed_paths.setdefault(inspector_path, set()).update({"build", "toolchain"})
            direct_seed_paths.add(inspector_path)
        for path in _native_artifact_files(
            artifact_root, self.artifact_files, _is_macho
        ):
            artifact_path = str(path.resolve())
            seed_paths.setdefault(artifact_path, set()).update({"runtime", "system"})
            direct_seed_paths.add(artifact_path)
        npm_components: list[dict[str, object]] = [
            _file_component(
                xcrun,
                _file_component_ref(xcrun, prefix="host-inspector"),
                name="xcrun",
                scopes=("build", "toolchain"),
            ),
            _file_component(
                dyld_info,
                _file_component_ref(dyld_info, prefix="host-inspector"),
                name="dyld_info",
                scopes=("build", "toolchain"),
            ),
        ]
        npm_edges: list[tuple[str, str]] = [
            (root_ref, _file_component_ref(xcrun, prefix="host-inspector")),
            (root_ref, _file_component_ref(dyld_info, prefix="host-inspector")),
        ]
        npm_native_owners: dict[str, str] = {}
        launcher_runtime_edges: list[tuple[str, str]] = []
        for command in self.toolchain_commands:
            executable = _resolve_command(command)
            if not _is_macho(executable):
                observed = _npm_toolchain_launcher_dependencies(
                    executable,
                    command[0],
                    root_ref=root_ref,
                    native_predicate=_is_macho,
                )
                if observed is None:
                    raise DependencyObservationError(
                        "dependencies.macos-toolchain-not-macho",
                        f"toolchain command {executable} is not a direct Mach-O binary",
                    )
                package_observation, native_owners, runtime_edges = observed
                npm_components.extend(package_observation.components)
                npm_edges.extend(package_observation.edges)
                npm_native_owners.update(native_owners)
                launcher_runtime_edges.extend(runtime_edges)
                for native in (*native_owners, *(edge[1] for edge in runtime_edges)):
                    seed_paths.setdefault(native, set()).update({"build", "toolchain"})
                continue
            executable_path = str(executable)
            seed_paths.setdefault(executable_path, set()).update({"build", "toolchain"})
            direct_seed_paths.add(executable_path)
        for command in self.lifecycle_commands:
            launcher = _resolve_command((command,))
            if _is_macho(launcher):
                launcher_ref = _file_component_ref(
                    launcher, prefix="lifecycle-launcher"
                )
                npm_components.append(
                    _file_component(
                        launcher,
                        launcher_ref,
                        name=f"{command}-launcher",
                        scopes=("build", "toolchain"),
                    )
                )
                npm_edges.append((root_ref, launcher_ref))
                seed_paths.setdefault(str(launcher), set()).update(
                    {"build", "toolchain"}
                )
                direct_seed_paths.add(str(launcher))
                continue
            package_observation, native_owners, runtime_edges = (
                _npm_launcher_dependencies(
                    launcher,
                    command,
                    root_ref=root_ref,
                    prefix="lifecycle-launcher",
                    native_predicate=_is_macho,
                )
            )
            npm_components.extend(package_observation.components)
            npm_edges.extend(package_observation.edges)
            npm_native_owners.update(native_owners)
            launcher_runtime_edges.extend(runtime_edges)
            for native in (*native_owners, *(edge[1] for edge in runtime_edges)):
                seed_paths.setdefault(native, set()).update({"build", "toolchain"})

        if not seed_paths:
            raise DependencyObservationError(
                "dependencies.macos-seed-missing",
                "no generated, runtime, compiler, or lifecycle Mach-O binary "
                "was observed",
            )
        images, image_edges = self._closure(seed_paths)
        if (
            _file_digest(xcrun) != xcrun_digest
            or _file_digest(dyld_info) != dyld_digest
        ):
            raise DependencyObservationError(
                "dependencies.macos-inspector-changed",
                "xcrun or dyld_info changed during dependency observation",
            )
        components = [*npm_components]
        refs: dict[str, str] = {}
        for image in images:
            ref = _macho_ref(image)
            refs[image.path] = ref
            scopes = tuple(sorted(seed_paths.get(image.path, {"system"})))
            components.append(_macho_component(image, ref, scopes=scopes))
        edges = [*npm_edges]
        for inspector in (xcrun, dyld_info):
            edges.append(
                (
                    _file_component_ref(inspector, prefix="host-inspector"),
                    refs[str(inspector.resolve())],
                )
            )
        edges.extend(
            _native_dependency_edges(
                root_ref,
                refs=refs,
                direct_seed_paths=direct_seed_paths,
                image_edges=image_edges,
                native_owners=npm_native_owners,
                launcher_runtime_edges=launcher_runtime_edges,
            )
        )
        return _normalized_observation(components, edges)

    def _closure(
        self, seed_paths: Mapping[str, set[str]]
    ) -> tuple[tuple[_MachOImage, ...], tuple[tuple[str, str], ...]]:
        images: dict[str, _MachOImage] = {}
        edges: set[tuple[str, str]] = set()
        executable_roots = {
            path: str(Path(path).parent) for path in seed_paths if Path(path).exists()
        }
        loader_paths = getattr(self, "loader_paths", None)
        pending: deque[tuple[str, str, tuple[str, ...], bool]] = deque()
        for path in sorted(executable_roots):
            runtime = loader_paths is not None and "runtime" in seed_paths[path]
            pending.append((path, executable_roots[path], (), runtime))
            if runtime and "build" in seed_paths[path]:
                pending.append((path, executable_roots[path], (), False))
        processed: set[tuple[str, str, tuple[str, ...], bool]] = set()
        while pending:
            path, executable_dir, inherited_rpaths, runtime = pending.popleft()
            state = (path, executable_dir, inherited_rpaths, runtime)
            if state in processed:
                continue
            processed.add(state)
            image = images.get(path)
            if image is None:
                image = self._inspect(path)
                images[path] = image
            loader_dir = str(Path(path).parent)
            rpaths = self._expanded_rpaths(
                image.rpaths,
                loader_dir=loader_dir,
                executable_dir=executable_dir,
                inherited_rpaths=inherited_rpaths,
            )
            for load_path, weak in image.linked_paths:
                target = self._resolve_load_path(
                    load_path,
                    loader_dir=loader_dir,
                    executable_dir=executable_dir,
                    rpaths=rpaths,
                    weak=weak,
                    loader_paths=loader_paths if runtime else None,
                )
                if target is None:
                    continue
                edges.add((path, target))
                pending.append((target, executable_dir, rpaths, runtime))
        return tuple(images[path] for path in sorted(images)), tuple(sorted(edges))

    @staticmethod
    def _expanded_rpaths(
        raw_rpaths: Sequence[str],
        *,
        loader_dir: str,
        executable_dir: str,
        inherited_rpaths: Sequence[str],
    ) -> tuple[str, ...]:
        expanded: list[str] = []
        for raw in raw_rpaths:
            value = raw.replace("@loader_path", loader_dir).replace(
                "@executable_path", executable_dir
            )
            if value.startswith("@rpath/"):
                suffix = value.removeprefix("@rpath/")
                expanded.extend(
                    posixpath.normpath(posixpath.join(parent, suffix))
                    for parent in inherited_rpaths
                )
            elif value.startswith("/"):
                expanded.append(posixpath.normpath(value))
            else:
                raise DependencyObservationError(
                    "dependencies.macos-rpath-unsupported",
                    f"unsupported Mach-O LC_RPATH value {raw!r}",
                )
        return _ordered_unique((*expanded, *inherited_rpaths))

    def _inspect(self, path: str) -> _MachOImage:
        exact_path = Path(path)
        # Some Apple frameworks are valid dyld-shared-cache images whose public
        # filesystem path is an intentionally dangling compatibility symlink.  Only
        # paths which currently resolve to bytes are materialized; dyld UUID evidence
        # remains authoritative for shared-cache-only images.
        materialized = exact_path.exists()
        binding_before = _macho_materialized_file(exact_path) if materialized else None
        # Inspect each image once. The sectioned output supports all three facts;
        # starting a second inspector for load commands adds no authority.
        summary = self._run_dyld(("-uuid", "-linked_dylibs", "-load_commands", path))
        uuids, linked = parse_dyld_info_links(summary)
        if not uuids:
            raise DependencyObservationError(
                "dependencies.macos-uuid-missing",
                f"dyld_info did not report an exact Mach-O UUID for {path}",
            )
        materialized_after = exact_path.exists()
        binding_after = (
            _macho_materialized_file(exact_path) if materialized_after else None
        )
        if materialized_after != materialized or binding_after != binding_before:
            raise DependencyObservationError(
                "dependencies.macos-image-changed",
                f"Mach-O image changed during inspection: {path}",
            )
        return _MachOImage(
            path,
            uuids,
            linked,
            parse_dyld_info_rpaths(summary),
            binding_before,
        )

    def _resolve_load_path(
        self,
        load_path: str,
        *,
        loader_dir: str,
        executable_dir: str,
        rpaths: Sequence[str],
        weak: bool = False,
        loader_paths: MacOsLoaderPaths | None = None,
    ) -> str | None:
        before, after = loader_paths.candidates(load_path) if loader_paths else ((), ())
        for candidate in before:
            if self._valid_load_candidate(candidate):
                return candidate
        candidates: list[str] = []
        if load_path.startswith("@loader_path/"):
            candidates.append(
                posixpath.normpath(posixpath.join(loader_dir, load_path[13:]))
            )
        elif load_path.startswith("@executable_path/"):
            candidates.append(
                posixpath.normpath(posixpath.join(executable_dir, load_path[17:]))
            )
        elif load_path.startswith("@rpath/"):
            suffix = load_path[7:]
            for raw in rpaths:
                expanded = raw.replace("@loader_path", loader_dir).replace(
                    "@executable_path", executable_dir
                )
                if expanded.startswith("@rpath"):
                    continue
                candidates.append(posixpath.normpath(posixpath.join(expanded, suffix)))
            candidates.extend(str(root / suffix) for root in self.library_roots)
        elif load_path.startswith("/"):
            candidates.append(posixpath.normpath(load_path))
        elif (
            "/" not in load_path
            and load_path
            and (self.library_roots or loader_paths is not None)
        ):
            candidates.extend(str(root / load_path) for root in self.library_roots)
        else:
            raise DependencyObservationError(
                "dependencies.macos-load-path-unsupported",
                f"unsupported Mach-O load path {load_path!r}",
            )
        valid: set[str] = set()
        for candidate in candidates:
            if self._valid_load_candidate(candidate):
                valid.add(candidate)
        if not valid:
            for candidate in after:
                if self._valid_load_candidate(candidate):
                    return candidate
        if not valid and weak:
            return None
        if len(valid) != 1:
            raise DependencyObservationError(
                "dependencies.macos-load-path-ambiguous",
                f"Mach-O load path {load_path!r} resolved to {len(valid)} images",
            )
        return next(iter(valid))

    def _valid_load_candidate(self, candidate):
        cache = getattr(self, "_validated_paths", None)
        accepted = cache.get(candidate) if isinstance(cache, dict) else None
        if accepted is None:
            try:
                self._run_dyld(("-validate_only", candidate))
            except DependencyObservationError:
                accepted = False
            else:
                accepted = True
            if isinstance(cache, dict):
                cache[candidate] = accepted
        return accepted

    def _run_dyld(self, arguments: Sequence[str]) -> str:
        dyld_info = getattr(self, "_dyld_info", None)
        if not isinstance(dyld_info, Path):
            raise DependencyObservationError(
                "dependencies.macos-inspector-unpinned",
                "dyld_info was not pinned before dependency observation",
            )
        return self._run_tool(dyld_info, arguments)

    @staticmethod
    def _run_tool(tool: Path, arguments: Sequence[str]) -> str:
        command = [str(tool), *arguments]
        tool_identity = _file_digest(tool)
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DependencyObservationError(
                "dependencies.macos-inspector-failed",
                "xcrun dyld_info could not inspect the complete Mach-O closure",
            ) from exc
        if _file_digest(tool) != tool_identity:
            raise DependencyObservationError(
                "dependencies.macos-inspector-changed",
                "dependency inspector changed while it was executing",
            )
        output = completed.stdout + completed.stderr
        if len(output) > _MAX_TOOL_OUTPUT or completed.returncode != 0:
            raise DependencyObservationError(
                "dependencies.macos-inspector-failed",
                "xcrun dyld_info rejected a required Mach-O image",
            )
        try:
            return output.decode("utf-8")
        except UnicodeError as exc:
            raise DependencyObservationError(
                "dependencies.macos-inspector-output-invalid",
                "xcrun dyld_info emitted non-UTF-8 output",
            ) from exc


@dataclass(frozen=True, slots=True)
class _ElfImage:
    path: str
    exact_identity: str
    architecture: tuple[str, str]
    needed: tuple[str, ...]
    search_paths: tuple[str, ...]
    interpreter: str | None
    runpath_present: bool = False


class LinuxElfDependencyObserver:
    """Recursively inspect ELF imports without executing generated binaries."""

    def __init__(
        self,
        *,
        toolchain_commands: Sequence[Sequence[str]] = (),
        lifecycle_commands: Sequence[str] = (),
        library_roots: Sequence[Path] = (),
        loader_paths: LinuxLoaderPaths | None = None,
        artifact_files: Sequence[Path] | None = None,
    ) -> None:
        self.toolchain_commands = tuple(tuple(item) for item in toolchain_commands)
        self.lifecycle_commands = tuple(lifecycle_commands)
        self.library_roots = _explicit_library_roots(library_roots)
        if loader_paths is not None and not isinstance(loader_paths, LinuxLoaderPaths):
            raise ValueError("Linux loader paths must be a typed projection")
        self.loader_paths = loader_paths
        self.artifact_files = _explicit_artifact_files(artifact_files)

    def observe(
        self, build: Mapping[str, object], *, root_ref: str
    ) -> HostDependencyObservation:
        if not sys.platform.startswith("linux"):
            raise DependencyObservationError(
                "dependencies.linux-host-required",
                "ELF dependency observation requires a Linux host",
            )
        readelf = _resolved_bound_tool("readelf")
        readelf_digest = _file_digest(readelf)
        ldconfig = _find_linux_ldconfig()
        ldconfig_digest = _file_digest(ldconfig)
        loader_cache = parse_ldconfig_cache(
            _run_bounded_tool(ldconfig, ("-p",), code="dependencies.ldconfig-failed")
        )
        artifact_root = _artifact_root(build)
        seeds: dict[str, set[str]] = {}
        direct_seed_paths: set[str] = set()
        for path in _native_artifact_files(artifact_root, self.artifact_files, _is_elf):
            artifact_path = str(path.resolve())
            seeds.setdefault(artifact_path, set()).update({"runtime", "system"})
            direct_seed_paths.add(artifact_path)
        package_components: list[dict[str, object]] = []
        package_edges: list[tuple[str, str]] = []
        native_owners: dict[str, str] = {}
        launcher_runtime_edges: list[tuple[str, str]] = []
        for command in self.toolchain_commands:
            path = _resolve_command(command)
            if not _is_elf(path):
                observed = _npm_toolchain_launcher_dependencies(
                    path,
                    command[0],
                    root_ref=root_ref,
                    native_predicate=_is_elf,
                )
                if observed is None:
                    raise DependencyObservationError(
                        "dependencies.linux-toolchain-not-elf",
                        f"toolchain command {path} is not a direct ELF binary",
                    )
                observation, owners, runtime_edges = observed
                package_components.extend(observation.components)
                package_edges.extend(observation.edges)
                native_owners.update(owners)
                launcher_runtime_edges.extend(runtime_edges)
                for native in (*owners, *(edge[1] for edge in runtime_edges)):
                    seeds.setdefault(native, set()).update({"build", "toolchain"})
                continue
            path_text = str(path)
            seeds.setdefault(path_text, set()).update({"build", "toolchain"})
            direct_seed_paths.add(path_text)
        readelf_path = str(readelf)
        seeds.setdefault(readelf_path, set()).update({"build", "toolchain"})
        direct_seed_paths.add(readelf_path)
        ldconfig_ref = _file_component_ref(ldconfig, prefix="linux-loader-cache-tool")
        package_components.append(
            _file_component(
                ldconfig,
                ldconfig_ref,
                name="ldconfig-loader-cache-tool",
                scopes=("build", "toolchain"),
            )
        )
        package_edges.append((root_ref, ldconfig_ref))
        for command in self.lifecycle_commands:
            launcher = _resolve_command((command,))
            if _is_elf(launcher):
                launcher_ref = _file_component_ref(
                    launcher, prefix="lifecycle-launcher"
                )
                package_components.append(
                    _file_component(
                        launcher,
                        launcher_ref,
                        name=f"{command}-launcher",
                        scopes=("build", "toolchain"),
                    )
                )
                package_edges.append((root_ref, launcher_ref))
                seeds.setdefault(str(launcher), set()).update({"build", "toolchain"})
                direct_seed_paths.add(str(launcher))
                continue
            observation, owners, runtime_edges = _npm_launcher_dependencies(
                launcher,
                command,
                root_ref=root_ref,
                prefix="lifecycle-launcher",
                native_predicate=_is_elf,
            )
            package_components.extend(observation.components)
            package_edges.extend(observation.edges)
            native_owners.update(owners)
            launcher_runtime_edges.extend(runtime_edges)
            for native in (*owners, *(edge[1] for edge in runtime_edges)):
                seeds.setdefault(native, set()).update({"build", "toolchain"})
        if not seeds:
            raise DependencyObservationError(
                "dependencies.linux-seed-missing",
                "no generated, runtime, compiler, or lifecycle ELF binary was observed",
            )
        images, image_edges = self._closure(readelf, seeds, loader_cache)
        if (
            _file_digest(readelf) != readelf_digest
            or _file_digest(ldconfig) != ldconfig_digest
        ):
            raise DependencyObservationError(
                "dependencies.linux-inspector-changed",
                "readelf or ldconfig changed during dependency observation",
            )
        refs = {image.path: _elf_ref(image) for image in images}
        components = [*package_components]
        components.extend(
            _elf_component(
                image,
                refs[image.path],
                scopes=tuple(sorted(seeds.get(image.path, {"system"}))),
            )
            for image in images
        )
        edges = [*package_edges]
        edges.extend(
            _native_dependency_edges(
                root_ref,
                refs=refs,
                direct_seed_paths=direct_seed_paths,
                image_edges=image_edges,
                native_owners=native_owners,
                launcher_runtime_edges=launcher_runtime_edges,
            )
        )
        return _normalized_observation(components, edges)

    def _closure(
        self,
        readelf: Path,
        seeds: Mapping[str, set[str]],
        loader_cache: Mapping[str, tuple[str, ...]],
    ) -> tuple[tuple[_ElfImage, ...], tuple[tuple[str, str], ...]]:
        images: dict[str, _ElfImage] = {}
        edges: set[tuple[str, str]] = set()
        processed: set[tuple[str, str, bool]] = set()
        pending: deque[tuple[str, str, tuple[str, ...], bool]] = deque()
        for path, scopes in sorted(seeds.items()):
            runtime = self.loader_paths is not None and "runtime" in scopes
            pending.append((path, path, (), runtime))
            if runtime and "build" in scopes:
                pending.append((path, path, (), False))
        while pending:
            path, seed, inherited, runtime = pending.popleft()
            context = (path, seed, runtime)
            if context in processed:
                continue
            processed.add(context)
            image = images.get(path)
            if image is None:
                image = _inspect_elf(readelf, Path(path))
                images[path] = image
            own_paths: list[str] = []
            for raw in image.search_paths:
                directory = re.sub(
                    r"\$(?:\{ORIGIN\}|ORIGIN(?![A-Za-z0-9_]))",
                    lambda _match, origin=str(Path(path).parent): origin,
                    raw,
                )
                if "$" in directory or not Path(directory).is_absolute():
                    raise DependencyObservationError(
                        "dependencies.elf-search-path-unsafe",
                        f"ELF image {path} declares an unmodeled loader search path",
                    )
                own_paths.append(directory)
            search_paths = _ordered_unique(
                (*own_paths, *(() if image.runpath_present else inherited))
            )
            child_inherited = inherited if image.runpath_present else search_paths
            environment_paths = (
                self.loader_paths.library
                if runtime and self.loader_paths is not None
                else ()
            )
            search_paths = (
                (*environment_paths, *search_paths)
                if image.runpath_present
                else (*search_paths, *environment_paths)
            )
            targets: list[str] = []
            if image.interpreter is not None:
                interpreter = _resolve_exact_elf_candidate(
                    (image.interpreter,), image.architecture, readelf
                )
                edges.add((path, interpreter))
                pending.append((interpreter, interpreter, (), runtime))
            for name in image.needed:
                expanded = re.sub(
                    r"\$(?:\{ORIGIN\}|ORIGIN(?![A-Za-z0-9_]))",
                    lambda _match, origin=str(Path(path).parent): origin,
                    name,
                )
                if "$" in expanded:
                    raise DependencyObservationError(
                        "dependencies.elf-import-token-unsupported",
                        f"ELF image {path} requires an unmodeled import token",
                    )
                if "/" in expanded or Path(expanded).is_absolute():
                    direct = Path(expanded)
                    if not direct.is_absolute():
                        raise DependencyObservationError(
                            "dependencies.elf-import-path-unsafe",
                            f"ELF image {path} requires an import relative to cwd",
                        )
                    targets.append(
                        _resolve_exact_elf_candidate(
                            (str(direct),), image.architecture, readelf
                        )
                    )
                    continue
                candidates = [str(Path(directory, name)) for directory in search_paths]
                candidates.extend(loader_cache.get(name, ()))
                if self.loader_paths is None:
                    candidates.extend(str(root / name) for root in self.library_roots)
                targets.append(
                    _resolve_exact_elf_candidate(
                        tuple(candidates), image.architecture, readelf
                    )
                )
            for target in targets:
                edges.add((path, target))
                if (target, seed, runtime) not in processed:
                    pending.append((target, seed, child_inherited, runtime))
        return tuple(images[path] for path in sorted(images)), tuple(sorted(edges))


@dataclass(frozen=True, slots=True)
class _PeImage:
    path: str
    digest: str
    imports: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True)
class _PeInspector:
    path: Path
    kind: str
    version: str
    version_output_identity: str
    digest: str


@dataclass(frozen=True, slots=True)
class _WindowsApiSetResolution:
    schema_contract: str
    host: str


@dataclass(frozen=True, slots=True)
class _WindowsApiSetSchema:
    path: str
    digest: str
    namespace_version: int
    contracts: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    lookup_prefixes: tuple[tuple[str, str], ...]
    mapping_identity: str

    def resolve(self, contract: str, *, importer: str) -> _WindowsApiSetResolution:
        normalized = contract.casefold()
        inventory = dict(self.contracts)
        schema_contract = normalized if normalized in inventory else None
        if schema_contract is None:
            lookup_name = normalized.removesuffix(".dll")
            matches = [
                candidate
                for candidate, prefix in self.lookup_prefixes
                if lookup_name == prefix or lookup_name.startswith(f"{prefix}-")
            ]
            if matches:
                longest = max(
                    len(prefix)
                    for candidate, prefix in self.lookup_prefixes
                    if candidate in matches
                    and (lookup_name == prefix or lookup_name.startswith(f"{prefix}-"))
                )
                matches = [
                    candidate
                    for candidate, prefix in self.lookup_prefixes
                    if len(prefix) == longest
                    and (lookup_name == prefix or lookup_name.startswith(f"{prefix}-"))
                ]
            if len(set(matches)) == 1:
                schema_contract = matches[0]
        if schema_contract is None:
            raise DependencyObservationError(
                "dependencies.pe-api-set-unresolved",
                f"Windows API-set contract {contract!r} is absent from the "
                "bound schema",
            )
        values = inventory[schema_contract]
        importer_name = PureWindowsPath(importer).name.casefold()
        aliases = [
            host
            for alias, host in values
            if alias and alias.casefold() == importer_name
        ]
        selected = aliases or [host for alias, host in values if not alias]
        if len(set(selected)) != 1:
            raise DependencyObservationError(
                "dependencies.pe-api-set-ambiguous",
                f"Windows API-set contract {contract!r} has no exact host mapping",
            )
        if not selected[0]:
            raise DependencyObservationError(
                "dependencies.pe-api-set-unresolved",
                f"Windows API-set contract {contract!r} is unavailable on this host",
            )
        return _WindowsApiSetResolution(schema_contract, selected[0])


@dataclass(frozen=True, slots=True)
class _PeApiSetBinding:
    importer_path: str
    contract: str
    schema_contract: str | None
    expected_host: str
    target_path: str | None
    delayed: bool


@dataclass(frozen=True, slots=True)
class _PeUnavailableDelayImport:
    importer_path: str
    name: str


class WindowsPeDependencyObserver:
    """Recursively inspect PE imports without launching candidate binaries."""

    def __init__(
        self,
        *,
        toolchain_commands: Sequence[Sequence[str]] = (),
        lifecycle_commands: Sequence[str] = (),
        library_roots: Sequence[Path] = (),
        environment: Mapping[str, str] | None = None,
        artifact_files: Sequence[Path] | None = None,
    ) -> None:
        self.toolchain_commands = tuple(tuple(item) for item in toolchain_commands)
        self.lifecycle_commands = tuple(lifecycle_commands)
        self.library_roots = _explicit_library_roots(library_roots)
        self.environment = _windows_environment_snapshot(environment)
        self.artifact_files = _explicit_artifact_files(artifact_files)

    def observe(
        self, build: Mapping[str, object], *, root_ref: str
    ) -> HostDependencyObservation:
        if sys.platform not in {"win32", "cygwin"}:
            raise DependencyObservationError(
                "dependencies.windows-host-required",
                "PE dependency observation requires a Windows host",
            )
        environment = self.environment
        if environment is None:
            environment = {key.upper(): value for key, value in os.environ.items()}
        system_root = environment.get("SYSTEMROOT")
        if not system_root:
            raise DependencyObservationError(
                "dependencies.windows-system-root-missing",
                "SystemRoot is required to resolve the PE system closure",
            )
        path_value = environment.get("PATH", "")
        search_paths = path_value.split(os.pathsep) if path_value else []
        if self.environment is not None and (
            not Path(system_root).is_absolute()
            or ".." in Path(system_root).parts
            or len(search_paths) > 128
            or any(
                not value or not Path(value).is_absolute() or ".." in Path(value).parts
                for value in search_paths
            )
        ):
            raise ValueError(
                "Windows dependency environment requires absolute search paths"
            )
        inspector = _find_windows_pe_inspector()
        api_set_schema = _load_windows_api_set_schema(
            Path(system_root) / "System32" / "apisetschema.dll"
        )
        artifact_root = _artifact_root(build)
        seeds: dict[str, set[str]] = {}
        direct_seed_paths: set[str] = set()
        for path in _native_artifact_files(artifact_root, self.artifact_files, _is_pe):
            artifact_path = str(path.resolve())
            seeds.setdefault(artifact_path, set()).update({"runtime", "system"})
            direct_seed_paths.add(artifact_path)
        command_paths: list[Path] = []
        package_components: list[dict[str, object]] = []
        package_edges: list[tuple[str, str]] = []
        native_owners: dict[str, str] = {}
        launcher_runtime_edges: list[tuple[str, str]] = []
        for command in self.toolchain_commands:
            path = _resolve_pe_toolchain_input(command)
            command_paths.append(path)
            if not _is_pe(path):
                observed = _npm_toolchain_launcher_dependencies(
                    path,
                    command[0],
                    root_ref=root_ref,
                    native_predicate=_is_pe,
                )
                if observed is None:
                    raise DependencyObservationError(
                        "dependencies.windows-toolchain-not-pe",
                        f"toolchain command {path} is not a direct PE binary",
                    )
                observation, owners, runtime_edges = observed
                package_components.extend(observation.components)
                package_edges.extend(observation.edges)
                native_owners.update(owners)
                launcher_runtime_edges.extend(runtime_edges)
                for native in (*owners, *(edge[1] for edge in runtime_edges)):
                    seeds.setdefault(native, set()).update({"build", "toolchain"})
                    command_paths.append(Path(native))
                continue
            path_text = str(path)
            seeds.setdefault(path_text, set()).update({"build", "toolchain"})
            direct_seed_paths.add(path_text)
        inspector_path = str(inspector.path)
        seeds.setdefault(inspector_path, set()).update({"build", "toolchain"})
        direct_seed_paths.add(inspector_path)
        seeds.setdefault(api_set_schema.path, set()).update(
            {"runtime", "system", "toolchain"}
        )
        direct_seed_paths.add(api_set_schema.path)
        for command in self.lifecycle_commands:
            launcher = _resolve_command((command,))
            if _is_pe(launcher):
                launcher_ref = _file_component_ref(
                    launcher, prefix="lifecycle-launcher"
                )
                package_components.append(
                    _file_component(
                        launcher,
                        launcher_ref,
                        name=f"{command}-launcher",
                        scopes=("build", "toolchain"),
                    )
                )
                package_edges.append((root_ref, launcher_ref))
                seeds.setdefault(str(launcher), set()).update({"build", "toolchain"})
                direct_seed_paths.add(str(launcher))
                continue
            observation, owners, runtime_edges = _npm_launcher_dependencies(
                launcher,
                command,
                root_ref=root_ref,
                prefix="lifecycle-launcher",
                native_predicate=_is_pe,
            )
            package_components.extend(observation.components)
            package_edges.extend(observation.edges)
            native_owners.update(owners)
            launcher_runtime_edges.extend(runtime_edges)
            for native in (*owners, *(edge[1] for edge in runtime_edges)):
                seeds.setdefault(native, set()).update({"build", "toolchain"})
        roots = [
            artifact_root,
            Path(system_root) / "System32",
            Path(system_root) / "System32" / "downlevel",
            *(path.parent for path in command_paths),
            *self.library_roots,
            *(Path(item) for item in search_paths if item),
        ]
        images, image_edges, api_set_bindings, unavailable_delay_imports = _pe_closure(
            inspector,
            seeds,
            roots,
            api_set_schema=api_set_schema,
        )
        if _file_digest(inspector.path) != inspector.digest:
            raise DependencyObservationError(
                "dependencies.windows-inspector-changed",
                "the PE dependency inspector changed during observation",
            )
        if _file_digest(Path(api_set_schema.path)) != api_set_schema.digest:
            raise DependencyObservationError(
                "dependencies.pe-api-set-changed",
                "the Windows API-set schema changed during dependency observation",
            )
        refs = {image.path: _pe_ref(image) for image in images}
        unavailable_by_importer: dict[str, list[str]] = {}
        for item in unavailable_delay_imports:
            unavailable_by_importer.setdefault(item.importer_path, []).append(item.name)
        api_set_keys = {
            (binding.contract, binding.target_path) for binding in api_set_bindings
        }
        api_set_schema_contracts: dict[tuple[str, str | None], set[str | None]] = {
            key: set() for key in api_set_keys
        }
        api_set_expected_hosts: dict[tuple[str, str | None], set[str]] = {
            key: set() for key in api_set_keys
        }
        for binding in api_set_bindings:
            key = (binding.contract, binding.target_path)
            api_set_schema_contracts[key].add(binding.schema_contract)
            api_set_expected_hosts[key].add(binding.expected_host)
        if any(
            len(values) != 1
            for values in (
                *api_set_schema_contracts.values(),
                *api_set_expected_hosts.values(),
            )
        ):
            raise DependencyObservationError(
                "dependencies.pe-api-set-ambiguous",
                "one Windows API-set contract resolved through conflicting "
                "schema entries",
            )
        api_set_refs = {
            key: _pe_api_set_ref(
                key[0],
                schema=api_set_schema,
                schema_contract=next(iter(api_set_schema_contracts[key])),
                expected_host=next(iter(api_set_expected_hosts[key])),
                target_ref=(None if key[1] is None else refs[key[1]]),
            )
            for key in sorted(api_set_keys, key=lambda item: (item[0], item[1] or ""))
        }
        components = [*package_components]
        for image in images:
            component = _pe_component(
                image,
                refs[image.path],
                scopes=tuple(sorted(seeds.get(image.path, {"system"}))),
                unavailable_delay_imports=tuple(
                    sorted(unavailable_by_importer.get(image.path, ()))
                ),
            )
            properties = component["properties"]
            assert isinstance(properties, list)
            if image.path == str(inspector.path):
                properties.extend(
                    [
                        {
                            "name": "literate-ai:pe-inspector-kind",
                            "value": inspector.kind,
                        },
                        {
                            "name": "literate-ai:pe-inspector-version",
                            "value": inspector.version,
                        },
                        {
                            "name": "literate-ai:pe-inspector-version-output-identity",
                            "value": inspector.version_output_identity,
                        },
                    ]
                )
            if image.path == api_set_schema.path:
                properties.extend(
                    [
                        {
                            "name": "literate-ai:pe-api-set-namespace-version",
                            "value": str(api_set_schema.namespace_version),
                        },
                        {
                            "name": "literate-ai:pe-api-set-contract-count",
                            "value": str(len(api_set_schema.contracts)),
                        },
                        {
                            "name": "literate-ai:pe-api-set-mapping-identity",
                            "value": api_set_schema.mapping_identity,
                        },
                    ]
                )
            components.append(component)
        api_set_scopes: dict[tuple[str, str | None], set[str]] = {
            key: {"runtime", "system"} for key in api_set_keys
        }
        api_set_delayed = {key: True for key in api_set_keys}
        for binding in api_set_bindings:
            api_set_scopes[(binding.contract, binding.target_path)].update(
                seeds.get(binding.importer_path, {"system"})
            )
            api_set_delayed[(binding.contract, binding.target_path)] &= binding.delayed
        components.extend(
            _pe_api_set_component(
                contract,
                api_set_refs[(contract, target_path)],
                schema=api_set_schema,
                schema_contract=next(
                    iter(api_set_schema_contracts[(contract, target_path)])
                ),
                expected_host=next(
                    iter(api_set_expected_hosts[(contract, target_path)])
                ),
                target_path=target_path,
                scopes=tuple(sorted(api_set_scopes[(contract, target_path)])),
                delayed=api_set_delayed[(contract, target_path)],
            )
            for contract, target_path in sorted(
                api_set_keys, key=lambda item: (item[0], item[1] or "")
            )
        )
        edges = [*package_edges]
        edges.extend(
            _native_dependency_edges(
                root_ref,
                refs=refs,
                direct_seed_paths=direct_seed_paths,
                image_edges=image_edges,
                native_owners=native_owners,
                launcher_runtime_edges=launcher_runtime_edges,
            )
        )
        for binding in api_set_bindings:
            contract_ref = api_set_refs[(binding.contract, binding.target_path)]
            edges.extend(
                (
                    (refs[binding.importer_path], contract_ref),
                    (contract_ref, refs[api_set_schema.path]),
                )
            )
            if binding.target_path is not None:
                edges.append((contract_ref, refs[binding.target_path]))
        return _normalized_observation(components, edges)


def parse_dyld_info_summary(
    text: str,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    uuids, links = parse_dyld_info_links(text)
    return uuids, tuple(path for path, _weak in links)


def parse_dyld_info_links(
    text: str,
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, bool], ...]]:
    """Parse exact Mach-O UUIDs and preserve weak-link load attributes."""

    arch: str | None = None
    section: str | None = None
    uuids: set[tuple[str, str]] = set()
    linked: dict[str, bool] = {}
    for raw in text.splitlines():
        stripped = raw.strip()
        header = re.search(r"\[([^]]+)\]:$", stripped)
        if header:
            arch = header.group(1)
            section = None
            continue
        if stripped.startswith("-") and stripped.endswith(":"):
            section = {
                "-uuid:": "uuid",
                "-linked_dylibs:": "linked",
            }.get(stripped)
            continue
        if section == "uuid" and arch is not None and _UUID.fullmatch(stripped):
            uuids.add((arch, stripped.lower()))
        elif section == "linked":
            match = re.fullmatch(r"(.*?)([/@].*)", stripped)
            if match:
                attributes, path = match.groups()
                weak = "weak-link" in attributes.split()
                normalized_path = path.strip()
                if normalized_path:
                    linked[normalized_path] = linked.get(normalized_path, True) and weak
    return tuple(sorted(uuids)), tuple(sorted(linked.items()))


def parse_dyld_info_rpaths(text: str) -> tuple[str, ...]:
    result: set[str] = set()
    lines = iter(text.splitlines())
    for raw in lines:
        if raw.strip() != "cmd: LC_RPATH":
            continue
        for following in lines:
            stripped = following.strip()
            if stripped.startswith(("path:", "rpath:")):
                _label, value = stripped.split(":", 1)
                value = value.strip().strip('"')
                if value:
                    result.add(value)
                break
            if stripped.startswith("Load command #"):
                break
    return tuple(sorted(result))


def parse_readelf_dynamic(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Parse direct imports and this image's effective loader path tag."""

    needed = _ordered_unique(re.findall(r"\(NEEDED\).*?\[([^]]+)]", text))
    runpaths = re.findall(r"\(RUNPATH\).*?\[([^]]*)]", text)
    selected = runpaths if runpaths else re.findall(r"\(RPATH\).*?\[([^]]*)]", text)
    paths: list[str] = []
    for value in selected:
        if not value:
            continue
        entries = value.split(":")
        if any(not item for item in entries):
            raise DependencyObservationError(
                "dependencies.elf-search-path-unsafe",
                "ELF loader paths require an unmodeled current-directory entry",
            )
        paths.extend(entries)
    return needed, _ordered_unique(paths)


def parse_dumpbin_dependents(text: str) -> tuple[str, ...]:
    """Parse case-insensitive direct PE imports from dumpbin output."""

    return tuple(name for name, _delayed in _parse_dumpbin_import_records(text))


def _parse_dumpbin_import_records(text: str) -> tuple[tuple[str, bool], ...]:
    delayed = False
    result: dict[str, bool] = {}
    for line in text.splitlines():
        normalized_line = line.casefold()
        if "delay load dependencies" in normalized_line:
            delayed = True
            continue
        if "dependencies" in normalized_line:
            delayed = False
            continue
        match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+\.dll)\s*", line, flags=re.IGNORECASE)
        if match is None:
            continue
        name = match.group(1).casefold()
        result[name] = result.get(name, True) and delayed
    return tuple(sorted(result.items()))


def parse_llvm_readobj_imports(text: str) -> tuple[str, ...]:
    """Parse direct and delay-load PE imports from ``llvm-readobj`` output."""

    return tuple(name for name, _delayed in _parse_llvm_readobj_import_records(text))


def _parse_llvm_readobj_import_records(
    text: str,
) -> tuple[tuple[str, bool], ...]:
    pending_delay: bool | None = None
    result: dict[str, bool] = {}
    for line in text.splitlines():
        if line == "Import {":
            pending_delay = False
            continue
        if line == "DelayImport {":
            pending_delay = True
            continue
        if pending_delay is None:
            continue
        match = re.fullmatch(
            r"\s*Name:\s*([A-Za-z0-9_.-]+\.dll)\s*",
            line,
            flags=re.IGNORECASE,
        )
        if match is None:
            continue
        name = match.group(1).casefold()
        result[name] = result.get(name, True) and pending_delay
        pending_delay = None
    return tuple(sorted(result.items()))


def parse_ldconfig_cache(text: str) -> dict[str, tuple[str, ...]]:
    """Parse ldconfig's loader-cache view without consulting target binaries."""

    result: dict[str, list[str]] = {}
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or "=>" not in stripped:
            continue
        match = re.fullmatch(r"([^\s]+)\s+\([^)]*\)\s+=>\s+(/\S+)", stripped)
        if match is None:
            raise DependencyObservationError(
                "dependencies.ldconfig-output-invalid",
                "ldconfig emitted an unrecognized cache entry",
            )
        name, path = match.groups()
        result.setdefault(name, []).append(path)
    if not result:
        raise DependencyObservationError(
            "dependencies.ldconfig-output-invalid",
            "ldconfig did not expose any loader-cache entries",
        )
    return {name: _ordered_unique(paths) for name, paths in sorted(result.items())}


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _run_bounded_tool(
    tool: Path,
    arguments: Sequence[str],
    *,
    code: str,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> str:
    if not tool.is_absolute() or tool.is_symlink() or not tool.is_file():
        raise DependencyObservationError(
            "dependencies.inspector-unsafe",
            "dependency inspector must be an absolute non-symlink file",
        )
    environment = {"LC_ALL": "C", "LANG": "C"}
    for name in ("SystemRoot", "WINDIR"):
        if value := os.environ.get(name):
            environment[name] = value
    tool_identity = _file_digest(tool)
    try:
        completed = subprocess.run(
            [str(tool), *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DependencyObservationError(
            code, f"dependency inspector {tool} could not complete"
        ) from exc
    if _file_digest(tool) != tool_identity:
        raise DependencyObservationError(
            "dependencies.inspector-changed",
            f"dependency inspector {tool} changed while it was executing",
        )
    output = completed.stdout + completed.stderr
    if (
        completed.returncode not in allowed_returncodes
        or len(output) > _MAX_TOOL_OUTPUT
    ):
        raise DependencyObservationError(
            code, f"dependency inspector {tool} rejected a required image"
        )
    try:
        return output.decode("utf-8")
    except UnicodeError as exc:
        raise DependencyObservationError(
            f"{code}.output-invalid",
            f"dependency inspector {tool} emitted non-UTF-8 output",
        ) from exc


def _resolved_bound_tool(name: str) -> Path:
    found = shutil.which(name)
    if found is None:
        raise DependencyObservationError(
            "dependencies.inspector-missing",
            f"required dependency inspector {name!r} is unavailable",
        )
    try:
        path = Path(found).resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.inspector-unsafe",
            f"required dependency inspector {name!r} cannot be resolved",
        ) from exc
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise DependencyObservationError(
            "dependencies.inspector-unsafe",
            f"required dependency inspector {name!r} is not a safe exact file",
        )
    return path


def _find_windows_pe_inspector() -> _PeInspector:
    candidates = (
        (
            "dumpbin",
            "dumpbin",
            ("/?",),
            r"Version\s+([0-9]+(?:\.[0-9]+)+)",
            frozenset({0, 1100}),
        ),
        (
            "llvm-readobj",
            "llvm-readobj",
            ("--version",),
            r"LLVM version\s+([0-9]+(?:\.[0-9]+)+)",
            frozenset({0}),
        ),
    )
    for kind, command, arguments, version_pattern, allowed_returncodes in candidates:
        if shutil.which(command) is None:
            continue
        path = _resolved_bound_tool(command)
        digest = _file_digest(path)
        output = _run_bounded_tool(
            path,
            arguments,
            code=f"dependencies.{kind}-version-failed",
            allowed_returncodes=allowed_returncodes,
        )
        match = re.search(version_pattern, output, flags=re.IGNORECASE)
        if match is None:
            raise DependencyObservationError(
                "dependencies.pe-inspector-version-invalid",
                f"PE dependency inspector {path} did not report an exact version",
            )
        if _file_digest(path) != digest:
            raise DependencyObservationError(
                "dependencies.windows-inspector-changed",
                "the PE dependency inspector changed while binding its version",
            )
        version = match.group(1)
        version_output_identity = canonical_identity(
            {
                "schema": "urn:literate-ai:windows-pe-inspector-version@1",
                "kind": kind,
                "version": version,
                "output": output,
            }
        ).uri
        return _PeInspector(
            path=path,
            kind=kind,
            version=version,
            version_output_identity=version_output_identity,
            digest=digest,
        )
    raise DependencyObservationError(
        "dependencies.inspector-missing",
        "Windows PE observation requires dumpbin or llvm-readobj on PATH",
    )


def _inspect_pe_imports(
    inspector: _PeInspector, path: Path
) -> tuple[tuple[str, bool], ...]:
    if inspector.kind == "dumpbin":
        output = _run_bounded_tool(
            inspector.path,
            ("/NOLOGO", "/DEPENDENTS", str(path)),
            code="dependencies.dumpbin-failed",
        )
        return _parse_dumpbin_import_records(output)
    if inspector.kind == "llvm-readobj":
        output = _run_bounded_tool(
            inspector.path,
            ("--coff-imports", str(path)),
            code="dependencies.llvm-readobj-failed",
        )
        return _parse_llvm_readobj_import_records(output)
    raise DependencyObservationError(
        "dependencies.pe-inspector-invalid",
        f"unsupported PE dependency inspector kind {inspector.kind!r}",
    )


def _find_linux_ldconfig() -> Path:
    candidates = [
        Path(item) for item in resolve_host_system_paths().linux_ldconfig_candidates
    ]
    found = shutil.which("ldconfig")
    if found is not None:
        candidates.append(Path(found))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_absolute() and not resolved.is_symlink() and resolved.is_file():
            return resolved
    raise DependencyObservationError(
        "dependencies.ldconfig-missing",
        "a safe exact ldconfig executable is required to prove the ELF closure",
    )


def _is_elf(path: Path) -> bool:
    return _stable_file_bytes(path, limit=4) == b"\x7fELF"


def _inspect_elf(readelf: Path, path: Path) -> _ElfImage:
    try:
        exact_path = path.resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.elf-image-missing", f"ELF image {path} is unavailable"
        ) from exc
    if exact_path.is_symlink() or not exact_path.is_file() or not _is_elf(exact_path):
        raise DependencyObservationError(
            "dependencies.elf-image-invalid", f"ELF image {path} is unsafe or invalid"
        )
    image_digest = _file_digest(exact_path)
    header = _run_bounded_tool(
        readelf, ("-h", str(exact_path)), code="dependencies.readelf-header-failed"
    )
    elf_class = re.search(r"^\s*Class:\s*(\S+)\s*$", header, re.MULTILINE)
    machine = re.search(r"^\s*Machine:\s*(.+?)\s*$", header, re.MULTILINE)
    if elf_class is None or machine is None:
        raise DependencyObservationError(
            "dependencies.readelf-header-invalid",
            f"readelf omitted the architecture of {exact_path}",
        )
    dynamic = _run_bounded_tool(
        readelf, ("-d", str(exact_path)), code="dependencies.readelf-dynamic-failed"
    )
    needed, search_paths = parse_readelf_dynamic(dynamic)
    program_headers = _run_bounded_tool(
        readelf, ("-l", str(exact_path)), code="dependencies.readelf-program-failed"
    )
    interpreter_match = re.search(
        r"\[Requesting program interpreter:\s*([^]]+)]", program_headers
    )
    notes = _run_bounded_tool(
        readelf, ("-n", str(exact_path)), code="dependencies.readelf-notes-failed"
    )
    build_id = re.search(r"^\s*Build ID:\s*([0-9A-Fa-f]+)\s*$", notes, re.MULTILINE)
    exact_identity = (
        f"build-id:{build_id.group(1).casefold()}"
        if build_id is not None
        else image_digest
    )
    if _file_digest(exact_path) != image_digest:
        raise DependencyObservationError(
            "dependencies.elf-image-changed",
            f"ELF image changed during inspection: {exact_path}",
        )
    return _ElfImage(
        path=str(exact_path),
        exact_identity=exact_identity,
        architecture=(elf_class.group(1), machine.group(1)),
        needed=needed,
        search_paths=search_paths,
        runpath_present=bool(re.search(r"\(RUNPATH\)", dynamic)),
        interpreter=(
            interpreter_match.group(1).strip()
            if interpreter_match is not None
            else None
        ),
    )


def _resolve_exact_elf_candidate(
    candidates: Sequence[str], architecture: tuple[str, str], readelf: Path
) -> str:
    for raw in _ordered_unique(candidates):
        candidate = Path(raw)
        if not candidate.is_absolute() or not candidate.exists():
            continue
        try:
            exact = candidate.resolve(strict=True)
        except OSError:
            continue
        if exact.is_symlink() or not exact.is_file() or not _is_elf(exact):
            continue
        header = _run_bounded_tool(
            readelf, ("-h", str(exact)), code="dependencies.readelf-header-failed"
        )
        elf_class = re.search(r"^\s*Class:\s*(\S+)\s*$", header, re.MULTILINE)
        machine = re.search(r"^\s*Machine:\s*(.+?)\s*$", header, re.MULTILINE)
        if (
            elf_class is not None
            and machine is not None
            and (elf_class.group(1), machine.group(1)) == architecture
        ):
            return str(exact)
    raise DependencyObservationError(
        "dependencies.elf-import-unresolved",
        "no architecture-compatible exact ELF image satisfies a required import",
    )


def _elf_ref(image: _ElfImage) -> str:
    identity = canonical_identity(
        {
            "elf_path": image.path,
            "elf_identity": image.exact_identity,
            "architecture": list(image.architecture),
        }
    )
    return f"urn:literate-ai:elf:{identity.digest}"


def _elf_component(
    image: _ElfImage, ref: str, *, scopes: Sequence[str]
) -> dict[str, object]:
    digest = _file_digest(Path(image.path)).removeprefix("sha256:")
    return {
        "type": "file",
        "bom-ref": ref,
        "name": Path(image.path).name,
        "version": image.exact_identity.replace(":", "-"),
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "system"},
            *(
                {"name": "literate-ai:dependency-scope", "value": scope}
                for scope in sorted(set(scopes) | {"system"})
            ),
            {"name": "literate-ai:elf-path", "value": image.path},
            {
                "name": "literate-ai:elf-architecture",
                "value": "/".join(image.architecture),
            },
        ],
    }


def _is_pe(path: Path) -> bool:
    dos_header = _stable_file_bytes(path, limit=0x40)
    if dos_header[:2] != b"MZ" or len(dos_header) < 0x40:
        return False
    offset = int.from_bytes(dos_header[0x3C:0x40], "little")
    if offset < 0x40:
        return False
    return _stable_file_bytes(path, offset=offset, limit=4) == b"PE\x00\x00"


def _is_windows_api_set_contract(name: str) -> bool:
    normalized = name.casefold()
    return normalized.endswith(".dll") and normalized.startswith(("api-ms-", "ext-ms-"))


def _load_windows_api_set_schema(path: Path) -> _WindowsApiSetSchema:
    if path.is_symlink() or not path.is_file():
        raise DependencyObservationError(
            "dependencies.pe-api-set-missing",
            "the authoritative Windows API-set schema is unavailable",
        )
    try:
        exact = path.resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.pe-api-set-missing",
            "the authoritative Windows API-set schema cannot be resolved",
        ) from exc
    content = _stable_file_bytes(exact)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    namespace = _pe_section_bytes(content, name=".apiset")
    namespace_version, contracts, lookup_prefixes = _parse_windows_api_set_namespace(
        namespace
    )
    mapping_identity = canonical_identity(
        {
            "schema": "urn:literate-ai:windows-api-set-map@1",
            "namespace_version": namespace_version,
            "contracts": [
                {
                    "contract": contract,
                    "lookup_prefix": dict(lookup_prefixes)[contract],
                    "values": [
                        {"alias": alias, "host": host} for alias, host in values
                    ],
                }
                for contract, values in contracts
            ],
        }
    ).uri
    if _file_digest(exact) != digest:
        raise DependencyObservationError(
            "dependencies.pe-api-set-changed",
            "the Windows API-set schema changed while it was parsed",
        )
    return _WindowsApiSetSchema(
        path=str(exact),
        digest=digest,
        namespace_version=namespace_version,
        contracts=contracts,
        lookup_prefixes=lookup_prefixes,
        mapping_identity=mapping_identity,
    )


def _pe_section_bytes(content: bytes, *, name: str) -> bytes:
    if content[:2] != b"MZ" or len(content) < 0x40:
        raise DependencyObservationError(
            "dependencies.pe-api-set-image-invalid",
            "Windows API-set material is not a valid PE image",
        )
    pe_offset = int.from_bytes(content[0x3C:0x40], "little")
    if pe_offset + 24 > len(content) or content[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise DependencyObservationError(
            "dependencies.pe-api-set-image-invalid",
            "Windows API-set material has an invalid PE header",
        )
    coff_offset = pe_offset + 4
    section_count = int.from_bytes(content[coff_offset + 2 : coff_offset + 4], "little")
    optional_size = int.from_bytes(
        content[coff_offset + 16 : coff_offset + 18], "little"
    )
    if not 0 < section_count <= 96:
        raise DependencyObservationError(
            "dependencies.pe-api-set-image-invalid",
            "Windows API-set material has an invalid PE section count",
        )
    table_offset = coff_offset + 20 + optional_size
    table_end = table_offset + section_count * 40
    if table_end > len(content):
        raise DependencyObservationError(
            "dependencies.pe-api-set-image-invalid",
            "Windows API-set material has a truncated PE section table",
        )
    matches: list[bytes] = []
    for index in range(section_count):
        offset = table_offset + index * 40
        raw_name = content[offset : offset + 8].split(b"\0", 1)[0]
        try:
            section_name = raw_name.decode("ascii")
        except UnicodeError as exc:
            raise DependencyObservationError(
                "dependencies.pe-api-set-image-invalid",
                "Windows API-set material has a non-ASCII PE section name",
            ) from exc
        if section_name != name:
            continue
        raw_size = int.from_bytes(content[offset + 16 : offset + 20], "little")
        raw_offset = int.from_bytes(content[offset + 20 : offset + 24], "little")
        if raw_size == 0 or raw_offset + raw_size > len(content):
            raise DependencyObservationError(
                "dependencies.pe-api-set-image-invalid",
                "Windows API-set material has an invalid schema section",
            )
        matches.append(content[raw_offset : raw_offset + raw_size])
    if len(matches) != 1:
        raise DependencyObservationError(
            "dependencies.pe-api-set-image-invalid",
            "Windows API-set material must contain exactly one .apiset section",
        )
    return matches[0]


def _parse_windows_api_set_namespace(
    content: bytes,
) -> tuple[
    int,
    tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
    tuple[tuple[str, str], ...],
]:
    if len(content) < 28:
        raise DependencyObservationError(
            "dependencies.pe-api-set-schema-invalid",
            "Windows API-set namespace header is truncated",
        )
    namespace_version = _api_set_u32(content, 0, len(content))
    size = _api_set_u32(content, 4, len(content))
    count = _api_set_u32(content, 12, len(content))
    entry_offset = _api_set_u32(content, 16, len(content))
    if namespace_version != 6:
        raise DependencyObservationError(
            "dependencies.pe-api-set-schema-unsupported",
            f"Windows API-set namespace version {namespace_version} is unsupported",
        )
    if size < 28 or size > len(content) or count > _MAX_OBSERVED_FILES:
        raise DependencyObservationError(
            "dependencies.pe-api-set-schema-invalid",
            "Windows API-set namespace bounds are invalid",
        )
    if entry_offset > size or count * 24 > size - entry_offset:
        raise DependencyObservationError(
            "dependencies.pe-api-set-schema-invalid",
            "Windows API-set namespace entry table is truncated",
        )
    contracts: dict[str, tuple[tuple[str, str], ...]] = {}
    lookup_prefixes: dict[str, str] = {}
    for index in range(count):
        offset = entry_offset + index * 24
        name_offset = _api_set_u32(content, offset + 4, size)
        name_length = _api_set_u32(content, offset + 8, size)
        hashed_length = _api_set_u32(content, offset + 12, size)
        value_offset = _api_set_u32(content, offset + 16, size)
        value_count = _api_set_u32(content, offset + 20, size)
        name = _api_set_text(content, name_offset, name_length, size).casefold()
        contract = name if name.endswith(".dll") else f"{name}.dll"
        lookup_prefix = _api_set_text(
            content, name_offset, hashed_length, size
        ).casefold()
        if not _is_windows_api_set_contract(contract) or contract in contracts:
            raise DependencyObservationError(
                "dependencies.pe-api-set-schema-invalid",
                f"Windows API-set namespace contains invalid contract {contract!r}",
            )
        if (
            hashed_length > name_length
            or not name.startswith(lookup_prefix)
            or (
                lookup_prefix in lookup_prefixes
                and lookup_prefixes[lookup_prefix] != contract
            )
        ):
            raise DependencyObservationError(
                "dependencies.pe-api-set-schema-invalid",
                f"Windows API-set contract {contract!r} has an invalid lookup prefix",
            )
        if value_count == 0 or value_count > _MAX_OBSERVED_FILES:
            raise DependencyObservationError(
                "dependencies.pe-api-set-schema-invalid",
                f"Windows API-set contract {contract!r} has no bounded host mapping",
            )
        if value_offset > size or value_count * 20 > size - value_offset:
            raise DependencyObservationError(
                "dependencies.pe-api-set-schema-invalid",
                f"Windows API-set contract {contract!r} has a truncated value table",
            )
        values: dict[str, str] = {}
        for value_index in range(value_count):
            value_entry = value_offset + value_index * 20
            alias_offset = _api_set_u32(content, value_entry + 4, size)
            alias_length = _api_set_u32(content, value_entry + 8, size)
            host_offset = _api_set_u32(content, value_entry + 12, size)
            host_length = _api_set_u32(content, value_entry + 16, size)
            alias = _api_set_text(
                content, alias_offset, alias_length, size, allow_empty=True
            ).casefold()
            host = _api_set_text(
                content, host_offset, host_length, size, allow_empty=True
            ).casefold()
            if (
                (alias and re.fullmatch(r"[a-z0-9_.-]+\.[a-z0-9]+", alias) is None)
                or (host and re.fullmatch(r"[a-z0-9_.-]+\.[a-z0-9]+", host) is None)
                or alias in values
            ):
                raise DependencyObservationError(
                    "dependencies.pe-api-set-schema-invalid",
                    f"Windows API-set contract {contract!r} has invalid host map "
                    f"alias={alias!r}, host={host!r}",
                )
            values[alias] = host
        contracts[contract] = tuple(sorted(values.items()))
        lookup_prefixes[lookup_prefix] = contract
    if len(contracts) != count:
        raise DependencyObservationError(
            "dependencies.pe-api-set-schema-invalid",
            "Windows API-set namespace contract count is inconsistent",
        )
    return (
        namespace_version,
        tuple(sorted(contracts.items())),
        tuple(
            sorted((contract, prefix) for prefix, contract in lookup_prefixes.items())
        ),
    )


def _api_set_u32(content: bytes, offset: int, bound: int) -> int:
    if offset < 0 or offset + 4 > bound:
        raise DependencyObservationError(
            "dependencies.pe-api-set-schema-invalid",
            "Windows API-set namespace integer is out of bounds",
        )
    return int.from_bytes(content[offset : offset + 4], "little")


def _api_set_text(
    content: bytes,
    offset: int,
    length: int,
    bound: int,
    *,
    allow_empty: bool = False,
) -> str:
    if (
        offset < 0
        or length < 0
        or length % 2
        or offset > bound
        or length > bound - offset
        or (not allow_empty and length == 0)
    ):
        raise DependencyObservationError(
            "dependencies.pe-api-set-schema-invalid",
            "Windows API-set namespace string is out of bounds",
        )
    try:
        value = content[offset : offset + length].decode("utf-16-le")
    except UnicodeError as exc:
        raise DependencyObservationError(
            "dependencies.pe-api-set-schema-invalid",
            "Windows API-set namespace string is not valid UTF-16LE",
        ) from exc
    if (not allow_empty and not value) or "\x00" in value:
        raise DependencyObservationError(
            "dependencies.pe-api-set-schema-invalid",
            "Windows API-set namespace string is invalid",
        )
    return value


def _pe_closure(
    inspector: _PeInspector,
    seeds: Mapping[str, set[str]],
    roots: Sequence[Path],
    *,
    api_set_schema: _WindowsApiSetSchema,
) -> tuple[
    tuple[_PeImage, ...],
    tuple[tuple[str, str], ...],
    tuple[_PeApiSetBinding, ...],
    tuple[_PeUnavailableDelayImport, ...],
]:
    images: dict[str, _PeImage] = {}
    edges: set[tuple[str, str]] = set()
    api_set_bindings: set[_PeApiSetBinding] = set()
    unavailable_delay_imports: set[_PeUnavailableDelayImport] = set()
    directory_entries: dict[Path, dict[str, Path]] = {}
    pe_validity: dict[Path, bool] = {}
    pending = deque(sorted(seeds))
    while pending:
        raw_path = pending.popleft()
        try:
            path = Path(raw_path).resolve(strict=True)
        except OSError as exc:
            raise DependencyObservationError(
                "dependencies.pe-image-missing", f"PE image {raw_path} is unavailable"
            ) from exc
        path_string = str(path)
        if path_string in images:
            continue
        if path.is_symlink() or not path.is_file() or not _is_pe(path):
            raise DependencyObservationError(
                "dependencies.pe-image-invalid", f"PE image {path} is unsafe or invalid"
            )
        image_digest = _file_digest(path)
        imports = _inspect_pe_imports(inspector, path)
        if _file_digest(path) != image_digest:
            raise DependencyObservationError(
                "dependencies.pe-image-changed",
                f"PE image changed during inspection: {path}",
            )
        image = _PeImage(path_string, image_digest, imports)
        images[path_string] = image
        for name, delayed in imports:
            contract = name.casefold()
            virtual_contract = False
            schema_contract: str | None = None
            if _is_windows_api_set_contract(contract):
                try:
                    api_set_resolution = api_set_schema.resolve(
                        contract, importer=path_string
                    )
                except DependencyObservationError as exc:
                    if exc.code != "dependencies.pe-api-set-unresolved":
                        raise
                    # Older contracts can be represented by exact forwarder images
                    # in System32/downlevel instead of the current namespace map.
                else:
                    name = api_set_resolution.host
                    schema_contract = api_set_resolution.schema_contract
                    virtual_contract = True
            try:
                target = _resolve_pe_import(
                    name,
                    (path.parent, *roots),
                    directory_entries=directory_entries,
                    pe_validity=pe_validity,
                )
            except DependencyObservationError as exc:
                if (
                    delayed
                    and _is_windows_api_set_contract(contract)
                    and exc.code == "dependencies.pe-import-unresolved"
                ):
                    api_set_bindings.add(
                        _PeApiSetBinding(
                            path_string,
                            contract,
                            schema_contract,
                            name,
                            None,
                            True,
                        )
                    )
                    continue
                if (
                    delayed
                    and not virtual_contract
                    and not _is_windows_api_set_contract(contract)
                    and exc.code == "dependencies.pe-import-unresolved"
                ):
                    unavailable_delay_imports.add(
                        _PeUnavailableDelayImport(path_string, name)
                    )
                    continue
                raise DependencyObservationError(
                    exc.code,
                    f"PE image {path_string} has unresolved import {name!r}",
                ) from exc
            if virtual_contract:
                api_set_bindings.add(
                    _PeApiSetBinding(
                        path_string,
                        contract,
                        schema_contract,
                        name,
                        target,
                        delayed,
                    )
                )
            else:
                edges.add((path_string, target))
            if target not in images:
                pending.append(target)
    return (
        tuple(images[path] for path in sorted(images)),
        tuple(sorted(edges)),
        tuple(
            sorted(
                api_set_bindings,
                key=lambda item: (
                    item.importer_path,
                    item.contract,
                    item.schema_contract or "",
                    item.expected_host,
                    item.target_path or "",
                    item.delayed,
                ),
            )
        ),
        tuple(
            sorted(
                unavailable_delay_imports,
                key=lambda item: (item.importer_path, item.name),
            )
        ),
    )


def _resolve_pe_import(
    name: str,
    roots: Sequence[Path],
    *,
    directory_entries: dict[Path, dict[str, Path]],
    pe_validity: dict[Path, bool] | None = None,
) -> str:
    validity = {} if pe_validity is None else pe_validity
    for raw_root in roots:
        try:
            root = raw_root.resolve(strict=True)
        except OSError:
            continue
        if not root.is_dir() or root.is_symlink():
            continue
        entries = directory_entries.get(root)
        if entries is None:
            entries = {}
            try:
                for index, child in enumerate(root.iterdir()):
                    if index >= _MAX_OBSERVED_FILES:
                        raise DependencyObservationError(
                            "dependencies.file-limit",
                            "PE library search directory exceeds the observation limit",
                        )
                    key = child.name.casefold()
                    if key in entries and entries[key] != child:
                        raise DependencyObservationError(
                            "dependencies.pe-import-ambiguous",
                            "PE library search directory has ambiguous name "
                            f"{child.name}",
                        )
                    entries[key] = child
            except OSError as exc:
                raise DependencyObservationError(
                    "dependencies.pe-root-unreadable",
                    f"cannot inspect PE library search directory {root}",
                ) from exc
            directory_entries[root] = entries
        candidate = entries.get(name.casefold())
        if candidate is None:
            continue
        try:
            exact = candidate.resolve(strict=True)
        except OSError:
            continue
        is_pe = validity.get(exact)
        if is_pe is None:
            is_pe = not exact.is_symlink() and exact.is_file() and _is_pe(exact)
            validity[exact] = is_pe
        if not is_pe:
            continue
        return str(exact)
    raise DependencyObservationError(
        "dependencies.pe-import-unresolved",
        f"no exact PE image satisfies required import {name!r}",
    )


def _pe_ref(image: _PeImage) -> str:
    identity = canonical_identity({"pe_path": image.path, "pe_digest": image.digest})
    return f"urn:literate-ai:pe:{identity.digest}"


def _pe_api_set_ref(
    contract: str,
    *,
    schema: _WindowsApiSetSchema,
    schema_contract: str | None,
    expected_host: str,
    target_ref: str | None,
) -> str:
    identity = canonical_identity(
        {
            "schema": "urn:literate-ai:windows-api-set-resolution@1",
            "contract": contract,
            "schema_contract": schema_contract,
            "expected_host": expected_host,
            "api_set_mapping": schema.mapping_identity,
            "target_ref": target_ref,
        }
    )
    return f"urn:literate-ai:windows-api-set:{identity.digest}"


def _pe_api_set_component(
    contract: str,
    ref: str,
    *,
    schema: _WindowsApiSetSchema,
    schema_contract: str | None,
    expected_host: str,
    target_path: str | None,
    scopes: Sequence[str],
    delayed: bool,
) -> dict[str, object]:
    digest = schema.digest.removeprefix("sha256:")
    return {
        "type": "framework",
        "bom-ref": ref,
        "name": contract,
        "version": schema.digest.replace(":", "-"),
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "system"},
            *(
                {"name": "literate-ai:dependency-scope", "value": scope}
                for scope in sorted(set(scopes) | {"runtime", "system"})
            ),
            {"name": "literate-ai:pe-api-set-contract", "value": contract},
            *(
                ()
                if schema_contract is None
                else (
                    {
                        "name": "literate-ai:pe-api-set-schema-contract",
                        "value": schema_contract,
                    },
                )
            ),
            *(
                ()
                if target_path is None
                else (
                    {
                        "name": "literate-ai:pe-api-set-resolved-host-path",
                        "value": target_path,
                    },
                )
            ),
            {
                "name": "literate-ai:pe-api-set-expected-host",
                "value": expected_host,
            },
            {
                "name": "literate-ai:pe-api-set-host-available",
                "value": str(target_path is not None).lower(),
            },
            {
                "name": "literate-ai:pe-delay-import-only",
                "value": str(delayed).lower(),
            },
            {
                "name": "literate-ai:pe-api-set-schema-path",
                "value": schema.path,
            },
            {
                "name": "literate-ai:pe-api-set-mapping-identity",
                "value": schema.mapping_identity,
            },
        ],
    }


def _pe_component(
    image: _PeImage,
    ref: str,
    *,
    scopes: Sequence[str],
    unavailable_delay_imports: Sequence[str] = (),
) -> dict[str, object]:
    digest = image.digest.removeprefix("sha256:")
    return {
        "type": "file",
        "bom-ref": ref,
        "name": Path(image.path).name,
        "version": image.digest.replace(":", "-"),
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "system"},
            *(
                {"name": "literate-ai:dependency-scope", "value": scope}
                for scope in sorted(set(scopes) | {"system"})
            ),
            {"name": "literate-ai:pe-path", "value": image.path},
            *(
                {
                    "name": "literate-ai:pe-import-edge",
                    "value": canonical_json_bytes(
                        {"name": name, "delay_load": delayed}
                    ).decode("utf-8"),
                }
                for name, delayed in image.imports
            ),
            *(
                {
                    "name": "literate-ai:pe-delay-import-unavailable",
                    "value": name,
                }
                for name in sorted(set(unavailable_delay_imports))
            ),
        ],
    }


@dataclass(frozen=True, slots=True)
class _NpmDependencyDeclaration:
    name: str
    target_name: str
    selector: str
    kind: str
    optional: bool


@dataclass(frozen=True, slots=True)
class NpmDistributionAuthority:
    """Exact root tree and resolved package closure for one npm implementation."""

    package_root: str
    version: str
    directories: tuple[str, ...]
    files: tuple[tuple[str, int, str], ...]
    package_roots: tuple[str, ...]
    root_package_ref: str
    dependency_graph: HostDependencyObservation

    def __post_init__(self) -> None:
        root = Path(self.package_root)
        if not root.is_absolute() or str(root) != self.package_root:
            raise ValueError("npm distribution root must be one absolute host path")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("npm distribution version must be nonempty")
        if self.directories != tuple(sorted(set(self.directories))):
            raise ValueError("npm distribution directories must be sorted and unique")
        for path in self.directories:
            relative = PurePosixPath(path)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.as_posix() != path
            ):
                raise ValueError("npm distribution directory path must be canonical")
        file_paths = tuple(record[0] for record in self.files)
        if file_paths != tuple(sorted(set(file_paths))):
            raise ValueError("npm distribution files must be sorted and unique")
        for path, size, digest in self.files:
            relative = PurePosixPath(path)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.as_posix() != path
            ):
                raise ValueError("npm distribution file path must be canonical")
            if type(size) is not int or size < 0:
                raise ValueError("npm distribution file size must be nonnegative")
            if not isinstance(digest, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", digest
            ):
                raise ValueError("npm distribution file digest must be sha256")
        if self.package_roots != tuple(sorted(set(self.package_roots))):
            raise ValueError("npm distribution package roots must be sorted and unique")
        if self.package_root not in self.package_roots:
            raise ValueError("npm distribution roots must include the npm package")
        for path in self.package_roots:
            try:
                resolved = Path(path).resolve(strict=True)
            except OSError as exc:
                raise ValueError(
                    "npm distribution package root is unavailable"
                ) from exc
            if (
                not Path(path).is_absolute()
                or str(resolved) != path
                or not resolved.is_dir()
            ):
                raise ValueError(
                    "npm distribution package roots must be resolved directories"
                )
        if not isinstance(self.root_package_ref, str) or not self.root_package_ref:
            raise ValueError("npm distribution root package ref must be nonempty")
        if not isinstance(self.dependency_graph, HostDependencyObservation):
            raise TypeError("npm distribution dependency graph must be observed")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/npm-distribution-authority@1",
                "package_root": self.package_root,
                "version": self.version,
                "directories": list(self.directories),
                "files": [
                    {"path": path, "size": size, "digest": digest}
                    for path, size, digest in self.files
                ],
                "package_roots": list(self.package_roots),
                "root_package_ref": self.root_package_ref,
                "dependency_graph": {
                    "components": list(self.dependency_graph.components),
                    "edges": [list(edge) for edge in self.dependency_graph.edges],
                },
            }
        )


NpmDependencyResolver = Callable[[Path, str], Path | None]
NpmLiteralDependencyResolver = Callable[
    [tuple[tuple[Path, str], ...]], Mapping[tuple[Path, str], Path | None]
]


def _npm_local_internal_import_targets(
    manifest: Mapping[str, object], specifier: str
) -> tuple[str, ...]:
    """Return every local target for one exact package ``imports`` key."""

    imports = manifest.get("imports")
    if not isinstance(imports, Mapping) or specifier not in imports:
        return ()
    targets: set[str] = set()
    valid = True

    def collect(value: object) -> None:
        nonlocal valid
        if isinstance(value, str):
            if not value.startswith("./"):
                valid = False
            else:
                targets.add(value)
        elif isinstance(value, Mapping):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)
        else:
            valid = False

    collect(imports[specifier])
    return tuple(sorted(targets)) if valid and targets else ()


def _npm_entrypoint_specifiers(manifest: Mapping[str, object]) -> tuple[str, ...]:
    values: set[str] = {"index.js"}

    def collect(value: object) -> None:
        if isinstance(value, str):
            values.add(value)
        elif isinstance(value, Mapping):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(manifest.get("main"))
    exports = manifest.get("exports")
    if isinstance(exports, Mapping) and any(
        isinstance(key, str) and key.startswith(".") for key in exports
    ):
        collect(exports.get("."))
    else:
        collect(exports)
    collect(manifest.get("bin"))
    imports = manifest.get("imports")
    if isinstance(imports, Mapping):
        for key in imports:
            if isinstance(key, str):
                values.update(_npm_local_internal_import_targets(manifest, key))
    return tuple(sorted(values))


def _npm_runtime_javascript_sources(
    package_root: Path,
    manifest: Mapping[str, object],
    sources: Mapping[Path, str],
) -> frozenset[Path]:
    """Conservatively follow literal relative imports from public entrypoints."""

    by_relative = {path.relative_to(package_root).as_posix(): path for path in sources}
    pending: deque[Path] = deque()
    selected: set[Path] = set()

    def add(specifier: str, parent: PurePosixPath | None = None) -> None:
        clean = specifier.split("?", 1)[0].split("#", 1)[0]
        base = PurePosixPath() if parent is None else parent
        normalized = posixpath.normpath((base / clean).as_posix())
        if (
            normalized == ".."
            or normalized.startswith("../")
            or PurePosixPath(normalized).is_absolute()
            or PureWindowsPath(clean).is_absolute()
            or clean.startswith("file:")
        ):
            raise DependencyObservationError(
                "dependencies.npm-entrypoint-path-unsafe",
                f"npm package {package_root} declares an entrypoint outside its "
                "hashed package root",
            )
        if "*" in normalized:
            matches = (
                path
                for relative, path in by_relative.items()
                if PurePosixPath(relative).match(normalized)
            )
        else:
            candidates = [normalized]
            if not PurePosixPath(normalized).suffix:
                candidates.extend(
                    f"{normalized}{suffix}" for suffix in (".js", ".cjs", ".mjs")
                )
                candidates.extend(
                    f"{normalized}/index{suffix}" for suffix in (".js", ".cjs", ".mjs")
                )
            matches = (by_relative[item] for item in candidates if item in by_relative)
        for path in matches:
            if path not in selected:
                selected.add(path)
                pending.append(path)

    for specifier in _npm_entrypoint_specifiers(manifest):
        add(specifier)
    while pending:
        source = pending.popleft()
        parent = PurePosixPath(source.relative_to(package_root).as_posix()).parent
        for specifier in javascript_import_specifiers(sources[source]):
            if specifier.startswith("."):
                add(specifier, parent)
    return frozenset(selected)


def _npm_package_graph(
    root: Path,
    *,
    native_predicate: Callable[[Path], bool],
    resolution_root: Path | None = None,
    dependency_resolver: NpmDependencyResolver | None = None,
    literal_dependency_resolver: NpmLiteralDependencyResolver | None = None,
    builtin_modules: frozenset[str] = NODE_BUILTIN_MODULES,
) -> tuple[HostDependencyObservation, dict[str, str], str]:
    root_package_root = root.resolve(strict=True)
    graph_root = (
        root_package_root
        if resolution_root is None
        else resolution_root.resolve(strict=True)
    )
    if root_package_root != graph_root and graph_root not in root_package_root.parents:
        raise DependencyObservationError(
            "dependencies.npm-dependency-path-unsafe",
            "npm root package escaped its dependency resolution boundary",
        )
    packages: dict[Path, tuple[str, str, str, Mapping[str, object]]] = {}
    components: dict[str, dict[str, object]] = {}
    manifest_digests: dict[Path, str] = {}
    observed_file_count = 0
    observed_byte_count = 0
    observed_manifest_bytes = 0
    literal_requests: set[tuple[Path, str]] = set()

    def load(package_root: Path) -> tuple[str, str, str, Mapping[str, object]]:
        nonlocal observed_manifest_bytes
        resolved_root = package_root.resolve(strict=True)
        existing = packages.get(resolved_root)
        if existing is not None:
            return existing
        if len(packages) >= _MAX_NPM_GRAPH_PACKAGES:
            raise DependencyObservationError(
                "dependencies.npm-package-limit",
                "reachable npm dependency graph exceeds the limit",
            )
        manifest = resolved_root / "package.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise DependencyObservationError(
                "dependencies.npm-manifest-invalid",
                f"reachable npm package {resolved_root} lacks a safe manifest",
            )
        try:
            manifest_content = _stable_file_bytes(
                manifest, limit=_MAX_NPM_OWNERSHIP_MANIFEST_BYTES + 1
            )
            if len(manifest_content) > _MAX_NPM_OWNERSHIP_MANIFEST_BYTES:
                raise DependencyObservationError(
                    "dependencies.npm-manifest-size-invalid",
                    f"npm manifest exceeds the byte limit: {manifest}",
                )
            observed_manifest_bytes += len(manifest_content)
            if observed_manifest_bytes > _MAX_NPM_GRAPH_MANIFEST_BYTES:
                raise DependencyObservationError(
                    "dependencies.npm-graph-budget-exceeded",
                    "reachable npm manifests exceed the aggregate byte limit",
                )
            value = json.loads(manifest_content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DependencyObservationError(
                "dependencies.npm-manifest-invalid", f"invalid npm manifest {manifest}"
            ) from exc
        if not isinstance(value, Mapping):
            raise DependencyObservationError(
                "dependencies.npm-manifest-invalid", f"invalid npm manifest {manifest}"
            )
        name, version = value.get("name"), value.get("version")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not name
            or not version
            or _NPM_PACKAGE_NAME.fullmatch(name) is None
        ):
            raise DependencyObservationError(
                "dependencies.npm-version-missing",
                f"npm package {resolved_root} lacks a valid name or exact version",
            )
        semantic_candidate = version[1:] if version.startswith("v") else version
        try:
            parsed_version = semantic_version.Version(semantic_candidate)
        except ValueError as exc:
            raise DependencyObservationError(
                "dependencies.npm-version-invalid",
                f"npm package {name!r} has an invalid exact version",
            ) from exc
        if str(parsed_version) != semantic_candidate or (
            version.startswith("v") and not semantic_candidate[:1].isdigit()
        ):
            raise DependencyObservationError(
                "dependencies.npm-version-invalid",
                f"npm package {name!r} has an invalid exact version",
            )
        ref = _npm_component_ref(resolved_root, name, version)
        package = (name, version, ref, value)
        packages[resolved_root] = package
        manifest_digests[resolved_root] = (
            "sha256:" + hashlib.sha256(manifest_content).hexdigest()
        )
        components[ref] = {
            "type": "library",
            "bom-ref": ref,
            "name": name,
            "version": version,
            "purl": f"pkg:npm/{name.replace('@', '%40')}@{version}",
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "toolchain"},
                {"name": "literate-ai:dependency-scope", "value": "build"},
                {
                    "name": "literate-ai:npm-package-root",
                    "value": str(resolved_root),
                },
            ],
        }
        return package

    root_package = load(root_package_root)
    edges: set[tuple[str, str]] = set()
    pending = deque((root_package_root,))
    traversed: set[Path] = set()
    while pending:
        package_root = pending.popleft()
        if package_root in traversed:
            continue
        traversed.add(package_root)
        package_name, _version, source_ref, manifest = load(package_root)
        for declaration in _npm_dependency_declarations(manifest):
            target_root = (
                _resolve_installed_npm_dependency(
                    package_root, graph_root, declaration.name
                )
                if dependency_resolver is None
                else dependency_resolver(package_root, declaration.name)
            )
            if target_root is not None and not isinstance(target_root, Path):
                raise DependencyObservationError(
                    "dependencies.npm-dependency-path-unsafe",
                    "npm dependency resolver returned an invalid package root",
                )
            if target_root is None:
                if declaration.optional:
                    _record_npm_edge_property(
                        components[source_ref], declaration, target_ref=None
                    )
                    continue
                raise DependencyObservationError(
                    "dependencies.npm-dependency-missing",
                    f"installed npm package {package_name} is missing "
                    f"{declaration.kind} dependency {declaration.name}",
                )
            target_package = load(target_root)
            if target_package[0] != declaration.target_name:
                raise DependencyObservationError(
                    "dependencies.npm-dependency-name-mismatch",
                    f"installed npm dependency {declaration.name!r} declares package "
                    f"name {target_package[0]!r}, expected "
                    f"{declaration.target_name!r}",
                )
            _require_npm_selector(
                declaration.selector,
                installed_version=target_package[1],
                dependency_name=declaration.name,
            )
            target_ref = target_package[2]
            _record_npm_edge_property(
                components[source_ref], declaration, target_ref=target_ref
            )
            if target_ref != source_ref:
                edges.add((source_ref, target_ref))
                pending.append(target_root)

    native_owners: dict[str, str] = {}
    for package_root in sorted(traversed):
        ref = packages[package_root][2]
        manifest = package_root / "package.json"
        if _file_digest(manifest) != manifest_digests[package_root]:
            raise DependencyObservationError(
                "dependencies.npm-manifest-changed",
                f"reachable npm manifest changed during observation: {manifest}",
            )
        files = _npm_package_own_files(package_root)
        observed_file_count += len(files)
        if observed_file_count > _MAX_NPM_GRAPH_FILES:
            raise DependencyObservationError(
                "dependencies.npm-graph-budget-exceeded",
                "reachable npm graph exceeds the aggregate file limit",
            )
        records: list[tuple[str, str]] = []
        javascript_sources: dict[Path, str] = {}
        for path in files:
            content = _stable_file_bytes(path, limit=_MAX_NPM_FILE_BYTES + 1)
            if len(content) > _MAX_NPM_FILE_BYTES:
                raise DependencyObservationError(
                    "dependencies.npm-graph-budget-exceeded",
                    f"reachable npm file exceeds the per-file byte limit: {path}",
                )
            observed_byte_count += len(content)
            if observed_byte_count > _MAX_NPM_GRAPH_BYTES:
                raise DependencyObservationError(
                    "dependencies.npm-graph-budget-exceeded",
                    "reachable npm graph exceeds the aggregate byte limit",
                )
            if name_or_magic_is_native_or_wasm(path.name, content[:8]):
                raise DependencyObservationError(
                    "dependencies.npm-native-payload-unsupported",
                    f"reachable npm implementation package contains a native or "
                    f"WebAssembly payload: {path}",
                )
            if path.suffix.casefold() in {".cjs", ".js", ".mjs"}:
                try:
                    javascript_source = content.decode("utf-8")
                except UnicodeError as exc:
                    raise DependencyObservationError(
                        "dependencies.npm-javascript-source-invalid",
                        f"reachable npm JavaScript source is not UTF-8: {path}",
                    ) from exc
                if has_computed_dynamic_import(javascript_source):
                    raise DependencyObservationError(
                        "dependencies.npm-computed-dynamic-import-unsupported",
                        f"reachable npm JavaScript source uses a computed dynamic "
                        f"import that cannot be bound statically: {path}",
                    )
                javascript_sources[path] = javascript_source
            records.append(
                (
                    path.relative_to(package_root).as_posix(),
                    "sha256:" + hashlib.sha256(content).hexdigest(),
                )
            )
        file_records = tuple(records)
        tree_identity = canonical_identity(
            {
                "npm_package": packages[package_root][0],
                "version": packages[package_root][1],
                "files": file_records,
            }
        )
        components[ref]["hashes"] = [
            {"alg": "SHA-256", "content": tree_identity.digest}
        ]
        properties = components[ref]["properties"]
        assert isinstance(properties, list)
        properties.append(
            {
                "name": "literate-ai:npm-installed-tree-identity",
                "value": tree_identity.uri,
            }
        )
        for path in files:
            if native_predicate(path):
                native_owners[str(path.resolve())] = ref
        repeated_records = tuple(
            (path.relative_to(package_root).as_posix(), _npm_bounded_file_digest(path))
            for path in _npm_package_own_files(package_root)
        )
        if repeated_records != file_records:
            raise DependencyObservationError(
                "dependencies.npm-package-changed",
                f"reachable npm package changed during observation: {package_root}",
            )
        runtime_sources = _npm_runtime_javascript_sources(
            package_root, packages[package_root][3], javascript_sources
        )
        for path, javascript_source in javascript_sources.items():
            specifiers = set(javascript_dynamic_import_specifiers(javascript_source))
            if path in runtime_sources:
                specifiers.update(javascript_esm_import_specifiers(javascript_source))
            for specifier in specifiers:
                if (
                    specifier.startswith(("/", "file:"))
                    or PureWindowsPath(specifier).is_absolute()
                ):
                    raise DependencyObservationError(
                        "dependencies.npm-literal-import-path-unsafe",
                        f"reachable npm JavaScript source uses an absolute literal "
                        f"import: {path}",
                    )
                if specifier.startswith("."):
                    relative_specifier = specifier.split("?", 1)[0].split("#", 1)[0]
                    candidate = Path(os.path.abspath(path.parent / relative_specifier))
                    if not candidate.is_relative_to(package_root):
                        raise DependencyObservationError(
                            "dependencies.npm-literal-import-path-unsafe",
                            f"reachable npm JavaScript source uses a literal import "
                            f"outside its hashed package root: {path}",
                        )
                imported = javascript_package_name(
                    specifier, builtin_modules=builtin_modules
                )
                if imported is not None:
                    internal_targets = (
                        _npm_local_internal_import_targets(
                            packages[package_root][3], imported
                        )
                        if imported.startswith("#")
                        else ()
                    )
                    if (imported.startswith("#") and not internal_targets) or (
                        not imported.startswith("#") and specifier != imported
                    ):
                        raise DependencyObservationError(
                            "dependencies.npm-esm-subpath-unsupported",
                            f"reachable npm JavaScript source imports ESM package "
                            f"subpath {specifier!r} without an exact entrypoint "
                            f"closure: {path}",
                        )
                    literal_requests.add((package_root, imported))

    ordered_literal_requests = tuple(
        sorted(literal_requests, key=lambda item: (str(item[0]), item[1]))
    )
    if literal_dependency_resolver is None:
        literal_resolutions = {
            request: (
                _resolve_installed_npm_dependency(request[0], graph_root, request[1])
                if dependency_resolver is None
                else dependency_resolver(*request)
            )
            for request in ordered_literal_requests
        }
    else:
        literal_resolutions = literal_dependency_resolver(ordered_literal_requests)
    if not isinstance(literal_resolutions, Mapping) or set(literal_resolutions) != set(
        ordered_literal_requests
    ):
        raise DependencyObservationError(
            "dependencies.npm-literal-resolution-invalid",
            "npm literal dependency resolver returned incomplete evidence",
        )
    for request in ordered_literal_requests:
        target = literal_resolutions[request]
        declared_targets = {
            declaration.name: declaration.target_name
            for declaration in _npm_dependency_declarations(packages[request[0]][3])
        }
        if target is None:
            raise DependencyObservationError(
                "dependencies.npm-literal-import-unresolved",
                f"reachable npm package {packages[request[0]][0]} imports "
                f"unresolved literal package {request[1]}",
            )
        if not isinstance(target, Path):
            raise DependencyObservationError(
                "dependencies.npm-literal-resolution-invalid",
                "npm literal dependency resolver returned an invalid package root",
            )
        try:
            target_root = target.resolve(strict=True)
        except OSError as exc:
            raise DependencyObservationError(
                "dependencies.npm-literal-resolution-invalid",
                "npm literal dependency resolver returned an unavailable package root",
            ) from exc
        if target_root not in traversed or (
            not request[1].startswith("#")
            and packages[target_root][0] != declared_targets.get(request[1], request[1])
        ):
            raise DependencyObservationError(
                "dependencies.npm-literal-import-unbound",
                f"reachable npm package {packages[request[0]][0]} imports literal "
                f"package {request[1]} outside the hashed dependency graph",
            )
    return (
        _normalized_observation(tuple(components.values()), tuple(edges)),
        native_owners,
        root_package[2],
    )


def _npm_bounded_file_digest(path: Path) -> str:
    content = _stable_file_bytes(path, limit=_MAX_NPM_FILE_BYTES + 1)
    if len(content) > _MAX_NPM_FILE_BYTES:
        raise DependencyObservationError(
            "dependencies.npm-graph-budget-exceeded",
            f"reachable npm file exceeds the per-file byte limit: {path}",
        )
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _npm_dependency_declarations(
    manifest: Mapping[str, object],
) -> tuple[_NpmDependencyDeclaration, ...]:
    fields: dict[str, Mapping[str, object]] = {}
    for field in ("dependencies", "optionalDependencies", "peerDependencies"):
        raw = manifest.get(field, {})
        if not isinstance(raw, Mapping):
            raise DependencyObservationError(
                "dependencies.npm-declarations-invalid",
                f"npm manifest {field} must be an object",
            )
        fields[field] = raw
    raw_peer_meta = manifest.get("peerDependenciesMeta", {})
    if not isinstance(raw_peer_meta, Mapping):
        raise DependencyObservationError(
            "dependencies.npm-peer-metadata-invalid",
            "npm peerDependenciesMeta must be an object",
        )
    peer_optional: dict[str, bool] = {}
    for name, raw in raw_peer_meta.items():
        if (
            not isinstance(name, str)
            or _NPM_PACKAGE_NAME.fullmatch(name) is None
            or not isinstance(raw, Mapping)
        ):
            raise DependencyObservationError(
                "dependencies.npm-peer-metadata-invalid",
                "npm peer dependency metadata entry is invalid",
            )
        optional = raw.get("optional", False)
        if not isinstance(optional, bool):
            raise DependencyObservationError(
                "dependencies.npm-peer-metadata-invalid",
                "npm peer dependency optionality must be boolean",
            )
        peer_optional[name] = optional

    result: list[_NpmDependencyDeclaration] = []
    optional_names = set(fields["optionalDependencies"])
    for field, kind in (
        ("dependencies", "runtime"),
        ("optionalDependencies", "optional"),
        ("peerDependencies", "peer"),
    ):
        for name, selector in sorted(fields[field].items()):
            if (
                not isinstance(name, str)
                or _NPM_PACKAGE_NAME.fullmatch(name) is None
                or not isinstance(selector, str)
            ):
                raise DependencyObservationError(
                    "dependencies.npm-declaration-invalid",
                    "npm dependency name and selector must be non-empty strings",
                )
            if field == "dependencies" and name in optional_names:
                continue
            target_name, target_selector = _npm_selector_target(name, selector)
            result.append(
                _NpmDependencyDeclaration(
                    name,
                    target_name,
                    target_selector,
                    kind,
                    field == "optionalDependencies"
                    or (field == "peerDependencies" and peer_optional.get(name, False)),
                )
            )
    return tuple(result)


def _npm_selector_target(name: str, selector: str) -> tuple[str, str]:
    if not selector.startswith("npm:"):
        return name, selector
    target_and_selector = selector.removeprefix("npm:")
    separator = target_and_selector.rfind("@")
    if separator == len(target_and_selector) - 1:
        raise DependencyObservationError(
            "dependencies.npm-selector-unsupported",
            f"npm dependency {name!r} uses a malformed package alias",
        )
    has_selector = separator > 0
    target_name = (
        target_and_selector[:separator] if has_selector else target_and_selector
    )
    target_selector = target_and_selector[separator + 1 :] if has_selector else "*"
    if _NPM_PACKAGE_NAME.fullmatch(target_name) is None:
        raise DependencyObservationError(
            "dependencies.npm-selector-unsupported",
            f"npm dependency {name!r} uses an invalid package alias target",
        )
    return target_name, target_selector


def _require_npm_selector(
    selector: str, *, installed_version: str, dependency_name: str
) -> None:
    if not selector.strip() or selector.startswith(
        ("file:", "git:", "git+", "http:", "https:", "link:", "workspace:")
    ):
        raise DependencyObservationError(
            "dependencies.npm-selector-unsupported",
            f"npm dependency {dependency_name!r} uses an unsupported selector",
        )
    normalized_selector = re.sub(
        r"(?P<operator><=|>=|<|>|=|~|\^)\s+(?=[vV]?\d)",
        r"\g<operator>",
        selector,
    )
    try:
        specification = semantic_version.NpmSpec(normalized_selector)
        version = semantic_version.Version(installed_version)
    except ValueError as exc:
        raise DependencyObservationError(
            "dependencies.npm-selector-unsupported",
            f"npm dependency {dependency_name!r} uses an unsupported selector",
        ) from exc
    if not specification.match(version):
        raise DependencyObservationError(
            "dependencies.npm-selector-mismatch",
            f"installed npm dependency {dependency_name!r} at {installed_version} "
            f"does not satisfy {selector!r}",
        )


def _record_npm_edge_property(
    component: dict[str, object],
    declaration: _NpmDependencyDeclaration,
    *,
    target_ref: str | None,
) -> None:
    properties = component.get("properties")
    if not isinstance(properties, list):
        raise DependencyObservationError(
            "dependencies.npm-component-invalid",
            "npm component lacks dependency evidence properties",
        )
    value = json.dumps(
        {
            "kind": declaration.kind,
            "name": declaration.name,
            "optional": declaration.optional,
            "selector": declaration.selector,
            "target_name": declaration.target_name,
            "target_ref": target_ref,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    property_value = {
        "name": "literate-ai:npm-dependency-edge",
        "value": value,
    }
    if property_value not in properties:
        properties.append(property_value)


def _resolve_installed_npm_dependency(
    package_root: Path, graph_root: Path, name: str
) -> Path | None:
    if _NPM_PACKAGE_NAME.fullmatch(name) is None:
        raise DependencyObservationError(
            "dependencies.npm-declaration-invalid",
            f"npm dependency name {name!r} is invalid",
        )
    parts = name.split("/") if name.startswith("@") else [name]
    try:
        graph_root = graph_root.resolve(strict=True)
        current = package_root.resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.npm-dependency-path-unsafe",
            "npm dependency graph root is unavailable",
        ) from exc
    if current != graph_root and graph_root not in current.parents:
        raise DependencyObservationError(
            "dependencies.npm-dependency-path-unsafe",
            f"npm package {current} escaped dependency graph {graph_root}",
        )
    while True:
        candidate = current.joinpath("node_modules", *parts)
        resolved = _safe_installed_npm_candidate(candidate, graph_root)
        if resolved is not None:
            return resolved
        if current == graph_root or graph_root not in current.parents:
            return None
        current = current.parent


def _safe_installed_npm_candidate(candidate: Path, graph_root: Path) -> Path | None:
    """Resolve one expected npm location without accepting links or graph escape."""

    try:
        relative = candidate.relative_to(graph_root)
    except ValueError as exc:
        raise DependencyObservationError(
            "dependencies.npm-dependency-path-unsafe",
            f"npm dependency candidate escaped its graph: {candidate}",
        ) from exc
    current = graph_root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DependencyObservationError(
                "dependencies.npm-dependency-path-unsafe",
                f"npm dependency path is unavailable: {current}",
            ) from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x0400:
            raise DependencyObservationError(
                "dependencies.npm-dependency-path-unsafe",
                f"npm dependency path contains a link or junction: {current}",
            )
    manifest = candidate / "package.json"
    try:
        manifest_metadata = manifest.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.npm-dependency-path-unsafe",
            f"npm dependency manifest is unavailable: {manifest}",
        ) from exc
    manifest_attributes = getattr(manifest_metadata, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(manifest_metadata.st_mode)
        or manifest_attributes & 0x0400
        or not stat.S_ISREG(manifest_metadata.st_mode)
    ):
        raise DependencyObservationError(
            "dependencies.npm-dependency-path-unsafe",
            f"npm dependency manifest is not a safe regular file: {manifest}",
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.npm-dependency-path-unsafe",
            f"npm dependency candidate is unavailable: {candidate}",
        ) from exc
    if resolved != candidate or (
        resolved != graph_root and graph_root not in resolved.parents
    ):
        raise DependencyObservationError(
            "dependencies.npm-dependency-path-unsafe",
            f"npm dependency candidate escaped or aliased its graph: {candidate}",
        )
    return resolved


def _safe_npm_package_tree(
    root: Path, *, include_node_modules: bool
) -> tuple[tuple[str, ...], tuple[Path, ...]]:
    """List one npm tree without following aliases, links, or reparse points."""

    requested_root = Path(os.path.abspath(root))
    try:
        root_metadata = requested_root.lstat()
        root = requested_root.resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.npm-package-path-unsafe",
            f"npm package root is unavailable: {root}",
        ) from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or getattr(root_metadata, "st_file_attributes", 0) & 0x0400
        or root != requested_root
    ):
        raise DependencyObservationError(
            "dependencies.npm-package-path-unsafe",
            f"npm package root is a link, junction, or non-directory: {root}",
        )

    def require_contained(path: Path, *, directory: bool) -> None:
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise DependencyObservationError(
                "dependencies.npm-package-path-unsafe",
                f"npm package path is unavailable: {path}",
            ) from exc
        expected_type = (
            stat.S_ISDIR(metadata.st_mode)
            if directory
            else stat.S_ISREG(metadata.st_mode)
        )
        if (
            not expected_type
            or stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & 0x0400
            or resolved != path
            or (resolved != root and root not in resolved.parents)
        ):
            raise DependencyObservationError(
                "dependencies.npm-package-path-unsafe",
                f"npm package path is a link, junction, alias, or escape: {path}",
            )

    directories: list[str] = []
    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        require_contained(current, directory=True)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            child = current / name
            require_contained(child, directory=True)
            if name == "node_modules" and not include_node_modules:
                continue
            kept_directories.append(name)
            directories.append(child.relative_to(root).as_posix())
            if (
                include_node_modules
                and len(files) + len(directories) > _MAX_NPM_GRAPH_FILES
            ):
                raise DependencyObservationError(
                    "dependencies.file-limit",
                    "npm package file observation limit exceeded",
                )
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            path = current / name
            require_contained(path, directory=False)
            files.append(path)
            observed_count = (
                len(files) + len(directories) if include_node_modules else len(files)
            )
            observed_limit = (
                _MAX_NPM_GRAPH_FILES if include_node_modules else _MAX_OBSERVED_FILES
            )
            if observed_count > observed_limit:
                raise DependencyObservationError(
                    "dependencies.file-limit",
                    "npm package file observation limit exceeded",
                )
    return tuple(sorted(directories)), tuple(sorted(files))


def _npm_package_own_files(root: Path) -> tuple[Path, ...]:
    return _safe_npm_package_tree(root, include_node_modules=False)[1]


def observe_npm_distribution(
    cli_path: Path,
    *,
    dependency_resolver: NpmDependencyResolver | None = None,
    literal_dependency_resolver: NpmLiteralDependencyResolver | None = None,
    builtin_modules: frozenset[str] = NODE_BUILTIN_MODULES,
) -> NpmDistributionAuthority:
    """Bind the complete npm package tree executed through one resolved CLI.

    The CLI can come from PATH or an operating-system package layout.  This binds
    the complete npm root tree, the manifest-declared production graph, and bare
    package names in statically quoted import/require forms. Computed, dynamic, and
    absolute module loads remain a trusted-tool boundary. This deliberately makes
    no claim that npm and Node share an installer or owner.
    """

    requested_cli = Path(os.path.abspath(cli_path))
    try:
        cli_metadata = requested_cli.lstat()
        cli = requested_cli.resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.npm-distribution-path-unsafe",
            f"npm CLI is unavailable: {requested_cli}",
        ) from exc
    if (
        requested_cli != cli
        or cli.name != "npm-cli.js"
        or not stat.S_ISREG(cli_metadata.st_mode)
        or stat.S_ISLNK(cli_metadata.st_mode)
        or getattr(cli_metadata, "st_file_attributes", 0) & 0x0400
    ):
        raise DependencyObservationError(
            "dependencies.npm-distribution-path-unsafe",
            "npm CLI must be one resolved regular npm-cli.js file",
        )
    package_root = cli.parent.parent
    manifest = package_root / "package.json"
    try:
        manifest_content = _stable_file_bytes(
            manifest, limit=_MAX_NPM_OWNERSHIP_MANIFEST_BYTES + 1
        )
        if len(manifest_content) > _MAX_NPM_OWNERSHIP_MANIFEST_BYTES:
            raise DependencyObservationError(
                "dependencies.npm-distribution-manifest-invalid",
                "npm distribution manifest exceeds the size limit",
            )
        document = json.loads(manifest_content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyObservationError(
            "dependencies.npm-distribution-manifest-invalid",
            f"npm distribution has an invalid package.json: {manifest}",
        ) from exc
    if not isinstance(document, Mapping) or document.get("name") != "npm":
        raise DependencyObservationError(
            "dependencies.npm-distribution-manifest-invalid",
            "npm distribution manifest must declare the npm package",
        )
    version = document.get("version")
    try:
        parsed_version = semantic_version.Version(version)
    except (TypeError, ValueError) as exc:
        raise DependencyObservationError(
            "dependencies.npm-distribution-manifest-invalid",
            "npm distribution manifest must declare an exact semantic version",
        ) from exc
    if str(parsed_version) != version:
        raise DependencyObservationError(
            "dependencies.npm-distribution-manifest-invalid",
            "npm distribution manifest version must be canonical",
        )
    declared_bin = document.get("bin")
    declared_target = (
        declared_bin.get("npm") if isinstance(declared_bin, Mapping) else None
    )
    if declared_target != cli.relative_to(package_root).as_posix():
        raise DependencyObservationError(
            "dependencies.npm-distribution-bin-invalid",
            "npm distribution manifest does not bind the resolved npm-cli.js",
        )

    directories, paths = _safe_npm_package_tree(package_root, include_node_modules=True)
    files: list[tuple[str, int, str]] = []
    observed_bytes = 0
    for path in paths:
        content = _stable_file_bytes(path, limit=_MAX_NPM_FILE_BYTES + 1)
        if len(content) > _MAX_NPM_FILE_BYTES:
            raise DependencyObservationError(
                "dependencies.npm-graph-budget-exceeded",
                f"npm distribution file exceeds the per-file limit: {path}",
            )
        observed_bytes += len(content)
        if observed_bytes > _MAX_NPM_GRAPH_BYTES:
            raise DependencyObservationError(
                "dependencies.npm-graph-budget-exceeded",
                "npm distribution exceeds the aggregate byte limit",
            )
        if name_or_magic_is_native_or_wasm(path.name, content[:8]):
            raise DependencyObservationError(
                "dependencies.npm-native-payload-unsupported",
                f"npm distribution contains a native or WebAssembly payload: {path}",
            )
        files.append(
            (
                path.relative_to(package_root).as_posix(),
                len(content),
                "sha256:" + hashlib.sha256(content).hexdigest(),
            )
        )
    files.sort(key=lambda item: item[0])
    resolution_root = next(
        (
            ancestor
            for ancestor in package_root.parents
            if ancestor.name.casefold() == "node_modules"
        ),
        package_root,
    )
    dependency_graph, _native_owners, root_package_ref = _npm_package_graph(
        package_root,
        native_predicate=lambda _path: False,
        resolution_root=resolution_root,
        dependency_resolver=dependency_resolver,
        literal_dependency_resolver=literal_dependency_resolver,
        builtin_modules=builtin_modules,
    )
    dependency_graph_package_roots = {
        str(property_["value"])
        for component in dependency_graph.components
        for property_ in component.get("properties", ())
        if isinstance(property_, Mapping)
        and property_.get("name") == "literate-ai:npm-package-root"
        and isinstance(property_.get("value"), str)
    }
    return NpmDistributionAuthority(
        package_root=str(package_root),
        version=version,
        directories=directories,
        files=tuple(files),
        package_roots=tuple(
            sorted(str(path) for path in dependency_graph_package_roots)
        ),
        root_package_ref=root_package_ref,
        dependency_graph=dependency_graph,
    )


def _normalized_observation(
    components: Sequence[Mapping[str, object]], edges: Sequence[tuple[str, str]]
) -> HostDependencyObservation:
    by_ref: dict[str, dict[str, object]] = {}
    for raw in components:
        component = dict(raw)
        ref = component.get("bom-ref")
        if not isinstance(ref, str) or not ref:
            raise DependencyObservationError(
                "dependencies.observation-ref-invalid",
                "observed component lacks a BOM reference",
            )
        existing = by_ref.get(ref)
        if existing is not None and existing != component:
            raise DependencyObservationError(
                "dependencies.observation-ref-conflict",
                "observed BOM reference has conflicting facts",
            )
        by_ref[ref] = component
    return HostDependencyObservation(
        tuple(by_ref[ref] for ref in sorted(by_ref)), tuple(sorted(set(edges)))
    )


def _native_dependency_edges(
    root_ref: str,
    *,
    refs: Mapping[str, str],
    direct_seed_paths: Sequence[str] | set[str],
    image_edges: Sequence[tuple[str, str]],
    native_owners: Mapping[str, str],
    launcher_runtime_edges: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Keep inspection seeds distinct from logical direct-root dependencies."""

    required_paths = {
        *direct_seed_paths,
        *(path for edge in image_edges for path in edge),
        *native_owners,
        *(native for _owner, native in launcher_runtime_edges),
    }
    missing = sorted(required_paths - refs.keys())
    if missing:
        raise DependencyObservationError(
            "dependencies.native-edge-target-missing",
            "native dependency edge references an unobserved image: "
            + ", ".join(missing),
        )
    edges = {(root_ref, refs[path]) for path in direct_seed_paths}
    edges.update(
        (refs[source], refs[target])
        for source, target in image_edges
        if source != target
    )
    edges.update((owner, refs[native]) for native, owner in native_owners.items())
    edges.update((owner, refs[native]) for owner, native in launcher_runtime_edges)
    return tuple(sorted(edges))


def _macho_component(
    image: _MachOImage, ref: str, *, scopes: Sequence[str]
) -> dict[str, object]:
    properties = [
        {"name": "literate-ai:dependency-kind", "value": "system"},
        *(
            {"name": "literate-ai:dependency-scope", "value": scope}
            for scope in sorted(set(scopes) | {"system"})
        ),
        {"name": "literate-ai:macho-path", "value": image.path},
        *(
            {"name": f"literate-ai:macho-uuid:{arch}", "value": uuid}
            for arch, uuid in image.uuids
        ),
    ]
    materialized = image.materialized_file
    if materialized is not None:
        properties.append(
            {
                "name": "literate-ai:macho-resolved-path",
                "value": materialized.resolved_path,
            }
        )
        properties.extend(
            {
                "name": "literate-ai:macho-symlink",
                "value": json.dumps(
                    {"path": path, "target": target},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            for path, target, _identity in materialized.symlink_chain
        )
    result: dict[str, object] = {
        "type": "file",
        "bom-ref": ref,
        "name": Path(image.path).name,
        "version": "macho-" + ",".join(f"{arch}:{uuid}" for arch, uuid in image.uuids),
        "properties": properties,
    }
    if materialized is not None:
        result["hashes"] = [
            {
                "alg": "SHA-256",
                "content": materialized.content_identity.removeprefix("sha256:"),
            }
        ]
    return result


def _macho_ref(image: _MachOImage) -> str:
    identity = canonical_identity(
        {
            "macho_path": image.path,
            "macho_uuids": [list(item) for item in image.uuids],
            "materialized_file": (
                None
                if image.materialized_file is None
                else {
                    "resolved_path": image.materialized_file.resolved_path,
                    "content_identity": image.materialized_file.content_identity,
                    "symlink_chain": [
                        {"path": path, "target": target}
                        for path, target, _identity in (
                            image.materialized_file.symlink_chain
                        )
                    ],
                }
            ),
        }
    )
    return f"urn:literate-ai:macho:{identity.digest}"


def _file_component(
    path: Path, ref: str, *, name: str, scopes: Sequence[str]
) -> dict[str, object]:
    digest = _file_digest(path).removeprefix("sha256:")
    return {
        "type": "file",
        "bom-ref": ref,
        "name": name,
        "version": f"sha256-{digest}",
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "toolchain"},
            *(
                {"name": "literate-ai:dependency-scope", "value": scope}
                for scope in sorted(set(scopes))
            ),
            {"name": "literate-ai:launcher-path", "value": str(path)},
        ],
    }


def _file_component_ref(path: Path, *, prefix: str) -> str:
    digest = _file_digest(path).removeprefix("sha256:")
    return f"urn:literate-ai:{prefix}:{digest}"


def _artifact_root(build: Mapping[str, object]) -> Path:
    value = build.get("artifact_path")
    if not isinstance(value, str):
        raise DependencyObservationError(
            "dependencies.artifact-path-missing", "build result omits its artifact path"
        )
    path = Path(value)
    if path.is_symlink() or not path.is_dir():
        raise DependencyObservationError(
            "dependencies.artifact-path-invalid",
            "build artifact path is unsafe or missing",
        )
    return path.resolve()


def _explicit_artifact_files(files):
    if files is None:
        return None
    if (
        not isinstance(files, Sequence)
        or not 1 <= len(files) <= 4096
        or any(
            not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
            for path in files
        )
        or len(set(files)) != len(files)
    ):
        raise ValueError("native artifact selection must contain unique absolute paths")
    return tuple(files)


def _native_artifact_files(root, selected, predicate):
    if selected is None:
        return tuple(path for path in _regular_tree_files(root) if predicate(path))
    for path in selected:
        try:
            if not path.is_relative_to(root):
                raise ValueError("selected native artifact escapes its artifact root")
            require_safe_directory(path.parent)
            node = path.lstat()
            if (
                stat_is_link_or_reparse(node)
                or not stat.S_ISREG(node.st_mode)
                or not predicate(path)
            ):
                raise ValueError(
                    "selected native artifact is unsafe or has the wrong format"
                )
        except (OSError, ValueError, UnsafeFilesystemPathError) as exc:
            raise DependencyObservationError(
                "dependencies.artifact-selection-invalid",
                "selected native artifact is missing, unsafe or has the wrong format",
            ) from exc
    return selected


def _regular_tree_files(root: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in root.rglob("*"):
        if len(result) >= _MAX_OBSERVED_FILES:
            raise DependencyObservationError(
                "dependencies.file-limit", "dependency observation file limit exceeded"
            )
        if path.is_symlink():
            raise DependencyObservationError(
                "dependencies.symlink-unsupported",
                "dependency observation rejects symlinks",
            )
        if path.is_file():
            result.append(path)
    return tuple(sorted(result))


def _is_macho(path: Path) -> bool:
    return _stable_file_bytes(path, limit=4) in _MACHO_MAGICS


def is_host_native_executable(path: Path) -> bool:
    """Return whether exact file bytes use this host's executable format."""

    if sys.platform == "darwin":
        return _is_macho(path)
    if sys.platform.startswith("linux"):
        return _is_elf(path)
    if sys.platform in {"win32", "cygwin"}:
        return _is_pe(path)
    return False


def _resolve_pe_toolchain_input(command: Sequence[str]) -> Path:
    """Observe an explicitly selected PE DLL without treating it as a command.

    Windows BEAM ships its native runtime as a DLL. PATH/PATHEXT command
    discovery cannot resolve that payload; it must be an exact absolute file
    selection, with no arguments, links or reparse points.
    """
    if command and isinstance(command[0], str):
        path = Path(command[0])
        if path.suffix.casefold() == ".dll":
            try:
                if len(command) != 1 or not path.is_absolute() or ".." in path.parts:
                    raise ValueError("PE DLL selection must be one absolute path")
                require_safe_directory(path.parent)
                node = path.lstat()
                if (
                    stat_is_link_or_reparse(node)
                    or not stat.S_ISREG(node.st_mode)
                    or not _is_pe(path)
                ):
                    raise ValueError("PE DLL selection must be a regular PE file")
            except (OSError, ValueError, UnsafeFilesystemPathError) as exc:
                raise DependencyObservationError(
                    "dependencies.windows-toolchain-invalid",
                    "selected PE toolchain DLL is missing, unsafe or invalid",
                ) from exc
            return path.resolve()
    return _resolve_command(command)


def _resolve_command(command: Sequence[str]) -> Path:
    if not command or not isinstance(command[0], str) or not command[0]:
        raise DependencyObservationError(
            "dependencies.command-invalid", "dependency observation command is invalid"
        )
    found = shutil.which(command[0])
    if found is None:
        raise DependencyObservationError(
            "dependencies.command-missing",
            f"required command {command[0]!r} is unavailable",
        )
    path = Path(found).resolve()
    if not path.is_file():
        raise DependencyObservationError(
            "dependencies.command-invalid",
            f"required command {command[0]!r} is not a file",
        )
    return path


def _resolve_non_symlink_tool(name: str) -> Path:
    found = shutil.which(name)
    if found is None:
        raise DependencyObservationError(
            "dependencies.inspector-missing",
            f"required inspector {name!r} is unavailable",
        )
    configured = Path(found)
    if (
        not configured.is_absolute()
        or configured.is_symlink()
        or not configured.is_file()
    ):
        raise DependencyObservationError(
            "dependencies.inspector-unsafe",
            f"required inspector {name!r} must be an absolute non-symlink file",
        )
    return configured


def _macho_materialized_file(path: Path) -> _MachOMaterializedFile:
    """Bind a loader-visible Mach-O path to one stable regular file and link chain."""

    if not path.is_absolute():
        raise DependencyObservationError(
            "dependencies.macos-image-unsafe",
            f"materialized Mach-O image path is not absolute: {path}",
        )
    try:
        resolved_before = path.resolve(strict=True)
        chain_before = _symlink_chain_snapshot(path)
    except (OSError, RuntimeError) as exc:
        raise DependencyObservationError(
            "dependencies.macos-image-unsafe",
            f"cannot resolve materialized Mach-O image path: {path}",
        ) from exc
    if resolved_before.is_symlink() or not resolved_before.is_file():
        raise DependencyObservationError(
            "dependencies.macos-image-unsafe",
            f"materialized Mach-O image does not resolve to a regular file: {path}",
        )
    content_identity = _file_digest(resolved_before)
    try:
        resolved_after = path.resolve(strict=True)
        chain_after = _symlink_chain_snapshot(path)
    except (OSError, RuntimeError) as exc:
        raise DependencyObservationError(
            "dependencies.macos-image-changed",
            f"Mach-O symlink chain changed while resolving: {path}",
        ) from exc
    if resolved_after != resolved_before or chain_after != chain_before:
        raise DependencyObservationError(
            "dependencies.macos-image-changed",
            f"Mach-O symlink chain changed while resolving: {path}",
        )
    return _MachOMaterializedFile(str(resolved_before), content_identity, chain_before)


def _symlink_chain_snapshot(
    path: Path,
) -> tuple[tuple[str, str, tuple[int, int, int, int, int]], ...]:
    """Capture every stable symlink encountered by an absolute loader path."""

    pending = [path]
    inspected_paths: set[str] = set()
    symlinks: dict[str, tuple[str, tuple[int, int, int, int, int]]] = {}
    while pending:
        candidate = pending.pop()
        candidate_key = os.path.normpath(str(candidate))
        if candidate_key in inspected_paths:
            continue
        inspected_paths.add(candidate_key)
        prefix = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            prefix /= part
            observed = os.lstat(prefix)
            if not stat.S_ISLNK(observed.st_mode):
                continue
            target = os.readlink(prefix)
            symlinks[str(prefix)] = (target, _stat_identity(observed))
            target_path = Path(target)
            if not target_path.is_absolute():
                target_path = prefix.parent / target_path
            pending.append(Path(os.path.abspath(target_path)))
    return tuple(
        (link_path, target, identity)
        for link_path, (target, identity) in sorted(symlinks.items())
    )


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(_stable_file_bytes(path)).hexdigest()


def _stable_file_bytes(
    path: Path, *, offset: int = 0, limit: int | None = None
) -> bytes:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
    ):
        raise ValueError("limit must be a non-negative integer or None")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        path_before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(path_before.st_mode):
            raise DependencyObservationError(
                "dependencies.file-unsafe",
                f"dependency evidence path is not a regular file: {path}",
            )
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.file-unreadable",
            f"cannot securely open dependency evidence path {path}",
        ) from exc
    try:
        descriptor_before = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_before.st_mode) or (
            _stat_binding_identity(path_before)
            != _stat_binding_identity(descriptor_before)
        ):
            raise DependencyObservationError(
                "dependencies.file-changed",
                f"dependency evidence path changed while opening: {path}",
            )
        if offset:
            os.lseek(descriptor, offset, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = limit
        while True:
            if remaining == 0:
                break
            chunk = os.read(
                descriptor,
                min(1024 * 1024, remaining) if remaining is not None else 1024 * 1024,
            )
            if not chunk:
                break
            chunks.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        descriptor_after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        if (
            _stat_identity(descriptor_after) != _stat_identity(descriptor_before)
            or _stat_identity(path_after) != _stat_identity(path_before)
            or _stat_binding_identity(path_after)
            != _stat_binding_identity(descriptor_after)
        ):
            raise DependencyObservationError(
                "dependencies.file-changed",
                f"dependency evidence path changed while reading: {path}",
            )
        return b"".join(chunks)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.file-unreadable",
            f"cannot securely read dependency evidence path {path}",
        ) from exc
    finally:
        os.close(descriptor)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _stat_binding_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    """Return fields whose path-stat and fd-stat meanings are interoperable.

    Windows may report slightly different ``st_ctime_ns`` values for the same file
    through ``stat`` and ``fstat``.  The full identity remains valuable when two
    observations use the same API, while device, inode, size, and modification time
    provide the cross-handle binding.
    """

    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _nearest_package_root(path: Path) -> Path | None:
    for parent in (path.parent, *path.parents):
        if parent.joinpath("package.json").is_file():
            return parent.resolve()
    return None


def _npm_launcher_package_root(launcher: Path, command: str) -> Path | None:
    """Bind an exact launcher file to the package.json bin entry that owns it."""

    command_name = _npm_command_name(command)
    target = launcher.resolve(strict=True)
    if launcher.suffix.casefold() == ".cmd":
        target = resolve_windows_npm_cmd_target(launcher)
    package_root = _nearest_package_root(target)
    if package_root is None:
        return None
    declared_target = _npm_declared_bin_target(package_root, command_name)
    if declared_target != target:
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            f"lifecycle launcher {launcher} does not equal the declared npm bin "
            f"target for {command_name!r}",
        )
    return package_root


def _npm_toolchain_launcher_dependencies(
    launcher: Path,
    command: str,
    *,
    root_ref: str,
    native_predicate: Callable[[Path], bool],
) -> (
    tuple[
        HostDependencyObservation,
        dict[str, str],
        tuple[tuple[str, str], ...],
    ]
    | None
):
    """Resolve the reviewed Bazelisk npm launcher and its native Node runtime."""

    logical_command = command
    if (
        launcher.name.casefold() == "bazelisk.js"
        and _npm_command_name(command) == "bazelisk.js"
    ):
        # Toolchain discovery may retain the resolved npm bin target rather than
        # the user-facing shim. Bind that exact target back through the package's
        # canonical declared ``bazel`` bin entry.
        logical_command = "bazel"
    package_root = _npm_launcher_package_root(launcher, logical_command)
    if package_root is None:
        return None
    name, _version, manifest = _npm_manifest_document(package_root)
    if name != "@bazel/bazelisk":
        return None
    command_name = _npm_command_name(logical_command)
    if command_name not in {"bazel", "bazelisk"}:
        raise DependencyObservationError(
            "dependencies.toolchain-launcher-unsupported",
            "the Bazelisk npm package may bind only bazel or bazelisk",
        )
    del manifest  # exact package/bin ownership was validated above
    resolution_root = next(
        (
            ancestor
            for ancestor in package_root.parents
            if ancestor.name == "node_modules"
        ),
        package_root,
    )
    packages, native_owners, package_ref = _npm_package_graph(
        package_root,
        native_predicate=native_predicate,
        resolution_root=resolution_root,
    )
    node = _resolve_command(("node",))
    if not native_predicate(node):
        raise DependencyObservationError(
            "dependencies.toolchain-runtime-unbound",
            "the Bazelisk Node runtime is not an inspectable native binary",
        )
    observation = _normalized_observation(
        packages.components,
        ((root_ref, package_ref), *packages.edges),
    )
    return observation, native_owners, ((package_ref, str(node)),)


def _npm_launcher_dependencies(
    launcher: Path,
    command: str,
    *,
    root_ref: str,
    prefix: str,
    native_predicate: Callable[[Path], bool],
) -> tuple[HostDependencyObservation, dict[str, str], tuple[tuple[str, str], ...]]:
    """Reject npm lifecycle launchers; no reviewed execution-closure resolver
    remains."""

    del launcher, root_ref, prefix, native_predicate
    command_name = _npm_command_name(command)
    raise DependencyObservationError(
        "dependencies.lifecycle-launcher-unsupported",
        f"npm lifecycle launcher command {command_name!r} has no "
        "reviewed execution-closure resolver",
    )


def _npm_manifest_document(
    package_root: Path,
) -> tuple[str, str, Mapping[str, object]]:
    manifest = package_root / "package.json"
    content = _stable_file_bytes(manifest, limit=_MAX_NPM_OWNERSHIP_MANIFEST_BYTES + 1)
    if len(content) > _MAX_NPM_OWNERSHIP_MANIFEST_BYTES:
        raise DependencyObservationError(
            "dependencies.npm-manifest-size-invalid",
            f"npm manifest exceeds the byte limit: {manifest}",
        )
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyObservationError(
            "dependencies.npm-manifest-invalid", f"invalid npm manifest {manifest}"
        ) from exc
    if not isinstance(value, Mapping):
        raise DependencyObservationError(
            "dependencies.npm-manifest-invalid", f"invalid npm manifest {manifest}"
        )
    name, version = value.get("name"), value.get("version")
    if (
        not isinstance(name, str)
        or _NPM_PACKAGE_NAME.fullmatch(name) is None
        or not isinstance(version, str)
        or not version
    ):
        raise DependencyObservationError(
            "dependencies.npm-manifest-invalid",
            f"npm manifest lacks a valid exact identity: {manifest}",
        )
    return name, version, value


def _npm_component_ref(root: Path, name: str, version: str) -> str:
    identity = canonical_identity(
        {"npm_package": name, "version": version, "path": str(root.resolve())}
    )
    return f"urn:literate-ai:npm:{identity.digest}"


def resolve_windows_npm_cmd_target(launcher: Path) -> Path:
    """Parse only the bounded modern npm cmd-shim template and return its target."""

    content = _stable_file_bytes(launcher, limit=_MAX_NPM_CMD_SHIM_BYTES + 1)
    if len(content) > _MAX_NPM_CMD_SHIM_BYTES:
        raise DependencyObservationError(
            "dependencies.npm-launcher-oversized",
            f"npm command shim exceeds {_MAX_NPM_CMD_SHIM_BYTES} bytes: {launcher}",
        )
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise DependencyObservationError(
            "dependencies.npm-launcher-invalid",
            f"npm command shim is not UTF-8: {launcher}",
        ) from exc
    if "\x00" in text or "\r" in text.replace("\r\n", ""):
        raise DependencyObservationError(
            "dependencies.npm-launcher-invalid",
            f"npm command shim has invalid control bytes: {launcher}",
        )
    lines = text.replace("\r\n", "\n").splitlines()
    if (
        len(lines) > _MAX_NPM_CMD_SHIM_LINES
        or any(
            len(line.encode("utf-8")) > _MAX_NPM_CMD_SHIM_LINE_BYTES for line in lines
        )
        or tuple(lines[:-1]) != _WINDOWS_NPM_CMD_SHIM_PREFIX
    ):
        raise DependencyObservationError(
            "dependencies.npm-launcher-invalid",
            f"npm command shim does not match the required control flow: {launcher}",
        )
    dispatch = _WINDOWS_NPM_CMD_SHIM_DISPATCH.fullmatch(lines[-1])
    if dispatch is None:
        raise DependencyObservationError(
            "dependencies.npm-launcher-invalid",
            f"npm command shim does not have one exact dispatch: {launcher}",
        )
    raw_target = dispatch.group("target")
    prefix = "%dp0%\\"
    if not raw_target.startswith(prefix):
        raise DependencyObservationError(
            "dependencies.npm-launcher-invalid",
            f"npm command shim target is not relative to its exact prefix: {launcher}",
        )
    relative_text = raw_target.removeprefix(prefix)
    raw_parts = relative_text.split("\\")
    local_bin_shim = (
        len(raw_parts) > 1
        and raw_parts[0] == ".."
        and launcher.parent.name.casefold() == ".bin"
    )
    target_parts = raw_parts[1:] if local_bin_shim else raw_parts
    relative = PureWindowsPath(*target_parts)
    if (
        not target_parts
        or any(part in {"", ".", ".."} for part in target_parts)
        or (not local_bin_shim and relative.parts[0].casefold() != "node_modules")
        or relative.is_absolute()
        or relative.drive
        or relative.root
        or "/" in relative_text
        or "%" in relative_text
        or ":" in relative_text
        or any(
            _WINDOWS_NPM_CMD_SHIM_SEGMENT.fullmatch(part) is None
            for part in target_parts
        )
    ):
        raise DependencyObservationError(
            "dependencies.npm-launcher-invalid",
            f"npm command shim target is not a safe node_modules path: {launcher}",
        )
    prefix_root = launcher.parent.resolve(strict=True)
    modules = prefix_root.parent if local_bin_shim else prefix_root / "node_modules"
    target = (
        modules.joinpath(*relative.parts)
        if local_bin_shim
        else prefix_root.joinpath(*relative.parts)
    )
    try:
        resolved_modules = modules.resolve(strict=True)
        _require_no_link_components(target, modules if local_bin_shim else prefix_root)
        resolved_target = target.resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.npm-launcher-invalid",
            f"npm command shim target is unavailable: {launcher}",
        ) from exc
    if (
        not resolved_modules.is_dir()
        or not resolved_target.is_file()
        or not resolved_target.is_relative_to(resolved_modules)
    ):
        raise DependencyObservationError(
            "dependencies.npm-launcher-invalid",
            f"npm command shim target escapes its exact package prefix: {launcher}",
        )
    return resolved_target


_windows_npm_cmd_target = resolve_windows_npm_cmd_target


def _npm_declared_bin_target(package_root: Path, command: str) -> Path:
    resolved_root = package_root.resolve(strict=True)
    manifest = resolved_root / "package.json"
    content = _stable_file_bytes(manifest, limit=_MAX_NPM_OWNERSHIP_MANIFEST_BYTES + 1)
    if len(content) > _MAX_NPM_OWNERSHIP_MANIFEST_BYTES:
        raise DependencyObservationError(
            "dependencies.npm-launcher-manifest-oversized",
            f"npm ownership manifest exceeds the limit: {manifest}",
        )
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyObservationError(
            "dependencies.npm-launcher-manifest-invalid",
            f"npm ownership manifest is invalid: {manifest}",
        ) from exc
    if not isinstance(value, Mapping):
        raise DependencyObservationError(
            "dependencies.npm-launcher-manifest-invalid",
            f"npm ownership manifest is not an object: {manifest}",
        )
    package_name = value.get("name")
    if (
        not isinstance(package_name, str)
        or _NPM_PACKAGE_NAME.fullmatch(package_name) is None
    ):
        raise DependencyObservationError(
            "dependencies.npm-launcher-manifest-invalid",
            f"npm ownership manifest has an invalid package name: {manifest}",
        )
    modules_root, expected_root = _npm_expected_package_root(
        resolved_root, package_name
    )
    if expected_root != resolved_root:
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            f"npm package name {package_name!r} does not match {resolved_root}",
        )
    declared = value.get("bin")
    target_value: object | None = None
    if isinstance(declared, str):
        if _npm_command_matches(package_name.rsplit("/", 1)[-1], command):
            target_value = declared
    elif isinstance(declared, Mapping):
        matching = [
            target
            for name, target in declared.items()
            if isinstance(name, str) and _npm_command_matches(name, command)
        ]
        if len(matching) == 1:
            target_value = matching[0]
        elif len(matching) > 1:
            raise DependencyObservationError(
                "dependencies.npm-launcher-binding-invalid",
                f"npm package {package_name!r} declares command {command!r} twice",
            )
    if not isinstance(target_value, str) or not target_value:
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            f"npm package {package_name!r} does not declare command {command!r}",
        )
    raw_parts = target_value.split("/")
    relative = PurePosixPath(*raw_parts)
    if (
        any(part in {"", ".", ".."} for part in raw_parts)
        or relative.is_absolute()
        or "\\" in target_value
        or "%" in target_value
        or ":" in target_value
    ):
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            f"npm package {package_name!r} declares an unsafe bin target",
        )
    target = resolved_root.joinpath(*relative.parts)
    try:
        _require_no_link_components(target, resolved_root)
        resolved_target = target.resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            f"npm package {package_name!r} bin target is unavailable",
        ) from exc
    if (
        not resolved_target.is_file()
        or not resolved_target.is_relative_to(resolved_root)
        or not resolved_root.is_relative_to(modules_root)
    ):
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            f"npm package {package_name!r} bin target is not a safe package file",
        )
    return resolved_target


def _npm_expected_package_root(
    package_root: Path, package_name: str
) -> tuple[Path, Path]:
    if package_name.startswith("@"):
        scope, name = package_name.split("/", 1)
        modules_root = package_root.parent.parent
        expected = modules_root / scope / name
    else:
        modules_root = package_root.parent
        expected = modules_root / package_name
    if modules_root.name.casefold() != "node_modules":
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            f"npm package root is not installed below node_modules: {package_root}",
        )
    try:
        return modules_root.resolve(strict=True), expected.resolve(strict=True)
    except OSError as exc:
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            f"npm package name {package_name!r} does not identify {package_root}",
        ) from exc


def _require_no_link_components(path: Path, boundary: Path) -> None:
    current = boundary
    try:
        relative = path.relative_to(boundary)
    except ValueError as exc:
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            f"npm launcher path escaped its boundary: {path}",
        ) from exc
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise DependencyObservationError(
                "dependencies.npm-launcher-binding-invalid",
                f"npm launcher path component is unavailable: {current}",
            ) from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x0400:
            raise DependencyObservationError(
                "dependencies.npm-launcher-binding-invalid",
                f"npm launcher path contains a link or junction: {current}",
            )


def _npm_command_name(command: str) -> str:
    name = Path(command).name
    for suffix in (".cmd", ".bat", ".ps1", ".exe"):
        if name.casefold().endswith(suffix):
            name = name[: -len(suffix)]
            break
    if not name or "/" in name or "\\" in name or "%" in name or ":" in name:
        raise DependencyObservationError(
            "dependencies.npm-launcher-binding-invalid",
            "npm lifecycle command name is invalid",
        )
    return name


def _npm_command_matches(declared: str, requested: str) -> bool:
    return (
        declared.casefold() == requested.casefold()
        if os.name == "nt"
        else declared == requested
    )


def _require_tool(name: str, *, code: str) -> None:
    if shutil.which(name) is None:
        raise DependencyObservationError(
            code, f"required dependency inspector {name!r} is unavailable"
        )
