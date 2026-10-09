"""Read-only, explicit monorepo custody planning; no conversion mutations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.harness_tree import capture_retained_source_scope
from literate_ai.contracts import canonical_identity

SELECTION_SCHEMA = "literate-ai/monorepo-adoption-selection@1"
PLAN_SCHEMA = "literate-ai/monorepo-adoption-plan@1"
_MARKERS = frozenset(
    {
        "repo.sh",
        "repo.bat",
        "repo.toml",
        "Makefile",
        "GNUMakefile",
        "GNUmakefile",
        "makefile",
        "CMakeLists.txt",
        "MODULE.bazel",
        "WORKSPACE",
        "WORKSPACE.bazel",
        "Cargo.toml",
        "mix.exs",
        "go.mod",
        "package.json",
        "pyproject.toml",
        "setup.cfg",
    }
)
_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
_RESERVED = frozenset({"con", "prn", "aux", "nul"}) | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}
_MAX_SELECTION_BYTES = 256 * 1024


class MonorepoAdoptionError(ValueError):
    def __init__(self, suffix: str, message: str) -> None:
        self.code = f"monorepo.{suffix}"
        self.message = message
        super().__init__(message)


def _fail(suffix: str, message: str) -> None:
    raise MonorepoAdoptionError(suffix, message)


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value:
        _fail("path_invalid", "paths must be nonempty project-relative POSIX paths")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != value
        or ".." in parsed.parts
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
    ):
        _fail("path_invalid", f"noncanonical project-relative path: {value}")
    return value


def _beneath(path: str, prefix: str) -> bool:
    return prefix == "." or path == prefix or path.startswith(prefix + "/")


def _direct(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(_relative(relative)).parts:
        current = current / part
        if stat_is_link_or_reparse(current.lstat()):
            _fail("indirect_source", f"refinement refuses indirect custody: {relative}")
        if current.is_dir() and (
            (current / ".git").exists() or (current / ".git").is_symlink()
        ):
            _fail(
                "separate_history",
                "nested Git histories require separate orchestration",
            )
    return current


def build_root_candidates(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Group existing source markers and stages, without assigning authority."""
    grouped: dict[str, set[str]] = {}
    for raw in inventory["source_scope"]["paths"]:
        path = PurePosixPath(raw)
        if path.name in _MARKERS:
            grouped.setdefault(path.parent.as_posix(), set()).add(raw)
    # Preserve detector evidence even when a driver uses another filename.
    for finding in inventory["findings"]:
        if finding["detector_id"].startswith("build-system."):
            path = PurePosixPath(finding["path"])
            grouped.setdefault(path.parent.as_posix(), set()).add(path.as_posix())
    return [
        {
            "root": root,
            "markers": sorted(markers),
            "stages": [
                stage for stage in inventory["stages"] if stage.get("cwd", ".") == root
            ],
            "selected": False,
            "authority": "candidate-only",
        }
        for root, markers in sorted(grouped.items())
    ]


