"""Read-only source/Git custody inputs; incomplete inputs authorize no retirement.

These observations do not freeze same-user filesystem changes. A live owner must
reobserve them before retention, and the remaining audit gates are never inferred
from raw source hashes or a clean-looking Git index.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes

from .project_worktree_discovery import _Observation, _registered_entry
from .project_worktree_operation_inputs import capture_operation_inputs
from .project_worktrees import (
    ProjectWorktreeError,
    resolve_project_worktree_location,
)
from .repository_orchestration import (
    _read_document,
    _require_nonwriting_index,
)
from .repository_refresh import _local_identity_wire
from .repository_refresh_worktrees import parse_refresh_worktrees

_CREATION_KEY = object()
AUDIT_INPUT_SCHEMA = "literate-ai/project-worktree-audit-inputs@1"


@dataclass(frozen=True, slots=True)
class WorktreeAuditLimits:
    maximum_entries: int = 10_000
    maximum_file_bytes: int = 64 * 1024 * 1024
    maximum_total_bytes: int = 256 * 1024 * 1024
    maximum_metadata_bytes: int = 4 * 1024 * 1024
    timeout_seconds: int = 120

    def __post_init__(self):
        ceilings = {
            "maximum_entries": 1_000_000,
            "maximum_file_bytes": 64 * 1024**3,
            "maximum_total_bytes": 1024**4,
            "maximum_metadata_bytes": 64 * 1024 * 1024,
            "timeout_seconds": 1800,
        }
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ProjectWorktreeError(f"worktree audit {name} exceeds its bound")
        if self.maximum_file_bytes > self.maximum_total_bytes:
            raise ProjectWorktreeError("worktree audit file limit exceeds total limit")

    def to_dict(self):
        return {"schema": "literate-ai/worktree-audit-limits@1", **asdict(self)}


class _Budget:
    def __init__(self, limits):
        self.limits = limits
        self.entries = self.bytes = self.metadata_bytes = 0

    def metadata(self, value):
        size = len(canonical_json_bytes(_local_identity_wire(value)))
        self.metadata_bytes += size
        if self.metadata_bytes > self.limits.maximum_metadata_bytes:
            raise ProjectWorktreeError("worktree audit metadata byte limit exceeded")

    def entry(self, value):
        self.entries += 1
        if self.entries > self.limits.maximum_entries:
            raise ProjectWorktreeError("worktree audit entry limit exceeded")
        self.metadata(value)

    def payload(self, size):
        self.bytes += size
        if self.bytes > self.limits.maximum_total_bytes:
            raise ProjectWorktreeError("worktree audit source byte limit exceeded")


def _signature(path):
    value = path.lstat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _source_snapshot(root, observation, budget):
    # Import the existing streaming custody owner only for explicit audit work.
    from .retained_native_file_custody import native_file_digest

    records, pending, directories = [], [], []

    def names_at(directory, target_budget):
        names = []
        with os.scandir(directory) as children:
            for child in children:
                target_budget.metadata(
                    {"directory": str(directory.relative_to(root)), "name": child.name}
                )
                if len(names) >= target_budget.limits.maximum_entries:
                    raise ProjectWorktreeError(
                        "worktree audit directory entry limit exceeded"
                    )
                names.append(child.name)
        return tuple(sorted(names))

    stack = [root]
    while stack:
        observation.remaining()
        directory = stack.pop()
        require_safe_directory(directory)
        signature = _signature(directory)
        names = names_at(directory, budget)
        directories.append((directory, signature, names))
        for name in names:
            observation.remaining()
            path = directory / name
            before = _signature(path)
            mode = before[2]
            relative = path.relative_to(root).as_posix()
            # The canonical shared object store is Git metadata, not source bytes.
            if path == root / ".git" and stat.S_ISDIR(mode):
                continue
            record = {"path": relative, "signature": before}
            if stat.S_ISLNK(mode):
                target = os.fsencode(os.readlink(path))
                budget.payload(len(target))
                record.update(
                    kind="symlink",
                    target_base64=base64.b64encode(target).decode("ascii"),
                )
            elif stat_is_link_or_reparse(path.lstat()):
                record["kind"] = "unsupported-reparse"
                pending.append({"code": "reparse-audit-required", "path": relative})
            elif stat.S_ISDIR(mode):
                record["kind"] = "directory"
                if name == ".git":
                    pending.append(
                        {"code": "nested-repository-audit-required", "path": relative}
                    )
                else:
                    stack.append(path)
            elif stat.S_ISREG(mode):
                if before[3] > budget.limits.maximum_file_bytes:
                    raise ProjectWorktreeError(
                        "worktree audit per-file byte limit exceeded"
                    )
                budget.payload(before[3])
                digest, size = native_file_digest(
                    path, budget.limits.maximum_file_bytes
                )
                if size != before[3]:
                    raise ProjectWorktreeError("worktree audit source size changed")
                record.update(
                    kind="file", bytes=size, content_identity="sha256:" + digest
                )
            else:
                record["kind"] = "unsupported-special-file"
                pending.append(
                    {"code": "special-file-audit-required", "path": relative}
                )
            if _signature(path) != before:
                raise ProjectWorktreeError(
                    "worktree audit source changed during capture"
                )
            budget.entry(record)
            records.append(record)
    verification_budget = _Budget(budget.limits)
    for directory, signature, names in directories:
        require_safe_directory(directory)
        if (
            _signature(directory) != signature
            or names_at(directory, verification_budget) != names
        ):
            raise ProjectWorktreeError("worktree audit directory membership changed")
    return sorted(records, key=lambda item: item["path"]), pending


def _git_snapshot(root, observation, budget):
    custody = observation.common(root)
    _require_nonwriting_index(root, _run_git=observation.git)
    index = custody.administration_directory / "index"
    observation.remember(index)
    raw_index = _read_document(
        index,
        label="worktree audit index",
        maximum_bytes=min(16 * 1024 * 1024, budget.limits.maximum_metadata_bytes),
    )
    index_fact = {
        "content_identity": "sha256:" + hashlib.sha256(raw_index).hexdigest(),
        "bytes": len(raw_index),
    }
    budget.metadata(index_fact)
    commands = (
        ("head", ("rev-parse", "--verify", "HEAD^{commit}")),
        ("object_format", ("rev-parse", "--show-object-format")),
        ("index_entries", ("ls-files", "--stage", "-z")),
        ("index_flags", ("ls-files", "-v", "-z")),
        (
            "staged_changes",
            (
                "diff-index",
                "--cached",
                "--raw",
                "-z",
                "--no-ext-diff",
                "--no-textconv",
                "--ignore-submodules=all",
                "HEAD",
                "--",
            ),
        ),
        ("references", ("for-each-ref", "--format=%(refname) %(objectname)")),
        ("reflog_roots", ("reflog", "show", "--all", "--format=%H")),
        (
            # fsck traverses peers' split indexes and refreshes their mtimes.
            # Enumerate all objects without an index walk; reachability and actual
            # object retention remain pending, never inferred from this inventory.
            "object_inventory",
            (
                "cat-file",
                "--batch-all-objects",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            ),
        ),
    )
    operations, operation_inputs = capture_operation_inputs(
        custody, observation, budget
    )
    facts = {
        "index": index_fact,
        "observed_operation_markers": operations,
        "operation_inputs": operation_inputs,
    }
    for name, arguments in commands:
        content = observation.git(root, *arguments)
        fact = {
            "content_identity": "sha256:" + hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "wire_base64": base64.b64encode(content).decode("ascii"),
        }
        budget.metadata(fact)
        facts[name] = fact
    return facts


class PreparedWorktreeAuditInputs:
    """A live capture owner, not constructible from stored JSON evidence."""

    __slots__ = (
        "_repository",
        "_checkout",
        "_limits",
        "_document",
        "_canonical",
        "_protected_roots",
    )

    def __init__(self, key, repository, checkout, limits, document, protected_roots=()):
        if key is not _CREATION_KEY:
            raise TypeError("worktree audit inputs must be captured by a live owner")
        self._repository, self._checkout, self._limits = repository, checkout, limits
        self._protected_roots = tuple(protected_roots)
        self._document = canonical_json_bytes(_local_identity_wire(document))
        self._canonical = canonical_identity(_local_identity_wire(document))

    @property
    def repository(self):
        return self._repository

    @property
    def checkout(self):
        return self._checkout

    @property
    def limits(self):
        return self._limits

    @property
    def identity(self):
        return self._canonical

    @property
    def canonical_bytes(self):
        return self._document

    @property
    def protected_source_roots(self):
        return self._protected_roots

    def require_current(self):
        current = capture_worktree_audit_inputs(
            self.repository, self.checkout, limits=self.limits
        )
        if current.canonical_bytes != self.canonical_bytes:
            raise ProjectWorktreeError("worktree audit inputs changed since capture")
        return current


def capture_worktree_audit_inputs(repository: Path, checkout: Path, *, limits=None):
    """Capture source/Git facts twice without filters, status hooks or mutation.

    This deliberately does not infer process/supervisor idleness, normalized working
    equivalence, nested repository state or retention authorization from these facts.
    """
    limits = WorktreeAuditLimits() if limits is None else limits
    if not isinstance(limits, WorktreeAuditLimits):
        raise ProjectWorktreeError("worktree audit requires typed limits")
    try:
        repository, checkout = (
            Path(repository).resolve(strict=True),
            Path(checkout).resolve(strict=True),
        )
        observation = _Observation()
        observation.deadline = time.monotonic() + limits.timeout_seconds
        location = resolve_project_worktree_location(
            repository, _git_command=observation.git
        )
        records = parse_refresh_worktrees(
            observation.git(repository, "worktree", "list", "--porcelain", "-z")
        )
        record = next((item for item in records if item.path == checkout), None)
        if record is None:
            raise ProjectWorktreeError("worktree audit requires registered ownership")
        selected = _registered_entry(record, location, observation)
        if selected.classification not in {
            "canonical-checkout",
            "project-worktree",
            "legacy-peer-worktree",
            "registered-external-worktree",
        }:
            raise ProjectWorktreeError(
                "worktree audit requires verified registered ownership"
            )
        first_budget = _Budget(limits)
        first_git = _git_snapshot(checkout, observation, first_budget)
        first_source, pending = _source_snapshot(checkout, observation, first_budget)
        observation.revalidate()
        second_budget = _Budget(limits)
        second_git = _git_snapshot(checkout, observation, second_budget)
        second_source, second_pending = _source_snapshot(
            checkout, observation, second_budget
        )
        observation.revalidate()
        if (first_git, first_source, pending) != (
            second_git,
            second_source,
            second_pending,
        ):
            raise ProjectWorktreeError(
                "worktree audit inputs changed during observation"
            )
        after = resolve_project_worktree_location(
            repository, _git_command=observation.git
        )
        if (
            location != after
            or _registered_entry(record, after, observation) != selected
        ):
            raise ProjectWorktreeError("worktree audit registered custody changed")
        final_records = parse_refresh_worktrees(
            observation.git(repository, "worktree", "list", "--porcelain", "-z")
        )
        if (
            next((item for item in final_records if item.path == checkout), None)
            != record
        ):
            raise ProjectWorktreeError("worktree audit registered custody changed")
        observation.revalidate()
        observation.remaining()
        document = {
            "schema": AUDIT_INPUT_SCHEMA,
            "registered_ownership_identity": canonical_identity(
                {
                    "schema": "literate-ai/worktree-audit-ownership@1",
                    "location": location.to_dict(),
                    "registration": selected.to_dict(),
                }
            ).to_dict(),
            "common_directory_observation_identity": canonical_identity(
                _local_identity_wire(
                    {
                        "schema": "literate-ai/worktree-audit-common-directory@1",
                        "path": str(location.common_directory),
                        "node": observation.nodes[location.common_directory],
                    }
                )
            ).to_dict(),
            "filesystem_observation_identity": observation.identity.to_dict(),
            "repository": str(location.common_directory),
            "checkout": str(checkout),
            "limits": limits.to_dict(),
            "registered_state": selected.to_dict(),
            "git_observations": first_git,
            "source": first_source,
            "source_payload_bytes": first_budget.bytes,
            "pending_checks": [
                "working-tree-normalization",
                "nested-repository-state",
                "git-operation-state",
                "git-object-retention",
                "link-topology-ownership",
                "process-activity",
                "supervisor-mounts",
            ],
            "pending_findings": pending,
            "audit_complete": False,
            "retirement_authorized": False,
        }
        if (
            len(canonical_json_bytes(_local_identity_wire(document)))
            > limits.maximum_metadata_bytes
        ):
            raise ProjectWorktreeError(
                "worktree audit report exceeds metadata byte bound"
            )
        return PreparedWorktreeAuditInputs(
            _CREATION_KEY,
            repository,
            checkout,
            limits,
            document,
            (item.path for item in final_records),
        )
    except ProjectWorktreeError:
        raise
    except (
        OSError,
        ValueError,
        UnsafeFilesystemPathError,
    ) as exc:
        raise ProjectWorktreeError(
            "worktree audit input capture could not establish custody"
        ) from exc


__all__ = [
    "PreparedWorktreeAuditInputs",
    "WorktreeAuditLimits",
    "capture_worktree_audit_inputs",
]
