"""Reviewed, additive root initialization; no child or Git metadata mutation.

The new metadata directory serializes competing initializations. Identity checks
detect concurrent changes but do not isolate a malicious same-user process.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_orchestration import RepositoryOrchestration
from literate_ai.projects import CANONICAL_AGENT_SHIMS, project_owns_agent_shim

from .orchestration_planning import plan_orchestration
from .orchestration_scaffold import prepare_orchestration_scaffold
from .project_validation import FilesystemProjectValidationAdapter
from .repository_orchestration import OrchestrationInventoryError, _read_document

_PLAN_SCHEMA = "literate-ai/orchestration-initialization-plan@1"
_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TARGETS = {".literate", "skill.md", "project.md", "literate.project.json"}


def _fail(code: str, message: str) -> None:
    raise OrchestrationInventoryError(code, message)


def _key(node: os.stat_result) -> tuple[int, int, int]:
    return node.st_dev, node.st_ino, node.st_mode


def _read(path: Path) -> bytes:
    return _read_document(
        path, label="orchestration file", error_suffix="initialization_changed"
    )


def _entries(directory: Path):
    for index, child in enumerate(directory.iterdir()):
        if index >= 16384:
            _fail("initialization_limit", "directory entry inventory exceeds its bound")
        yield child


def _no_alias(path: Path) -> None:
    if any(
        child.name.casefold() == path.name.casefold() and child.name != path.name
        for child in _entries(path.parent)
    ):
        _fail("initialization_collision", "initialization path has a portable alias")


def _preflight(root: Path, definition) -> dict[str, tuple[tuple[int, int, int], bytes]]:
    require_safe_directory(root)
    for child in _entries(root):
        if child.name.casefold() in _TARGETS:
            _fail(
                "initialization_collision", "root initialization targets already exist"
            )
        if (
            child.name.casefold() in {"agents.md", "claude.md"}
            and child.name not in CANONICAL_AGENT_SHIMS
            and project_owns_agent_shim(definition, child.name)
        ):
            _fail("initialization_collision", "root agent shim has a portable alias")
    shims = {}
    for relative, marker in CANONICAL_AGENT_SHIMS.items():
        if not project_owns_agent_shim(definition, relative):
            continue
        path = root / relative
        try:
            node = path.lstat()
        except FileNotFoundError:
            continue
        content = _read(path)
        if marker not in content.decode("utf-8") or _key(path.lstat()) != _key(node):
            _fail(
                "initialization_shim_invalid",
                "existing agent shim is invalid or changed",
            )
        shims[relative] = (_key(node), content)
    return shims


def _prepare(root: Path, declaration: Path, project_id: str, version: str):
    require_safe_directory(root.absolute())
    root = root.resolve(strict=True)
    require_safe_directory(root)
    root_key = _key(root.lstat())
    observation = plan_orchestration(root, declaration)
    scaffold = prepare_orchestration_scaffold(
        RepositoryOrchestration.from_dict(observation["repository_authority"]),
        project_id=project_id,
        version=version,
    )
    shims = _preflight(root, scaffold.definition)
    value = {
        "schema": _PLAN_SCHEMA,
        "inspection_plan_identity": observation["plan_identity"],
        "project": scaffold.definition.to_dict(),
        "scaffold_identity": scaffold.identity,
        "files": [
            {
                "path": path,
                "identity": "sha256:" + hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
            for path, content in scaffold.files
        ],
        "preserved_shims": {
            path: "sha256:" + hashlib.sha256(content).hexdigest()
            for path, (_node, content) in shims.items()
        },
        "writes": False,
        "execution": False,
        "apply_supported": True,
        "child_authority": "independent",
        "publication": "not-checked",
    }
    if (
        _key(root.lstat()) != root_key
        or plan_orchestration(root, declaration) != observation
        or _preflight(root, scaffold.definition) != shims
    ):
        _fail("inputs_changed", "initialization inputs changed during planning")
    return (
        root,
        scaffold,
        shims,
        {**value, "plan_identity": canonical_identity(value).uri},
    )


def plan_orchestration_initialization(
    root: Path, declaration: Path, *, project_id: str, version: str
) -> dict[str, Any]:
    """Bind a prospective file set without creating staging or lock files."""
    return _prepare(root, declaration, project_id, version)[3]


def check_orchestration_initialization(
    root: Path,
    declaration: Path,
    *,
    project_id: str,
    version: str,
    expected_plan_identity: str,
) -> dict[str, Any]:
    if (
        not isinstance(expected_plan_identity, str)
        or _IDENTITY.fullmatch(expected_plan_identity) is None
    ):
        _fail("plan_identity_invalid", "check requires an exact reviewed identity")
    plan = plan_orchestration_initialization(
        root, declaration, project_id=project_id, version=version
    )
    if plan["plan_identity"] != expected_plan_identity:
        _fail(
            "plan_stale",
            "reviewed initialization plan no longer matches current inputs",
        )
    return {
        "schema": "literate-ai/orchestration-initialization-check@1",
        "state": "current",
        "plan_identity": expected_plan_identity,
        "writes": False,
        "execution": False,
        "apply_supported": True,
        "publication": "not-checked",
    }


@dataclass
class _OwnedFile:
    path: Path
    key: tuple[int, int, int]
    content: bytes


class _Additions:
    def __init__(self, root: Path, root_key: tuple[int, int, int]):
        self.root = root
        self.directories = {root: root_key}
        self.files: list[_OwnedFile] = []

    def guard(self, parent: Path) -> None:
        require_safe_directory(parent)
        for path in (parent, *parent.parents):
            if (
                path not in self.directories
                or _key(path.lstat()) != self.directories[path]
            ):
                _fail(
                    "initialization_changed", "owned initialization directory changed"
                )
            if path == self.root:
                return
            _no_alias(path)
        _fail("initialization_changed", "initialization path escaped the root")

    def directory(self, path: Path) -> None:
        if not path.is_relative_to(self.root):
            _fail("initialization_changed", "initialization directory escaped the root")
        if path in self.directories:
            self.guard(path)
            return
        self.directory(path.parent)
        self.guard(path.parent)
        _no_alias(path)
        path.mkdir()
        node = path.lstat()
        if stat_is_link_or_reparse(node) or not stat.S_ISDIR(node.st_mode):
            _fail("initialization_changed", "created directory is indirect")
        self.directories[path] = _key(node)

    def current(self, owned: _OwnedFile) -> bool:
        self.guard(owned.path.parent)
        _no_alias(owned.path)
        return (
            _key(owned.path.lstat()) == owned.key and _read(owned.path) == owned.content
        )

    def stage(self, path: Path, content: bytes) -> _OwnedFile:
        self.directory(path.parent)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        owned = _OwnedFile(path, _key(os.fstat(descriptor)), b"")
        self.files.append(owned)
        try:
            while len(owned.content) < len(content):
                count = os.write(descriptor, content[len(owned.content) :])
                if count <= 0:
                    raise OSError("orchestration stage write made no progress")
                owned.content = content[: len(owned.content) + count]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if not self.current(owned):
            _fail("initialization_changed", "staged initialization bytes changed")
        return owned

    def publish(self, source: _OwnedFile, target: Path) -> None:
        self.directory(target.parent)
        _no_alias(target)
        if not self.current(source):
            _fail("initialization_changed", "staged initialization bytes changed")
        os.link(source.path, target, follow_symlinks=False)
        owned = _OwnedFile(target, source.key, source.content)
        self.files.append(owned)
        if not self.current(owned):
            _fail("initialization_changed", "published initialization bytes changed")

    def cleanup(self, *, only_under: Path | None = None) -> tuple[str, ...]:
        retained = []
        try:
            self.guard(self.root)
        except (
            OSError,
            ValueError,
            UnsafeFilesystemPathError,
            OrchestrationInventoryError,
        ):
            return (".",)
        for owned in reversed(self.files):
            if only_under is not None and not owned.path.is_relative_to(only_under):
                continue
            try:
                if not self.current(owned):
                    retained.append(owned.path.relative_to(self.root).as_posix())
                    continue
                owned.path.unlink()
            except FileNotFoundError:
                pass
            except (
                OSError,
                ValueError,
                UnsafeFilesystemPathError,
                OrchestrationInventoryError,
            ):
                retained.append(owned.path.relative_to(self.root).as_posix())
        for path in reversed(self.directories):
            if path == self.root or (
                only_under is not None and not path.is_relative_to(only_under)
            ):
                continue
            try:
                self.guard(path)
                path.rmdir()
            except FileNotFoundError:
                pass
            except (
                OSError,
                ValueError,
                UnsafeFilesystemPathError,
                OrchestrationInventoryError,
            ):
                retained.append(path.relative_to(self.root).as_posix())
        return tuple(sorted(set(retained)))


def initialize_orchestration(
    root: Path,
    declaration: Path,
    *,
    project_id: str,
    version: str,
    expected_plan_identity: str,
    acknowledged: bool,
) -> dict[str, Any]:
    """Publish reviewed root additions; preserve concurrent user replacements."""
    if acknowledged is not True:
        _fail(
            "acknowledgement_required", "root initialization requires acknowledgement"
        )
    if (
        not isinstance(expected_plan_identity, str)
        or _IDENTITY.fullmatch(expected_plan_identity) is None
    ):
        _fail(
            "plan_identity_invalid",
            "initialization requires an exact reviewed identity",
        )
    root_key = _key(root.absolute().lstat())
    root, scaffold, shims, plan = _prepare(root, declaration, project_id, version)
    if plan["plan_identity"] != expected_plan_identity:
        _fail(
            "plan_stale",
            "reviewed initialization plan no longer matches current inputs",
        )
    owned = _Additions(root, root_key)
    stage = root / ".literate" / ".orchestration-stage"

    def revalidate():
        owned.guard(root)
        current = plan_orchestration(root, declaration)
        if current["plan_identity"] != plan["inspection_plan_identity"]:
            _fail(
                "inputs_changed",
                "Gitlinks or declaration changed during initialization",
            )
        refreshed = prepare_orchestration_scaffold(
            RepositoryOrchestration.from_dict(current["repository_authority"]),
            project_id=project_id,
            version=version,
        )
        if refreshed != scaffold:
            _fail(
                "inputs_changed", "initialization scaffold changed during publication"
            )
        for relative in CANONICAL_AGENT_SHIMS:
            if not project_owns_agent_shim(scaffold.definition, relative):
                continue
            path = root / relative
            if relative not in shims:
                if path.exists() or path.is_symlink():
                    _fail("inputs_changed", "agent shim appeared during initialization")
            elif (_key(path.lstat()), _read(path)) != shims[relative]:
                _fail("inputs_changed", "agent shim changed during initialization")

    try:
        owned.directory(stage)
        staged = {
            path: owned.stage(stage / path, content) for path, content in scaffold.files
        }
        validator = FilesystemProjectValidationAdapter()
        validator.validate(
            stage, require_authority_review=True, synchronize_source_intelligence=False
        )
        revalidate()
        for path in sorted(
            staged, key=lambda path: (path == "literate.project.json", path)
        ):
            if path == "literate.project.json":
                revalidate()
            owned.publish(staged[path], root / path)
        validation = validator.validate(
            root, require_authority_review=True, synchronize_source_intelligence=False
        )
        revalidate()
        if any(not owned.current(item) for item in owned.files):
            _fail(
                "initialization_changed", "initialized files changed before completion"
            )
    except BaseException as exc:
        retained = owned.cleanup()
        if not isinstance(exc, Exception):
            raise
        _fail(
            "rollback_incomplete" if retained else "initialization_failed",
            "root initialization refused; unchanged owned additions were removed"
            + (" and changed additions were preserved" if retained else ""),
        )
    retained = owned.cleanup(only_under=stage)
    return {
        "schema": "literate-ai/orchestration-initialization@1",
        "state": "initialized",
        "plan_identity": expected_plan_identity,
        "project_identity": scaffold.definition.identity.uri,
        "authority_identity": validation["authority_review"]["authority_identity"],
        "created_files": [path for path, _content in scaffold.files],
        "cleanup_retained": list(retained),
        "writes": True,
        "execution": False,
        "child_authority": "independent",
        "publication": "not-checked",
    }