def _fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail(
            "selection_invalid",
            f"{label} requires exactly: {', '.join(sorted(expected))}",
        )
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("selection_invalid", f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _consume_direct_file(
    path: Path,
    consume: Callable[[bytes], Any],
    *,
    maximum_bytes: int | None = None,
) -> os.stat_result:
    """Read one observed regular file, with bounded growth and descriptor custody.

    Parent checks before and after opening detect indirect custody. They are not
    an OS sandbox against a malicious process with the same user's permissions.
    """
    path = path.absolute()

    def signature(node: os.stat_result) -> tuple[int, ...]:
        return (
            node.st_dev,
            node.st_ino,
            node.st_size,
            node.st_mtime_ns,
            node.st_ctime_ns,
            node.st_mode,
        )

    try:
        require_safe_directory(path.parent)
        before = path.lstat()
        if stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
            _fail("source_invalid", "monorepo inputs must be direct regular files")
        if maximum_bytes is not None and before.st_size > maximum_bytes:
            _fail("input_too_large", "monorepo input exceeds its byte limit")
        flags = os.O_RDONLY
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY", "O_CLOEXEC"):
            flags |= getattr(os, name, 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            named_signature, opened_signature = signature(before), signature(opened)
            require_safe_directory(path.parent)
            # Windows ctime can denote different clocks across stat APIs.
            # Bind shared fields and recheck each complete observation separately.
            if (
                stat_is_link_or_reparse(opened)
                or not stat.S_ISREG(opened.st_mode)
                or opened_signature[:4] != named_signature[:4]
                or opened_signature[-1] != named_signature[-1]
                or signature(path.lstat()) != named_signature
            ):
                _fail("source_changed", "monorepo input changed before reading")
            # Never chase a file that grows indefinitely. One extra byte detects
            # growth even if the writer later restores the original file size.
            remaining = before.st_size + 1
            total = 0
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                total += len(chunk)
                remaining -= len(chunk)
                if total > before.st_size:
                    _fail("source_changed", "monorepo input grew while reading")
                consume(chunk)
            require_safe_directory(path.parent)
            if (
                total != before.st_size
                or signature(os.fstat(descriptor)) != opened_signature
                or signature(path.lstat()) != named_signature
            ):
                _fail("source_changed", "monorepo input changed while reading")
        finally:
            os.close(descriptor)
    except (OSError, UnsafeFilesystemPathError) as exc:
        _fail("source_invalid", f"monorepo input is unavailable or indirect: {exc}")
    return before


def _read_direct_bytes(path: Path, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    _consume_direct_file(path, chunks.append, maximum_bytes=maximum_bytes)
    return b"".join(chunks)


def _selection(path: Path) -> dict[str, Any]:
    # Bound reads even when a declared selection grows during inspection.
    try:
        raw = _read_direct_bytes(path, _MAX_SELECTION_BYTES)
    except MonorepoAdoptionError as exc:
        if exc.code == "monorepo.input_too_large":
            _fail("selection_invalid", "root plan exceeds 256 KiB")
        if exc.code == "monorepo.source_invalid":
            _fail("selection_invalid", exc.message)
        raise
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        _fail("selection_invalid", f"invalid root-plan JSON: {exc}")
    value = _fields(value, {"schema", "components", "shared_sources"}, "root plan")
    if value["schema"] != SELECTION_SCHEMA:
        _fail("selection_invalid", f"root plan requires {SELECTION_SCHEMA}")
    return value


def _source_members(root: Path, paths: list[str]) -> list[dict[str, Any]]:
    members = []
    for relative in paths:
        path = _direct(root, relative)
        digest = hashlib.sha256()
        before = _consume_direct_file(path, digest.update)
        after = _direct(root, relative).lstat()
        if any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        ):
            _fail("source_changed", f"source changed during planning: {relative}")
        members.append(
            {
                "path": relative,
                "identity": "sha256:" + digest.hexdigest(),
                "executable": bool(before.st_mode & 0o111),
            }
        )
    return members


def plan_monorepo_adoption(
    root: Path,
    selection_path: Path,
    inventory: dict[str, Any],
    *,
    submodules: list[Any],
) -> dict[str, Any]:
    """Validate explicit ownership and commands, binding exact admitted bytes."""
    if submodules:
        _fail(
            "separate_history", "submodules require the separate orchestration profile"
        )
    selection = _selection(selection_path)
    components = selection["components"]
    shared = selection["shared_sources"]
    if not isinstance(components, list) or len(components) < 2:
        _fail(
            "selection_invalid", "refinement requires at least two selected Components"
        )
    if not isinstance(shared, list):
        _fail("selection_invalid", "shared_sources must be an explicit list")
    paths = inventory["source_scope"]["paths"]
    candidates = {item["root"] for item in build_root_candidates(inventory)}
    by_name: dict[str, dict[str, Any]] = {}
    for raw in components:
        item = _fields(raw, {"name", "root", "commands"}, "Component")
        name = item["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in _RESERVED:
            _fail(
                "name_invalid", "Component names must be short portable lowercase slugs"
            )
        if name in by_name:
            _fail("duplicate_component", f"duplicate Component name: {name}")
        relative = _relative(item["root"])
        if relative not in candidates:
            _fail(
                "root_undetected",
                f"selected root is not a detected candidate: {relative}",
            )
        if not _direct(root, relative).is_dir():
            _fail("root_invalid", f"selected root must be a directory: {relative}")
        if relative != "." and (root / relative / ".git").exists():
            _fail(
                "separate_history",
                "nested Git histories require separate orchestration",
            )
        if any(
            _beneath(relative, other["root"]) or _beneath(other["root"], relative)
            for other in by_name.values()
        ):
            _fail("root_overlap", "selected Component roots must not overlap")
        by_name[name] = item
    owners: dict[str, str] = {}
    consumers: dict[str, set[str]] = {path: set() for path in paths}
    for path in paths:
        for name, item in by_name.items():
            if _beneath(path, item["root"]):
                owners[path] = name
    prefixes: list[str] = []
    for raw in shared:
        item = _fields(raw, {"path", "owner", "consumers"}, "shared source")
        prefix = _relative(item["path"])
        if any(
            _beneath(prefix, other) or _beneath(other, prefix) for other in prefixes
        ):
            _fail("ownership_overlap", "shared-source assignments must not overlap")
        if any(
            _beneath(prefix, c["root"]) or _beneath(c["root"], prefix)
            for c in components
        ):
            _fail(
                "ownership_overlap", "shared-source assignment overlaps a selected root"
            )
        names = item["consumers"]
        owner = item["owner"]
        if not isinstance(owner, str) or owner not in by_name:
            _fail("owner_invalid", "shared-source owner must name a selected Component")
        if (
            not isinstance(names, list)
            or not all(isinstance(n, str) and n in by_name for n in names)
            or len(set(names)) != len(names)
            or owner in names
        ):
            _fail(
                "consumer_invalid",
                "shared consumers must be distinct other Component names",
            )
        matched = [path for path in paths if _beneath(path, prefix)]
        if not matched:
            _fail(
                "source_unadmitted",
                f"shared source is outside captured membership: {prefix}",
            )
        for path in matched:
            owners[path] = owner
            consumers[path].update(names)
        prefixes.append(prefix)
    unowned = sorted(set(paths) - owners.keys())
    if unowned:
        _fail(
            "ownership_incomplete",
            "source ownership is missing: " + ", ".join(unowned[:10]),
        )
    planned = []
    for name, item in sorted(by_name.items()):
        commands = item["commands"]
        if not isinstance(commands, list) or not commands:
            _fail("commands_missing", f"explicit per-root commands required for {name}")
        stage_ids: set[str] = set()
        for raw in commands:
            stage = _fields(raw, {"id", "command", "cwd", "evidence"}, "command")
            stage_id, command = stage["id"], stage["command"]
            if not isinstance(stage_id, str) or stage_id not in {
                "build",
                "test",
                "package",
                "ci",
            }:
                _fail(
                    "command_invalid", "command id must be build, test, package or ci"
                )
            if stage_id in stage_ids:
                _fail("command_invalid", f"duplicate stage for {name}: {stage_id}")
            if not isinstance(command, str) or not command.strip() or "\0" in command:
                _fail("command_invalid", "command must be nonempty text without NUL")
            stage_ids.add(stage_id)
            cwd = _relative(stage["cwd"])
            if cwd != "." and not _beneath(cwd, item["root"]):
                _fail(
                    "command_cwd_invalid",
                    "command cwd must be repository root or within its Component",
                )
            if not _direct(root, cwd).is_dir():
                _fail(
                    "command_cwd_invalid",
                    "command cwd must be an existing direct directory",
                )
            evidence = _relative(stage["evidence"])
            if evidence not in owners or (
                owners[evidence] != name and name not in consumers[evidence]
            ):
                _fail(
                    "command_evidence_invalid",
                    "command evidence must be owned or explicitly shared source",
                )
        planned.append(
            {
                **item,
                "owned_sources": sorted(path for path in paths if owners[path] == name),
                "shared_inputs": sorted(
                    path for path in paths if name in consumers[path]
                ),
            }
        )
    members = _source_members(root, paths)
    if capture_retained_source_scope(root)["paths"] != paths:
        _fail("source_changed", "source membership changed during planning; replan")
    if _source_members(root, paths) != members:
        _fail("source_changed", "source bytes changed during planning; replan")
    if _selection(selection_path) != selection:
        _fail("selection_changed", "root selection changed during planning; replan")
    plan = {
        "schema": PLAN_SCHEMA,
        "selection": selection,
        "components": planned,
        "source_scope": inventory["source_scope"],
        "source_members": members,
        "selection_identity": canonical_identity(selection).uri,
        "source_identity": canonical_identity(members).uri,
        "writes": False,
        "execution_performed": False,
        "apply_supported": True,
        "blockers": [],
    }
    return {**plan, "plan_identity": canonical_identity(plan).uri}
