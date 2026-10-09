"""Admit generated manifests and imports against the source BOM."""

from __future__ import annotations

import ast
import json
import posixpath
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import PurePosixPath

from literate_ai.contracts.mix_projects import MixProjectIntent

from .javascript_imports import javascript_package_imports
from .mix_lock import MixLock
from .types import DependencyObservationError, _normalized_package_name

_PYTHON_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

_RUST_IMPORT = re.compile(
    r"(?:^|\n)\s*(?:use|extern\s+crate)\s+([A-Za-z_][A-Za-z0-9_]*)"
)

# `mod name;` declares a crate-local module, so a later `use name::...` resolves
# locally and is never an external crate dependency.
_RUST_MODULE_DECLARATION = re.compile(
    r"(?:^|\n)\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*[;{]"
)

_CPP_QUOTED_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)

_RUST_BUILTINS = frozenset({"alloc", "core", "crate", "self", "std", "super"})


def reconcile_generated_dependencies(
    files: Mapping[str, object],
    source_bom_content: bytes,
    *,
    allow_missing_cargo_lock: bool = False,
    runtime_python_imports: frozenset[str] = frozenset(),
    observed_python_imports: frozenset[str] | None = None,
) -> None:
    """Fail when generated dependency declarations/imports vanish from the BOM."""

    runtime_modules = {
        _normalized_package_name(name) for name in runtime_python_imports
    }
    document = json.loads(source_bom_content)
    available = _bom_package_names(document)
    available_python_imports = available | _bom_python_import_names(document)
    declared: set[str] = set()
    locked: set[str] = set()
    imported: set[str] = set()
    text_files = {
        path: content
        for path, content in files.items()
        if isinstance(path, str) and isinstance(content, str)
    }
    local_python_modules = {
        _normalized_package_name(PurePosixPath(path).stem)
        for path in text_files
        if PurePosixPath(path).suffix.casefold() == ".py"
        and PurePosixPath(path).stem != "__init__"
    }
    local_python_modules.update(
        _normalized_package_name(PurePosixPath(path).parent.name)
        for path in text_files
        if PurePosixPath(path).name == "__init__.py" and PurePosixPath(path).parent.name
    )
    # Python 3 namespace packages do not require ``__init__.py``. Generated
    # applications intentionally keep native test hooks under ``source/tests``;
    # when ``source/main.py`` runs, that first directory below ``source`` is a
    # repository-local top-level import just as surely as ``source/helper.py``.
    local_python_modules.update(
        _normalized_package_name(parts[1])
        for path in text_files
        if PurePosixPath(path).suffix.casefold() == ".py"
        and (parts := PurePosixPath(path).parts)
        and len(parts) >= 3
        and parts[0] == "source"
    )
    # Rust has the same repository-local import shape as Python: a `.rs` file or a
    # `mod name;` declaration makes `use name::...` a crate-local path, not an
    # external crate.
    local_rust_modules = {
        _normalized_package_name(PurePosixPath(path).stem)
        for path in text_files
        if PurePosixPath(path).suffix.casefold() == ".rs"
        and PurePosixPath(path).stem not in {"main", "lib", "mod"}
    }
    local_rust_modules.update(
        _normalized_package_name(name)
        for path, content in text_files.items()
        if PurePosixPath(path).suffix.casefold() == ".rs"
        for name in _RUST_MODULE_DECLARATION.findall(content)
    )
    local_rust_modules.update(
        _cargo_root_name(content)
        for path, content in text_files.items()
        if PurePosixPath(path).name == "Cargo.toml"
        and _cargo_root_name(content) is not None
    )
    # JavaScript has the same repository-local shape. A generated entrypoint that
    # imports a sibling by bare specifier (``main``, ``litai_test``) resolves to a
    # generated file, not to an npm distribution.
    local_javascript_modules = {
        _normalized_package_name(PurePosixPath(path).stem)
        for path in text_files
        if PurePosixPath(path).suffix.casefold() in {".js", ".mjs", ".cjs", ".jsx"}
    }
    project_package_names: set[str] = set()
    for path, content in text_files.items():
        name = PurePosixPath(path).name
        if name in {"requirements.txt", "requirements.in"}:
            declared.update(_python_requirements(content))
        elif name == "pyproject.toml":
            declared.update(_pyproject_dependencies(content))
        elif name == "package.json":
            declared.update(_package_json_dependencies(content))
            root_name = _package_json_root_name(content)
            if root_name is not None:
                project_package_names.add(root_name)
        elif name == "package-lock.json":
            locked.update(_package_lock_dependencies(content))
        elif name == "Cargo.toml":
            declared.update(_cargo_dependencies(content))
            root_name = _cargo_root_name(content)
            if root_name is not None:
                project_package_names.add(root_name)
        elif name == "mix-project.json":
            project = _mix_project_intent(content)
            declared.update(
                _normalized_package_name(item.name) for item in project.dependencies
            )
            project_package_names.add(_normalized_package_name(project.app))
        elif name == "mix.exs":
            raise DependencyObservationError(
                "dependencies.mix-authority-unsupported",
                "generated Mix projects must use declarative mix-project.json; "
                "native mix.exs belongs only in the authorized build projection",
            )
        elif name == "mix.lock":
            intent_path = str(PurePosixPath(path).parent / "mix-project.json")
            intent = text_files.get(intent_path)
            if intent is None:
                raise DependencyObservationError(
                    "dependencies.mix-manifest-missing",
                    "Mix lock requires corresponding declarative project intent",
                )
            try:
                lock = MixLock.from_bytes(
                    content.encode("utf-8"), project=_mix_project_intent(intent)
                )
            except UnicodeError as exc:
                raise DependencyObservationError(
                    "dependencies.mix-lock-invalid", "Mix lock must be UTF-8"
                ) from exc
            locked.update(_normalized_package_name(item.name) for item in lock.packages)
        elif name in {"Cargo.lock", "poetry.lock", "uv.lock"}:
            locked.update(_toml_lock_dependencies(content, name=name))
        elif name == "vcpkg.json":
            declared.update(_vcpkg_dependencies(content))
        suffix = PurePosixPath(path).suffix.casefold()
        if suffix == ".py":
            python_imports = (
                python_import_names(content) - local_python_modules - runtime_modules
            )
            if observed_python_imports is not None:
                missing = python_imports - {
                    _normalized_package_name(alias) for alias in observed_python_imports
                }
                if missing:
                    raise DependencyObservationError(
                        "dependencies.python-import-unobserved",
                        "Python imports lack installed payload evidence: "
                        + ", ".join(sorted(missing)),
                    )
            imported.update(python_imports)
        elif suffix in {".js", ".mjs", ".cjs", ".jsx"}:
            imported.update(_javascript_imports(content) - local_javascript_modules)
        elif suffix == ".rs":
            imported.update(_rust_imports(content) - local_rust_modules)
        elif suffix in {".c", ".cc", ".cpp", ".cu", ".cxx", ".h", ".hpp"}:
            _require_local_cpp_includes(path, content, text_files)
    missing_declared = sorted(name for name in declared if name not in available)
    if missing_declared:
        raise DependencyObservationError(
            "dependencies.manifest-bom-mismatch",
            "generated dependency declarations are absent from the source BOM: "
            + ", ".join(missing_declared),
        )
    if allow_missing_cargo_lock and not locked:
        locked.update(
            name
            for path, content in text_files.items()
            if PurePosixPath(path).name == "Cargo.toml"
            for name in _cargo_dependencies(content)
        )
    missing_locked = sorted(
        name for name in locked - project_package_names if name not in available
    )
    if missing_locked:
        raise DependencyObservationError(
            "dependencies.lock-bom-mismatch",
            "generated lockfile dependencies are absent from the source BOM: "
            + ", ".join(missing_locked),
        )
    missing_imported = sorted(
        name
        for name in imported
        if name not in declared and name not in available_python_imports
    )
    if missing_imported:
        raise DependencyObservationError(
            "dependencies.import-bom-mismatch",
            "generated external imports are absent from manifests and the source BOM: "
            + ", ".join(missing_imported),
        )


