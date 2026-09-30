"""Read-only Gitlink observations for explicit repository orchestration.

This internal inventory does not initialize, fetch, run child code, or assert that
any commit is published. Ordinary adoption deliberately does not call it.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.repository_orchestration import gitlink_url

from .builders._process import run_bounded_process
from .builders.python import BuildError

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_MODULE_BYTES = 256 * 1024
_MAX_CHILDREN = 128


class OrchestrationInventoryError(ValueError):
    def __init__(self, suffix: str, message: str) -> None:
        self.code = f"orchestration.{suffix}"
        self.message = message
        super().__init__(message)


def _fail(suffix: str, message: str) -> None:
    raise OrchestrationInventoryError(suffix, message)


@dataclass(frozen=True, slots=True)
class GitlinkObservation:
    name: str
    path: str
    url: str
    commit: str
    branch: str | None
    state: str
    checked_out_commit: str | None


@dataclass(frozen=True, slots=True)
class GitlinkInventory:
    gitmodules_identity: str | None
    children: tuple[GitlinkObservation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/gitlink-inventory@1",
            "gitmodules_identity": self.gitmodules_identity,
            "children": [asdict(child) for child in self.children],
        }

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri


def _git(root: Path, *arguments: str) -> bytes:
    # Ambient Git selectors must not redirect observation to a different index,
    # worktree, object store, or global command/configuration injection.
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
    )
    try:
        result = run_bounded_process(
            ("git", "--no-pager", "-c", "core.fsmonitor=false", *arguments),
            cwd=root,
            environment=environment,
            # Repository-refresh suites can keep hosted Windows runners under
            # sustained filesystem pressure for hours. Preserve the bounded
            # observation while allowing a delayed read-only Git process to
            # complete instead of turning runner contention into data drift.
            timeout_seconds=60,
            stdout_limit_bytes=8 * 1024 * 1024,
            stderr_limit_bytes=64 * 1024,
            error_prefix="orchestration.git",
        )
    except BuildError as exc:
        _fail("inspection_failed", f"bounded Git observation failed ({exc.code})")
    if result.returncode:
        # Git diagnostics may contain configured credentials; never echo them.
        _fail("inspection_failed", "Git could not inspect the requested repository")
    return result.stdout


def _oid(raw: bytes) -> str:
    value = raw.decode("ascii")
    if _OID.fullmatch(value) is None or not value.strip("0"):
        _fail("index_invalid", "an exact nonzero Git object ID is required")
    return value


def _index(root: Path) -> dict[str, tuple[str, str]]:
    _require_nonwriting_index(root)
    raw = _git(root, "ls-files", "--stage", "-z")
    if raw and not raw.endswith(b"\0"):
        _fail("index_invalid", "Git index output is incomplete")
    entries: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\0")[:-1]:
        metadata, separator, path_bytes = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3 or fields[2] != b"0":
            _fail("index_invalid", "orchestration refuses ambiguous index stages")
        path = path_bytes.decode("utf-8")
        if path in entries:
            _fail("index_invalid", "orchestration refuses duplicate index paths")
        entries[path] = (fields[0].decode("ascii"), _oid(fields[1]))
    return entries


def _read_document(
    path: Path,
    *,
    label: str = ".gitmodules",
    error_suffix: str = "modules_invalid",
    maximum_bytes: int = _MAX_MODULE_BYTES,
) -> bytes:
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise ValueError("document byte limit must be a positive integer")
    require_safe_directory(path.parent)
    before = path.lstat()
    if (
        stat_is_link_or_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > maximum_bytes
    ):
        _fail(
            error_suffix,
            f"{label} must be a direct file of at most {maximum_bytes} bytes",
        )
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(descriptor)

        def snapshot(node):
            return tuple(
                getattr(node, field)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            )

        named_snapshot, opened_snapshot = snapshot(before), snapshot(opened)
        # Windows may give pathname and descriptor ctime different meanings.
        # Cross-bind common fields; keep each complete clock for its own drift check.
        if named_snapshot[:-1] != opened_snapshot[:-1]:
            _fail("inputs_changed", f"{label} changed during inspection")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(maximum_bytes + 1)
        after = path.lstat()
        require_safe_directory(path.parent)
        if (
            snapshot(after) != named_snapshot
            or snapshot(os.fstat(descriptor)) != opened_snapshot
        ):
            _fail("inputs_changed", f"{label} changed during inspection")
        if len(raw) != before.st_size:
            _fail("inputs_changed", f"{label} changed during inspection")
        return raw
    finally:
        os.close(descriptor)


def _require_nonwriting_index(root: Path) -> None:
    """Refuse split indexes before Git can freshen their shared-index mtime.

    Only locate extension boundaries here; Git still validates admitted indexes.
    Do not use `rev-parse --shared-index-path`: it reads the index.
    """
    # Both metadata values come from one fresh, non-index-reading invocation.
    # Keep repeated inventory observations independent; never cache this result.
    metadata = _git(
        root,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "index",
        "--show-object-format",
    ).split(b"\n")
    if len(metadata) != 3 or metadata[-1] != b"":
        _fail("index_invalid", "Git index metadata is absent or ambiguous")
    raw_path, algorithm, _ = metadata
    path_text = raw_path.decode("utf-8")
    if not path_text or "\n" in path_text or "\r" in path_text:
        _fail("index_invalid", "Git index location is absent or ambiguous")
    path = Path(os.path.abspath(root / path_text))
    try:
        raw = _read_document(
            path,
            label="Git index",
            error_suffix="index_invalid",
            maximum_bytes=16 * 1024 * 1024,
        )
    except FileNotFoundError:
        return  # An unborn repository can have no index.
    oid_size = {b"sha1": 20, b"sha256": 32}.get(algorithm)
    if oid_size is None or len(raw) < 12 + oid_size or raw[:4] != b"DIRC":
        _fail("index_invalid", "Git index header is unavailable or unsupported")
    version = int.from_bytes(raw[4:8], "big")
    count = int.from_bytes(raw[8:12], "big")
    end = len(raw) - oid_size
    if version not in {2, 3, 4} or count > (end - 12) // (42 + oid_size):
        _fail("index_invalid", "Git index version or entry count is invalid")
    cursor, previous_length = 12, 0
    for _ in range(count):
        start = cursor
        cursor += 40 + oid_size
        if cursor + 2 > end:
            _fail("index_invalid", "Git index entry is truncated")
        flags = int.from_bytes(raw[cursor : cursor + 2], "big")
        cursor += 2
        if flags & 0x4000:
            if version == 2:
                _fail("index_invalid", "Git index flags require version 3 or later")
            cursor += 2
        removed = 0
        if version == 4:
            for position in range(10):
                if cursor >= end:
                    _fail("index_invalid", "Git index compressed path is truncated")
                value = raw[cursor]
                cursor += 1
                removed = ((removed + 1) << 7 if position else 0) + (value & 0x7F)
                if not value & 0x80:
                    break
            else:
                _fail("index_invalid", "Git index compressed path exceeds its bound")
        nul = raw.find(b"\0", cursor, end)
        if nul < 0:
            _fail("index_invalid", "Git index path is unterminated")
        length = nul - cursor
        if version == 4:
            if removed > previous_length:
                _fail("index_invalid", "Git index path prefix is invalid")
            length += previous_length - removed
        if flags & 0xFFF != min(length, 0xFFF):
            _fail("index_invalid", "Git index path length is inconsistent")
        previous_length = length
        cursor = nul + 1
        if version != 4:
            cursor = start + ((cursor - start + 7) // 8) * 8
            if any(raw[nul:cursor]):
                _fail("index_invalid", "Git index padding is not canonical")
        if cursor > end:
            _fail("index_invalid", "Git index entry exceeds its byte bound")
    while cursor < end:
        if cursor + 8 > end:
            _fail("index_invalid", "Git index extension is truncated")
        signature = raw[cursor : cursor + 4]
        size = int.from_bytes(raw[cursor + 4 : cursor + 8], "big")
        cursor += 8 + size
        if cursor > end:
            _fail("index_invalid", "Git index extension exceeds its byte bound")
        if signature == b"link":
            _fail(
                "split_index_unsupported",
                "nonwriting inspection cannot use Git's shared-index timestamp refresh",
            )


def _url(value: str) -> str:
    try:
        return gitlink_url(value)
    except (TypeError, ValueError):
        _fail(
            "modules_invalid",
            "submodule URL is absent, ambiguous or credential-bearing",
        )


def _configuration(root: Path, oid: str) -> dict[str, dict[str, str]]:
    raw = _git(root, "config", "--no-includes", "--blob", oid, "--null", "--list")
    groups: dict[str, dict[str, str]] = {}
    if raw and not raw.endswith(b"\0"):
        _fail("modules_invalid", "Git configuration output is incomplete")
    seen: set[str] = set()
    for record in raw.split(b"\0")[:-1]:
        key_bytes, separator, value_bytes = record.partition(b"\n")
        key = key_bytes.decode("utf-8")
        if key in seen:
            _fail("modules_invalid", "duplicate .gitmodules configuration key")
        seen.add(key)
        if key.startswith(("include.", "includeif.")):
            _fail(
                "modules_invalid",
                ".gitmodules includes are not orchestration authority",
            )
        if not key.startswith("submodule."):
            continue
        name, dot, field = key.removeprefix("submodule.").rpartition(".")
        if not dot or not name or any(ord(char) < 32 for char in name):
            _fail("modules_invalid", "submodule configuration requires a named section")
        group = groups.setdefault(name, {})
        if field in {"path", "url", "branch"}:
            if not separator or not value_bytes:
                _fail(
                    "modules_invalid", "submodule path, URL and branch require values"
                )
            group[field] = value_bytes.decode("utf-8")
    if len(groups) > _MAX_CHILDREN:
        _fail(
            "modules_invalid", "orchestration inventory exceeds 128 child repositories"
        )
    for group in groups.values():
        if not {"path", "url"} <= group.keys():
            _fail("modules_invalid", "each submodule requires a path and URL")
        _url(group["url"])
        branch = group.get("branch")
        if branch is not None and branch != ".":
            if branch.startswith("-"):
                _fail("modules_invalid", "submodule branch is not a valid branch name")
            _git(root, "check-ref-format", "refs/heads/" + branch)
    canonical_relative_posix_paths(
        (group["path"] for group in groups.values()), label="Gitlink paths"
    )
    return groups


def _worktree(root: Path) -> None:
    require_safe_directory(root)
    boundary = (root / ".git").lstat()
    if stat_is_link_or_reparse(boundary) or not (
        stat.S_ISDIR(boundary.st_mode) or stat.S_ISREG(boundary.st_mode)
    ):
        _fail("repository_invalid", "repository requires its own direct Git boundary")
    actual = _git(root, "rev-parse", "--show-toplevel").decode("utf-8").rstrip("\n")
    if Path(actual).resolve() != root.resolve():
        _fail("repository_invalid", "Git resolved a different repository root")


def _child_head(root: Path, relative: str) -> str | None:
    child = root
    for part in relative.split("/"):
        child = child / part
        try:
            child.lstat()
        except FileNotFoundError:
            return None
        require_safe_directory(child)
    try:
        (child / ".git").lstat()
    except FileNotFoundError:
        return None
    _worktree(child)
    return _oid(_git(child, "rev-parse", "--verify", "HEAD^{commit}").strip())


def _observe(root: Path) -> GitlinkInventory:
    _worktree(root)
    entries = _index(root)
    links = {path: oid for path, (mode, oid) in entries.items() if mode == "160000"}
    if len(links) > _MAX_CHILDREN:
        _fail("index_invalid", "orchestration inventory exceeds 128 child repositories")
    modules = entries.get(".gitmodules")
    if modules is None:
        if links or os.path.lexists(root / ".gitmodules"):
            _fail("modules_invalid", "Gitlinks require indexed .gitmodules authority")
        return GitlinkInventory(None, ())
    mode, oid = modules
    if mode not in {"100644", "100755"}:
        _fail("modules_invalid", "indexed .gitmodules must be a regular file")
    raw = _read_document(root / ".gitmodules")
    blob = b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
    algorithm = "sha1" if len(oid) == 40 else "sha256"
    if hashlib.new(algorithm, blob).hexdigest() != oid:
        _fail("modules_drift", "working .gitmodules differs from the indexed authority")
    configuration = _configuration(root, oid)
    if {group["path"] for group in configuration.values()} != set(links):
        _fail("modules_invalid", ".gitmodules paths and indexed Gitlinks disagree")
    children = []
    for name, group in sorted(configuration.items(), key=lambda item: item[1]["path"]):
        head = _child_head(root, group["path"])
        children.append(
            GitlinkObservation(
                name,
                group["path"],
                group["url"],
                links[group["path"]],
                group.get("branch"),
                "uninitialized" if head is None else "initialized",
                head,
            )
        )
    return GitlinkInventory(
        "sha256:" + hashlib.sha256(raw).hexdigest(), tuple(children)
    )


def inspect_gitlink_inventory(root: Path) -> GitlinkInventory:
    """Observe exact direct-child pins twice; no receipt or publication claim.

    Like other local-development observations, rechecking detects drift but is not
    an atomic snapshot or hostile same-user filesystem containment guarantee.
    """
    try:
        first = _observe(root.absolute())
        if _observe(root.absolute()) != first:
            _fail("inputs_changed", "Gitlink inventory changed during inspection")
        return first
    except OrchestrationInventoryError:
        raise
    except (OSError, UnicodeError, ValueError, UnsafeFilesystemPathError) as exc:
        raise OrchestrationInventoryError(
            "inspection_failed", "Gitlink inventory is unavailable or noncanonical"
        ) from exc
