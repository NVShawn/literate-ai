"""Authored-source scope and bounded tree evidence for retained harnesses."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

SOURCE_SCOPE_SCHEMA = "literate-ai/retained-source-scope@1"
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".cache",
        ".gradle",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "build-win",
        "_build",
        "_repo",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)


def capture_retained_source_scope(
    root: Path, *, required_paths: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Capture Git-visible source, falling back to a pruned filesystem walk."""

    resolved = root.resolve()
    git_scope = _git_visible_scope(resolved)
    if git_scope is None:
        paths, excluded = _filesystem_paths(resolved)
        policy = "filesystem-pruned@1"
    else:
        paths, excluded = git_scope
        policy = "git-visible@1"
    for required in required_paths:
        relative = _safe_relative_path(required)
        candidate = resolved.joinpath(*relative.parts)
        if candidate.is_file() or candidate.is_symlink():
            paths.add(relative.as_posix())
    ordered_paths = tuple(sorted(paths))
    ordered_excluded = tuple(sorted(excluded))
    return {
        "schema": SOURCE_SCOPE_SCHEMA,
        "policy": policy,
        "paths": list(ordered_paths),
        "paths_identity": _identity(list(ordered_paths)),
        "excluded_roots": list(ordered_excluded),
        "excluded": _excluded_summary(ordered_excluded),
    }