def _mix_project_intent(content: str) -> MixProjectIntent:
    try:
        return MixProjectIntent.from_bytes(content.encode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise DependencyObservationError(
            "dependencies.mix-manifest-invalid",
            "Mix project requires bounded declarative dependency intent",
        ) from exc


def _bom_package_names(document: object) -> set[str]:
    if not isinstance(document, Mapping):
        raise DependencyObservationError(
            "dependencies.source-bom-invalid", "source BOM root is not an object"
        )
    metadata = document.get("metadata")
    values: list[object] = []
    if isinstance(metadata, Mapping):
        values.append(metadata.get("component"))
    components = document.get("components")
    if isinstance(components, list):
        values.extend(components)
    result: set[str] = set()
    pending = list(values)
    while pending:
        value = pending.pop()
        if not isinstance(value, Mapping):
            continue
        name = value.get("name")
        if isinstance(name, str):
            result.add(_normalized_package_name(name))
        purl = value.get("purl")
        if isinstance(purl, str):
            package = purl.split("?", 1)[0].rsplit("/", 1)[-1].split("@", 1)[0]
            result.add(_normalized_package_name(package))
        nested = value.get("components")
        if isinstance(nested, list):
            pending.extend(nested)
    return result


def _bom_python_import_names(document: object) -> set[str]:
    """Return explicit distribution-to-import aliases from CycloneDX properties."""

    if not isinstance(document, Mapping):
        return set()
    pending = list(document.get("components", []))
    result: set[str] = set()
    while pending:
        value = pending.pop()
        if not isinstance(value, Mapping):
            continue
        properties = value.get("properties", [])
        if isinstance(properties, list):
            for property_value in properties:
                if (
                    isinstance(property_value, Mapping)
                    and property_value.get("name")
                    == "literate-ai:python-top-level-import"
                    and isinstance(property_value.get("value"), str)
                ):
                    result.add(_normalized_package_name(str(property_value["value"])))
        nested = value.get("components")
        if isinstance(nested, list):
            pending.extend(nested)
    return result


def _python_requirements(content: str) -> set[str]:
    result: set[str] = set()
    for line in content.splitlines():
        stripped = line.partition("#")[0].strip()
        if not stripped or stripped.startswith(("-", "http:", "https:", "git+")):
            continue
        match = _PYTHON_REQUIREMENT.match(stripped)
        if match:
            result.add(_normalized_package_name(match.group(1)))
    return result


def _pyproject_dependencies(content: str) -> set[str]:
    try:
        value = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise DependencyObservationError(
            "dependencies.python-manifest-invalid",
            "generated pyproject.toml is invalid",
        ) from exc
    project = value.get("project", {})
    dependencies = (
        project.get("dependencies", []) if isinstance(project, Mapping) else []
    )
    result = {
        match.group(1)
        for item in dependencies
        if isinstance(item, str) and (match := _PYTHON_REQUIREMENT.match(item))
    }
    return {_normalized_package_name(item) for item in result}


def _package_json_dependencies(content: str) -> set[str]:
    value = _package_json(content)
    result: set[str] = set()
    for field in (
        "dependencies",
        "optionalDependencies",
        "peerDependencies",
        "devDependencies",
    ):
        entries = value.get(field, {})
        if not isinstance(entries, Mapping):
            raise DependencyObservationError(
                "dependencies.javascript-manifest-invalid",
                f"generated package.json {field} must be an object",
            )
        result.update(_normalized_package_name(str(item)) for item in entries)
    return result


def _package_json(content: str) -> Mapping[str, object]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DependencyObservationError(
            "dependencies.javascript-manifest-invalid",
            "generated package.json is invalid",
        ) from exc
    if not isinstance(value, Mapping):
        raise DependencyObservationError(
            "dependencies.javascript-manifest-invalid",
            "generated package.json is invalid",
        )
    return value


def _package_json_root_name(content: str) -> str | None:
    name = _package_json(content).get("name")
    return _normalized_package_name(name) if isinstance(name, str) and name else None


def _package_lock_dependencies(content: str) -> set[str]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DependencyObservationError(
            "dependencies.javascript-lock-invalid",
            "generated package-lock.json is invalid",
        ) from exc
    if not isinstance(value, Mapping):
        raise DependencyObservationError(
            "dependencies.javascript-lock-invalid",
            "generated package-lock.json is invalid",
        )
    result: set[str] = set()
    packages = value.get("packages", {})
    if not isinstance(packages, Mapping):
        raise DependencyObservationError(
            "dependencies.javascript-lock-invalid",
            "generated package-lock.json packages must be an object",
        )
    for package in packages.values():
        if not isinstance(package, Mapping):
            raise DependencyObservationError(
                "dependencies.javascript-lock-invalid",
                "generated package-lock.json contains an invalid package",
            )
        name = package.get("name")
        if isinstance(name, str) and name:
            result.add(_normalized_package_name(name))
    dependencies = value.get("dependencies", {})
    if isinstance(dependencies, Mapping):
        result.update(_normalized_package_name(str(name)) for name in dependencies)
    return result


def _cargo_dependencies(content: str) -> set[str]:
    value = _cargo_manifest(content)
    result: set[str] = set()
    for field, entries in value.items():
        if field == "dependencies" and isinstance(entries, Mapping):
            result.update(_normalized_package_name(str(item)) for item in entries)
        if isinstance(entries, Mapping):
            nested = entries.get("dependencies")
            if isinstance(nested, Mapping):
                result.update(_normalized_package_name(str(item)) for item in nested)
    return result


def _cargo_manifest(content: str) -> Mapping[str, object]:
    try:
        value = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise DependencyObservationError(
            "dependencies.rust-manifest-invalid", "generated Cargo.toml is invalid"
        ) from exc
    return value


def _cargo_root_name(content: str) -> str | None:
    package = _cargo_manifest(content).get("package")
    name = package.get("name") if isinstance(package, Mapping) else None
    return _normalized_package_name(name) if isinstance(name, str) and name else None


def _toml_lock_dependencies(content: str, *, name: str) -> set[str]:
    try:
        value = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise DependencyObservationError(
            "dependencies.lock-invalid", f"generated {name} is invalid"
        ) from exc
    packages = value.get("package", [])
    if not isinstance(packages, list):
        raise DependencyObservationError(
            "dependencies.lock-invalid", f"generated {name} package list is invalid"
        )
    result: set[str] = set()
    for package in packages:
        package_name = package.get("name") if isinstance(package, Mapping) else None
        if not isinstance(package_name, str) or not package_name:
            raise DependencyObservationError(
                "dependencies.lock-invalid", f"generated {name} package is invalid"
            )
        result.add(_normalized_package_name(package_name))
    return result


def _vcpkg_dependencies(content: str) -> set[str]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DependencyObservationError(
            "dependencies.cpp-manifest-invalid", "generated vcpkg.json is invalid"
        ) from exc
    dependencies = value.get("dependencies", []) if isinstance(value, Mapping) else []
    result: set[str] = set()
    if not isinstance(dependencies, list):
        raise DependencyObservationError(
            "dependencies.cpp-manifest-invalid", "vcpkg dependencies must be an array"
        )
    for item in dependencies:
        name = (
            item
            if isinstance(item, str)
            else item.get("name")
            if isinstance(item, Mapping)
            else None
        )
        if not isinstance(name, str):
            raise DependencyObservationError(
                "dependencies.cpp-manifest-invalid", "vcpkg dependency is invalid"
            )
        result.add(_normalized_package_name(name))
    return result


# Neither name is a distribution and neither appears in sys.stdlib_module_names:
# `__future__` is a compiler directive module, and `__main__` always exists at
# runtime for the executing entrypoint. A zipapp entry that imports `__main__`
# would otherwise be reported as an undeclared external dependency.
_PYTHON_NON_DISTRIBUTION_MODULES = frozenset({"__future__", "__main__"})


def python_import_names(content: str) -> set[str]:
    """Return normalized non-stdlib top-level imports from Python source."""

    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        raise DependencyObservationError(
            "dependencies.python-source-invalid", "generated Python source is invalid"
        ) from exc
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(item.name.partition(".")[0] for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            result.add(node.module.partition(".")[0])
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    return {
        _normalized_package_name(item)
        for item in result
        if item not in stdlib and item not in _PYTHON_NON_DISTRIBUTION_MODULES
    }


def _javascript_imports(content: str) -> set[str]:
    return {
        _normalized_package_name(name) for name in javascript_package_imports(content)
    }


def _rust_imports(content: str) -> set[str]:
    return {
        _normalized_package_name(item)
        for item in _RUST_IMPORT.findall(content)
        if item not in _RUST_BUILTINS
    }


def _require_local_cpp_includes(
    path: str, content: str, files: Mapping[str, str]
) -> None:
    parent = PurePosixPath(path).parent
    for include in _CPP_QUOTED_INCLUDE.findall(content):
        candidates = {
            posixpath.normpath((parent / include).as_posix()),
            posixpath.normpath(include),
        }
        path_parts = PurePosixPath(path).parts
        if path_parts and path_parts[0] == "source":
            candidates.add(
                posixpath.normpath((PurePosixPath("source") / include).as_posix())
            )
        if candidates.isdisjoint(files):
            raise DependencyObservationError(
                "dependencies.cpp-local-include-missing",
                f"generated C++ source includes missing local file {include!r}",
            )
