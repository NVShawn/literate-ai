"""Project-scoped generated-source and object cache directories.

The directory names are an operator interface, not authority.  Every reusable entry
inside them is independently content/provenance keyed and is revalidated before use.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from literate_ai._cache_lock import (
    CacheLockError,
    cache_lock_path,
    exclusive_cache_lock,
)
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    path_is_link_or_reparse,
    require_safe_directory,
)
from literate_ai.adapters.user_paths import UserPathError, resolve_user_home
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.projects import ProjectConfigurationStore, ProjectError

BUILD_DIR_ENVIRONMENT = "BUILD_DIR"
OBJ_DIR_ENVIRONMENT = "OBJ_DIR"
DEFAULT_BUILD_DIRECTORY = "generated"
DEFAULT_OBJECT_DIRECTORY = "_build"
_MARKER = ".litai-cache-root.json"
_LOCK_DIRECTORY = ".litai-cache-locks"
_MARKER_SCHEMA = "literate-ai/cache-root@1"
_PROJECT_AUTHORITY_ROOT_FIELDS = (
    "component_roots",
    "flavor_roots",
    "skill_roots",
    "mcp_roots",
    "workflow_roots",
    "routing_roots",
    "documentation_roots",
)
_BASELINE_AUTHORITY_ROOTS = (
    ".codegraph",
    ".cursor",
    ".git",
    ".github",
    "agents",
    "components",
    "docs",
    "flavors",
    "openspec",
    "routing",
    "samples",
    "schemas",
    "scripts",
    "skills",
    "mcps",
    "specs",
    "src",
    "tests",
    "tools",
    "verification",
    "workflows",
)


class CacheDirectoryError(RuntimeError):
    """A configured cache root is unsafe or owned by another project."""


@dataclass(frozen=True, slots=True)
class CacheDirectories:
    project_root: Path
    build_dir: Path
    obj_dir: Path

    @property
    def identity(self) -> ContentIdentity:
        """Bind the exact host directory custody used by one lifecycle."""

        return canonical_identity(
            {
                "schema": "literate-ai/cache-directory-custody@1",
                "project_root": str(self.project_root),
                "build_dir": str(self.build_dir),
                "obj_dir": str(self.obj_dir),
            }
        )

    def environment(self) -> dict[str, str]:
        return {
            BUILD_DIR_ENVIRONMENT: str(self.build_dir),
            OBJ_DIR_ENVIRONMENT: str(self.obj_dir),
        }


def resolve_cache_directories(
    project_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> CacheDirectories:
    """Resolve portable defaults, with relative overrides anchored at the project."""

    root = Path(project_root).resolve(strict=True)
    configured = os.environ if environment is None else environment
    build_dir = _configured_path(
        configured.get(BUILD_DIR_ENVIRONMENT),
        default=root / DEFAULT_BUILD_DIRECTORY,
        project_root=root,
    )
    obj_dir = _configured_path(
        configured.get(OBJ_DIR_ENVIRONMENT),
        default=root / DEFAULT_OBJECT_DIRECTORY,
        project_root=root,
    )
    return bind_cache_directories(root, build_dir=build_dir, obj_dir=obj_dir)


def select_cache_directories(
    project_root: Path,
    *,
    build_dir: str | Path | None = None,
    obj_dir: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> CacheDirectories:
    """Resolve roots by explicit request, then environment, then portable default.

    An explicitly supplied root is the operator's most direct statement of intent, so it
    wins over an environment variable that disagrees rather than conflicting with it.
    Every selection still binds through ``bind_cache_directories`` and is therefore
    subject to the same safety, distinctness, and nesting rules.
    """

    root = Path(project_root).resolve(strict=True)
    configured = os.environ if environment is None else environment
    selected_build = _selected_path(
        build_dir,
        fallback=configured.get(BUILD_DIR_ENVIRONMENT),
        default=root / DEFAULT_BUILD_DIRECTORY,
        project_root=root,
    )
    selected_obj = _selected_path(
        obj_dir,
        fallback=configured.get(OBJ_DIR_ENVIRONMENT),
        default=root / DEFAULT_OBJECT_DIRECTORY,
        project_root=root,
    )
    return bind_cache_directories(root, build_dir=selected_build, obj_dir=selected_obj)


def _selected_path(
    requested: str | Path | None,
    *,
    fallback: str | None,
    default: Path,
    project_root: Path,
) -> Path:
    raw = None if requested is None else str(requested)
    if raw is not None and raw.strip():
        return _configured_path(raw, default=default, project_root=project_root)
    return _configured_path(fallback, default=default, project_root=project_root)


def bind_cache_directories(
    project_root: Path,
    *,
    build_dir: Path,
    obj_dir: Path,
) -> CacheDirectories:
    """Bind explicit lifecycle roots without consulting process environment."""

    root = Path(project_root).resolve(strict=True)
    bound_build = Path(build_dir).expanduser().resolve()
    bound_obj = Path(obj_dir).expanduser().resolve()
    _require_safe_root(bound_build, project_root=root, label=BUILD_DIR_ENVIRONMENT)
    _require_safe_root(bound_obj, project_root=root, label=OBJ_DIR_ENVIRONMENT)
    if bound_build == bound_obj:
        raise CacheDirectoryError("BUILD_DIR and OBJ_DIR must be different directories")
    if bound_build.is_relative_to(bound_obj):
        raise CacheDirectoryError("BUILD_DIR cannot be nested beneath OBJ_DIR")
    return CacheDirectories(root, bound_build, bound_obj)


def ensure_cache_directory(
    path: Path,
    *,
    kind: str,
    project_root: Path,
    required_subdirectories: Iterable[Path] = (),
) -> Path:
    """Create or verify one explicitly managed cache root."""

    if kind not in {"generated-source", "object"}:
        raise ValueError("cache directory kind is invalid")
    root = Path(project_root).resolve(strict=True)
    supplied = Path(path)
    if path_is_link_or_reparse(supplied):
        raise CacheDirectoryError(
            f"{kind} cache root cannot be a link or reparse point"
        )
    candidate = supplied.resolve()
    _require_safe_root(candidate, project_root=root, label=kind)
    descendants = tuple(
        _cache_subdirectory(candidate, relative) for relative in required_subdirectories
    )
    marker = candidate / _MARKER
    expected = canonical_json_bytes(
        {
            "schema": _MARKER_SCHEMA,
            "kind": kind,
            "project_owner": _project_owner(root),
        }
    )
    lock_path = cache_lock_path(root, candidate, "lifecycle.lock")
    try:
        with exclusive_cache_lock(lock_path):
            try:
                ensure_safe_directory(candidate)
            except UnsafeFilesystemPathError as exc:
                raise CacheDirectoryError(
                    f"{kind} cache root has an unsafe path component"
                ) from exc
            if path_is_link_or_reparse(candidate) or not candidate.is_dir():
                raise CacheDirectoryError(
                    f"{kind} cache root must be a regular directory"
                )
            if marker.exists() or path_is_link_or_reparse(marker):
                _require_expected_marker(marker, expected=expected, kind=kind)
            else:
                if any(candidate.iterdir()) and not _default_disposable_object_root(
                    candidate, project_root=root, kind=kind
                ):
                    raise CacheDirectoryError(
                        f"refusing to adopt non-empty unmarked {kind} cache root: "
                        f"{candidate}"
                    )
                _require_disposable_tree_safe(candidate)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f"{_MARKER}.tmp-", dir=candidate
                )
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(expected)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, marker)
                finally:
                    temporary.unlink(missing_ok=True)
            try:
                for descendant in descendants:
                    ensure_safe_directory(descendant)
            except UnsafeFilesystemPathError as exc:
                raise CacheDirectoryError(
                    f"{kind} cache subtree has an unsafe path component"
                ) from exc
    except CacheLockError as exc:
        raise CacheDirectoryError(f"{kind} cache root lock is unavailable") from exc
    return candidate


def clean_cache_directories(
    project_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
    remove_generated_sources: bool = False,
    preserve_object_subdirectories: Iterable[Path] = (),
) -> tuple[Path, ...]:
    """Remove only marked roots; ``really-clean`` additionally removes BUILD_DIR."""

    directories = resolve_cache_directories(project_root, environment=environment)
    preserved_object_paths = frozenset(
        _preserved_object_subdirectory(directories.obj_dir, relative)
        for relative in preserve_object_subdirectories
    )
    targets = [(directories.obj_dir, "object")]
    if remove_generated_sources and directories.build_dir != directories.obj_dir:
        targets.append((directories.build_dir, "generated-source"))
    # Remove nested paths first for explicit overrides that place one cache within the
    # other. The defaults keep accepted source and disposable host output as siblings.
    ordered = sorted(set(targets), key=lambda item: len(item[0].parts), reverse=True)
    removed: list[Path] = []
    locks = {
        target: cache_lock_path(directories.project_root, target, "lifecycle.lock")
        for target, _expected_kind in ordered
    }
    try:
        with ExitStack() as stack:
            for lock_path in sorted(locks.values()):
                stack.enter_context(exclusive_cache_lock(lock_path))
            for target, expected_kind in ordered:
                if not target.exists():
                    continue
                _require_managed_for_removal(
                    target,
                    project_root=directories.project_root,
                    expected_kind=expected_kind,
                )
            for target, _expected_kind in ordered:
                if target.exists():
                    # The manifest is mutable authority outside the cache lock
                    # namespace. Recompute overlap at the last possible point so a
                    # newly declared catalog can never be removed with stale safety
                    # information.
                    _require_safe_root(
                        target,
                        project_root=directories.project_root,
                        label="cache cleanup target",
                    )
                    if target == directories.obj_dir and preserved_object_paths:
                        removed.extend(
                            _clean_cache_root_except(
                                target,
                                preserved=preserved_object_paths,
                            )
                        )
                    else:
                        shutil.rmtree(target)
                        removed.append(target)
    except CacheLockError as exc:
        raise CacheDirectoryError("cache cleanup lock is unavailable") from exc
    return tuple(removed)


def _preserved_object_subdirectory(root: Path, relative: Path) -> Path:
    candidate = _cache_subdirectory(root, Path(relative))
    if candidate.parent != root:
        raise CacheDirectoryError(
            "preserved object cache entries must be direct subdirectories"
        )
    return candidate


def _clean_cache_root_except(
    root: Path,
    *,
    preserved: frozenset[Path],
) -> tuple[Path, ...]:
    """Remove disposable children while retaining exact session-tool roots."""

    marker = root / _MARKER
    for path in preserved:
        if not path.exists() and not path_is_link_or_reparse(path):
            continue
        if path_is_link_or_reparse(path) or not path.is_dir():
            raise CacheDirectoryError(
                "preserved object cache entry must be a regular directory"
            )
    removed: list[Path] = []
    for child in sorted(root.iterdir()):
        if child == marker or child in preserved:
            continue
        if path_is_link_or_reparse(child) or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            raise CacheDirectoryError(
                "object cache contains an unsupported filesystem entry"
            )
        removed.append(child)
    return tuple(removed)


def require_cache_directory_current(
    path: Path, *, kind: str, project_root: Path
) -> None:
    """Revalidate a managed cache root while its lifecycle lock is held."""

    root = Path(project_root).resolve(strict=True)
    candidate = Path(path).resolve(strict=False)
    try:
        require_safe_directory(candidate)
    except UnsafeFilesystemPathError as exc:
        raise CacheDirectoryError(
            f"{kind} cache root is unavailable or unsafe"
        ) from exc
    if path_is_link_or_reparse(path) or not candidate.is_dir():
        raise CacheDirectoryError(f"{kind} cache root is unavailable or unsafe")
    _require_expected_marker(
        candidate / _MARKER,
        expected=canonical_json_bytes(
            {
                "schema": _MARKER_SCHEMA,
                "kind": kind,
                "project_owner": _project_owner(root),
            }
        ),
        kind=kind,
    )


def _configured_path(raw: str | None, *, default: Path, project_root: Path) -> Path:
    if raw is None or not raw.strip():
        return default.resolve()
    expanded = Path(raw.strip()).expanduser()
    supplied = expanded if expanded.is_absolute() else project_root / expanded
    if path_is_link_or_reparse(supplied):
        raise CacheDirectoryError(
            "configured cache root cannot be a link or reparse point"
        )
    return supplied.resolve()


def _cache_subdirectory(root: Path, relative: Path) -> Path:
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError("cache subdirectory must be a canonical relative path")
    return root.joinpath(*candidate.parts)


def _require_safe_root(path: Path, *, project_root: Path, label: str) -> None:
    try:
        home = Path(resolve_user_home()).resolve()
    except UserPathError as exc:
        raise CacheDirectoryError("user home directory is unavailable") from exc
    forbidden = {
        Path(path.anchor).resolve(),
        home,
        project_root,
        project_root / _LOCK_DIRECTORY,
    }
    if path in forbidden:
        raise CacheDirectoryError(f"{label} resolves to a protected directory")
    protected_roots = (*_authority_roots(project_root), project_root / _LOCK_DIRECTORY)
    for protected in protected_roots:
        if path == protected or path.is_relative_to(protected):
            raise CacheDirectoryError(
                f"{label} resolves within a protected authority directory"
            )
        if protected.is_relative_to(path):
            raise CacheDirectoryError(
                f"{label} contains a protected authority directory"
            )


def _authority_roots(project_root: Path) -> tuple[Path, ...]:
    roots = {
        (project_root / relative).resolve(strict=False)
        for relative in _BASELINE_AUTHORITY_ROOTS
    }
    store = ProjectConfigurationStore(project_root)
    if not store.path.exists() and not path_is_link_or_reparse(store.path):
        return tuple(sorted(roots))
    try:
        definition = store.read().definition
    except ProjectError as exc:
        raise CacheDirectoryError(
            "project manifest is invalid during cache validation"
        ) from exc
    for field in _PROJECT_AUTHORITY_ROOT_FIELDS:
        for value in getattr(definition, field):
            roots.add(project_root.joinpath(*Path(value).parts).resolve(strict=False))
    return tuple(sorted(roots))


def _require_managed_for_removal(
    path: Path,
    *,
    project_root: Path,
    expected_kind: str,
) -> None:
    if path_is_link_or_reparse(path) or not path.is_dir():
        raise CacheDirectoryError("refusing to remove a non-directory cache root")
    marker = path / _MARKER
    if not marker.exists() and not path_is_link_or_reparse(marker):
        if _default_disposable_object_root(
            path, project_root=project_root, kind=expected_kind
        ):
            _require_disposable_tree_safe(path)
            return
        raise CacheDirectoryError(
            f"refusing to remove unmarked cache directory: {path}"
        )
    if path_is_link_or_reparse(marker) or not marker.is_file():
        raise CacheDirectoryError(
            f"refusing to remove unmarked cache directory: {path}"
        )
    try:
        document = json.loads(marker.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CacheDirectoryError("cache directory marker is invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema") != _MARKER_SCHEMA
        or document.get("kind") != expected_kind
        or document.get("project_owner") != _project_owner(project_root)
    ):
        raise CacheDirectoryError("cache directory marker does not authorize removal")


def _default_disposable_object_root(
    path: Path, *, project_root: Path, kind: str
) -> bool:
    """Recognize the one conventionally destructible, repository-local boundary."""

    return kind == "object" and path == project_root / DEFAULT_OBJECT_DIRECTORY


def _require_disposable_tree_safe(path: Path) -> None:
    """Never follow internal links/reparses; reject other special entries."""

    for current, directories, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        retained_directories: list[str] = []
        for name in directories:
            candidate = current_path / name
            if path_is_link_or_reparse(candidate):
                continue
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                raise CacheDirectoryError(
                    "default _build tree changed during safety validation"
                ) from exc
            if stat.S_ISDIR(metadata.st_mode):
                retained_directories.append(name)
                continue
            raise CacheDirectoryError(
                "default _build tree contains a non-directory tree entry"
            )
        directories[:] = retained_directories
        for name in files:
            candidate = current_path / name
            if path_is_link_or_reparse(candidate):
                continue
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                raise CacheDirectoryError(
                    "default _build tree changed during safety validation"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise CacheDirectoryError(
                    "default _build tree contains a non-regular entry"
                )


def _project_owner(project_root: Path) -> str:
    """Return a portable owner when a canonical project manifest is available."""

    try:
        project_id = (
            ProjectConfigurationStore(project_root).read().definition.project_id
        )
    except ProjectError:
        project_id = None
    if project_id is not None:
        return f"project:{project_id}"
    # Non-project callers (primarily focused adapter tests) remain path scoped.
    return f"path:{project_root}"


def _require_expected_marker(marker: Path, *, expected: bytes, kind: str) -> None:
    if (
        path_is_link_or_reparse(marker)
        or not marker.is_file()
        or marker.read_bytes() != expected
    ):
        raise CacheDirectoryError(
            f"{kind} cache root is owned by another project or cache kind"
        )


__all__ = [
    "BUILD_DIR_ENVIRONMENT",
    "CacheDirectories",
    "CacheDirectoryError",
    "DEFAULT_BUILD_DIRECTORY",
    "DEFAULT_OBJECT_DIRECTORY",
    "OBJ_DIR_ENVIRONMENT",
    "bind_cache_directories",
    "clean_cache_directories",
    "ensure_cache_directory",
    "require_cache_directory_current",
    "resolve_cache_directories",
    "select_cache_directories",
]