def copy_retained_source_tree(
    source_root: Path, destination_root: Path, scope: object
) -> None:
    """Copy only the captured authored-source paths into a fresh directory."""

    source = source_root.resolve()
    paths, _excluded = _validated_scope(scope)
    if destination_root.exists():
        raise ValueError("retained source destination already exists")
    destination_root.mkdir(parents=True)
    for raw in paths:
        relative = _safe_relative_path(raw)
        source_path = source.joinpath(*relative.parts)
        destination = destination_root.joinpath(*relative.parts)
        if not source_path.is_file() and not source_path.is_symlink():
            raise ValueError(f"retained source path is unavailable: {raw}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_symlink():
            destination.symlink_to(os.readlink(source_path))
        else:
            shutil.copy2(source_path, destination)


def observe_retained_tree(root: Path, scope: object) -> dict[str, Any]:
    """Observe pre-command source authority and post-command artifacts separately."""

    resolved = root.resolve()
    original_paths, declared_excluded = _validated_scope(scope)
    current_paths, observed_excluded = _filesystem_paths(
        resolved,
        excluded_roots=frozenset(declared_excluded),
        protected_paths=frozenset(original_paths),
    )
    source_members, source_bytes, source_missing = _observed_members(
        resolved, original_paths
    )
    artifact_members, artifact_bytes, artifact_missing = _observed_members(
        resolved, tuple(sorted(current_paths | set(original_paths)))
    )
    source_tree = {
        "identity": _identity(source_members),
        "file_count": len(source_members) - source_missing,
        "missing_count": source_missing,
        "total_bytes": source_bytes,
        "scope": "authored-source",
    }
    excluded = tuple(sorted(set(declared_excluded) | observed_excluded))
    excluded_summary = _excluded_summary(excluded, root=resolved)
    tree = {
        "identity": _identity(
            {"artifact_members": artifact_members, "excluded": excluded_summary}
        ),
        "file_count": len(artifact_members) - artifact_missing,
        "missing_count": artifact_missing,
        "total_bytes": artifact_bytes,
        "scope": "post-command-artifacts-plus-excluded-root-accounting",
        "excluded": excluded_summary,
    }
    return {"tree": tree, "source_tree": source_tree, "excluded": excluded_summary}


def _observed_members(
    root: Path, paths: tuple[str, ...]
) -> tuple[list[dict[str, Any]], int, int]:
    members: list[dict[str, Any]] = []
    total_bytes = 0
    missing_count = 0
    for raw in paths:
        relative = _safe_relative_path(raw)
        path = root.joinpath(*relative.parts)
        if path.is_symlink():
            content = os.fsencode(os.readlink(path))
            kind = "symlink"
        elif path.is_file():
            content = path.read_bytes()
            kind = "file"
        else:
            members.append({"path": raw, "kind": "missing"})
            missing_count += 1
            continue
        normalized = _normalized_content(content, root=root)
        total_bytes += len(normalized)
        members.append(
            {
                "path": raw,
                "kind": kind,
                "size": len(normalized),
                "sha256": hashlib.sha256(normalized).hexdigest(),
            }
        )
    return members, total_bytes, missing_count


def _git_visible_scope(root: Path) -> tuple[set[str], set[str]] | None:
    git = shutil.which("git")
    if git is None:
        return None
    top_result = _run_git(root, git, "rev-parse", "--show-toplevel")
    if top_result is None:
        return None
    try:
        top = Path(os.fsdecode(top_result.strip())).resolve()
        prefix_path = root.relative_to(top)
    except (UnicodeError, ValueError):
        return None
    prefix = prefix_path.as_posix() if prefix_path.parts else "."
    visible = _run_git(
        top,
        git,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        prefix,
    )
    deleted = _run_git(top, git, "ls-files", "-z", "--deleted", "--", prefix)
    ignored = _run_git(
        top,
        git,
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "--",
        prefix,
    )
    if visible is None or deleted is None or ignored is None:
        return None
    deleted_paths = set(_relative_git_paths(deleted, prefix))
    paths = {
        path
        for path in _relative_git_paths(visible, prefix)
        if path not in deleted_paths
        and ((root / path).is_file() or (root / path).is_symlink())
    }
    excluded = {
        path.rstrip("/")
        for path in _relative_git_paths(ignored, prefix)
        if path.rstrip("/")
    }
    for gitlink in _gitlink_paths(top, root, prefix, git):
        child = root.joinpath(*PurePosixPath(gitlink).parts)
        if child.is_symlink() or not child.is_dir():
            continue
        child_top = _run_git(child, git, "rev-parse", "--show-toplevel")
        if child_top is None:
            continue
        try:
            if Path(os.fsdecode(child_top.strip())).resolve() != child.resolve():
                # An uninitialized gitlink directory still belongs to the parent
                # repository. It is not an independent child source authority.
                continue
        except (UnicodeError, OSError):
            continue
        child_scope = _git_visible_scope(child)
        if child_scope is None:
            continue
        child_paths, child_excluded = child_scope
        paths.update(f"{gitlink}/{path}" for path in child_paths)
        excluded.update(f"{gitlink}/{path}" for path in child_excluded)
    return paths, _minimal_roots(excluded)


def _gitlink_paths(top: Path, root: Path, prefix: str, git: str) -> tuple[str, ...]:
    """Return initialized-or-not gitlink paths beneath this capture root."""

    staged = _run_git(top, git, "ls-files", "-z", "--stage", "--", prefix)
    if staged is None:
        raise ValueError("Git could not enumerate retained gitlinks")
    marker = "" if prefix == "." else f"{prefix}/"
    result = []
    for record in staged.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator or metadata.split(b" ", 1)[0] != b"160000":
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "Git reported a non-portable retained gitlink path"
            ) from exc
        if marker:
            if not path.startswith(marker):
                continue
            path = path[len(marker) :]
        relative = _safe_relative_path(path)
        candidate = root.joinpath(*relative.parts)
        if candidate == root or not candidate.is_relative_to(root):
            raise ValueError("Git reported an unsafe retained gitlink path")
        result.append(relative.as_posix())
    return tuple(sorted(set(result)))


def _run_git(root: Path, git: str, *arguments: str) -> bytes | None:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            (git, "-c", "core.fsmonitor=false", *arguments),
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _relative_git_paths(content: bytes, prefix: str) -> tuple[str, ...]:
    result: list[str] = []
    marker = "" if prefix == "." else f"{prefix}/"
    for raw in content.split(b"\0"):
        if not raw:
            continue
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "Git reported a non-portable retained-source path"
            ) from exc
        if marker:
            if not path.startswith(marker):
                continue
            path = path[len(marker) :]
        _safe_relative_path(path.rstrip("/"))
        result.append(path)
    return tuple(result)


def _filesystem_paths(
    root: Path,
    *,
    excluded_roots: frozenset[str] = frozenset(),
    protected_paths: frozenset[str] = frozenset(),
) -> tuple[set[str], set[str]]:
    paths: set[str] = set()
    excluded: set[str] = set()
    effective_excluded_roots = set(excluded_roots)
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        kept: list[str] = []
        for name in sorted(names):
            path = current / name
            relative = path.relative_to(root).as_posix()
            protected = any(
                item == relative or item.startswith(f"{relative}/")
                for item in protected_paths
            )
            excluded_by_name = name in EXCLUDED_DIRECTORY_NAMES
            excluded_by_root = _beneath_any(
                relative, frozenset(effective_excluded_roots)
            )
            if excluded_by_name:
                effective_excluded_roots.add(relative)
            if excluded_by_name or excluded_by_root:
                excluded.add(relative)
                if not protected:
                    continue
            if path.is_symlink():
                paths.add(relative)
                continue
            kept.append(name)
        names[:] = kept
        for name in sorted(filenames):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if (
                _beneath_any(relative, frozenset(effective_excluded_roots))
                and relative not in protected_paths
            ):
                excluded.add(relative)
                continue
            if path.is_file() or path.is_symlink():
                paths.add(relative)
    return paths, _minimal_roots(excluded)


def _beneath_any(path: str, roots: frozenset[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def _minimal_roots(paths: set[str]) -> set[str]:
    result: set[str] = set()
    for path in sorted(paths, key=lambda item: (item.count("/"), item)):
        if not _beneath_any(path, frozenset(result)):
            result.add(path)
    return result


def _validated_scope(scope: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(scope, dict) or scope.get("schema") != SOURCE_SCOPE_SCHEMA:
        raise ValueError("retained source scope is invalid")
    paths = scope.get("paths")
    excluded = scope.get("excluded_roots")
    if not isinstance(paths, list) or not isinstance(excluded, list):
        raise ValueError("retained source scope paths are invalid")
    if not all(isinstance(item, str) for item in (*paths, *excluded)):
        raise ValueError("retained source scope contains a non-string path")
    for path in (*paths, *excluded):
        _safe_relative_path(path)
    if paths != sorted(set(paths)) or excluded != sorted(set(excluded)):
        raise ValueError("retained source scope paths are not canonical")
    if scope.get("paths_identity") != _identity(paths):
        raise ValueError("retained source scope identity does not match its paths")
    return tuple(paths), tuple(excluded)


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"retained source path is unsafe: {value!r}")
    return path


def _normalized_content(content: bytes, *, root: Path) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    return (
        text.replace(str(root), "${LEGACY_ROOT}")
        .replace(str(root).replace("\\", "/"), "${LEGACY_ROOT}")
        .replace("\r\n", "\n")
        .encode("utf-8")
    )


def _excluded_summary(
    paths: tuple[str, ...], *, root: Path | None = None
) -> list[dict[str, Any]]:
    counts = Counter(_excluded_class(path) for path in paths)
    present = Counter(
        _excluded_class(path)
        for path in paths
        if root is not None and (root / path).exists()
    )
    return [
        {
            "class": name,
            "root_count": count,
            "present_root_count": present[name] if root is not None else None,
        }
        for name, count in sorted(counts.items())
    ]


def _excluded_class(path: str) -> str:
    names = set(PurePosixPath(path).parts)
    if names & {".venv", "venv", ".tox"}:
        return "virtual-environment"
    if names & {".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache"}:
        return "test-or-analysis-cache"
    if names & {"build", "build-win", "_build", "_repo", "dist", "target"}:
        return "build-output"
    if names & {"node_modules", ".gradle", ".cache"}:
        return "dependency-cache"
    if ".git" in names:
        return "vcs-metadata"
    return "vcs-ignored"


def _identity(value: object) -> str:
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()
