"""Operational source-intake facts bound to canonical source contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from literate_ai.contracts import SourceSnapshot, VerificationResult


def _nonempty(value: str, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def _relative_path(value: str, field_name: str) -> str:
    value = _nonempty(value, field_name)
    if value.startswith("/") or "\\" in value:
        raise ValueError(f"{field_name} must be a relative POSIX path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"{field_name} must be normalized")
    return value


@dataclass(frozen=True, slots=True)
class GitChange:
    """One machine-readable Git porcelain change."""

    status: str
    path: str
    original_path: str | None = None

    def __post_init__(self) -> None:
        if len(self.status) != 2 or self.status == "  ":
            raise ValueError("Git change status must contain two porcelain columns")
        _relative_path(self.path, "GitChange.path")
        if self.original_path is not None:
            _relative_path(self.original_path, "GitChange.original_path")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "original_path": self.original_path,
        }


@dataclass(frozen=True, slots=True)
class GitSubmoduleFact:
    """The checked-out state of one recursive Git submodule."""

    path: str
    commit: str
    state: str

    def __post_init__(self) -> None:
        _relative_path(self.path, "GitSubmoduleFact.path")
        _nonempty(self.commit, "GitSubmoduleFact.commit")
        if self.state not in {"clean", "uninitialized", "different", "conflict"}:
            raise ValueError("unsupported Git submodule state")

    @property
    def initialized(self) -> bool:
        return self.state != "uninitialized"

    @property
    def clean(self) -> bool:
        return self.state == "clean"

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "path": self.path,
            "commit": self.commit,
            "state": self.state,
            "initialized": self.initialized,
            "clean": self.clean,
        }


@dataclass(frozen=True, slots=True)
class GitFacts:
    """Git facts captured without treating a checkout as an opaque SDK."""

    head_commit: str
    head_ref: str | None
    changes: tuple[GitChange, ...] = ()
    submodules: tuple[GitSubmoduleFact, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.head_commit, "GitFacts.head_commit")
        if self.head_ref is not None:
            _nonempty(self.head_ref, "GitFacts.head_ref")
        if len({item.path for item in self.changes}) != len(self.changes):
            raise ValueError("Git change paths must be unique")
        if len({item.path for item in self.submodules}) != len(self.submodules):
            raise ValueError("Git submodule paths must be unique")

    @property
    def dirty(self) -> bool:
        return bool(self.changes) or any(not item.clean for item in self.submodules)

    def to_dict(self) -> dict[str, Any]:
        return {
            "head_commit": self.head_commit,
            "head_ref": self.head_ref,
            "dirty": self.dirty,
            "changes": [item.to_dict() for item in self.changes],
            "submodules": [item.to_dict() for item in self.submodules],
        }


@dataclass(frozen=True, slots=True)
class LfsPointerFact:
    """An unresolved Git LFS pointer found in the snapshotted bytes."""

    path: str
    oid_sha256: str
    object_size: int

    def __post_init__(self) -> None:
        _relative_path(self.path, "LfsPointerFact.path")
        if len(self.oid_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.oid_sha256
        ):
            raise ValueError("Git LFS object ID must be a lower-case SHA-256 digest")
        if self.object_size < 0:
            raise ValueError("Git LFS object size must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "oid_sha256": self.oid_sha256,
            "object_size": self.object_size,
            "hydrated": False,
        }


@dataclass(frozen=True, slots=True)
class SourceCapture:
    """A canonical snapshot plus acquisition facts not embedded in its tree."""

    snapshot: SourceSnapshot
    lfs_pointers: tuple[LfsPointerFact, ...] = ()
    git: GitFacts | None = None

    def __post_init__(self) -> None:
        if len({item.path for item in self.lfs_pointers}) != len(self.lfs_pointers):
            raise ValueError("Git LFS pointer paths must be unique")
        if self.git is None and self.snapshot.source_provider == "git":
            raise ValueError("Git source captures require Git facts")
        if self.git is not None:
            if self.snapshot.source_provider != "git":
                raise ValueError("Git facts may only accompany a Git snapshot")
            if self.snapshot.resolved_revision != self.git.head_commit:
                raise ValueError("Git facts must match the resolved snapshot revision")
            if self.snapshot.dirty != self.git.dirty:
                raise ValueError("Git dirty facts must match the snapshot")

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.identity.uri

    @property
    def tree_id(self) -> str:
        return self.snapshot.tree_identity.uri

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "urn:literate-ai:schema:v1:source-capture",
            "snapshot": self.snapshot.to_dict(),
            "snapshot_id": self.snapshot_id,
            "tree_id": self.tree_id,
            "lfs_pointers": [item.to_dict() for item in self.lfs_pointers],
            "git": None if self.git is None else self.git.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class GitSignatureVerification:
    """A signed-commit result bound to the exact captured source bytes."""

    source_snapshot_id: str
    source_tree_id: str
    commit: str
    result: VerificationResult
    signer: str
    key_fingerprint: str
    trust_code: str
    verifier: str
    detail_digest: str
    reason: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "source_snapshot_id",
            "source_tree_id",
            "commit",
            "verifier",
            "detail_digest",
        ):
            _nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.result, VerificationResult):
            raise ValueError("result must be a VerificationResult")
        if self.result is VerificationResult.VERIFIED:
            if self.trust_code != "G":
                raise ValueError("verified Git signatures require Git trust code 'G'")
            _nonempty(self.signer, "GitSignatureVerification.signer")
            _nonempty(self.key_fingerprint, "GitSignatureVerification.key_fingerprint")

    @property
    def verified(self) -> bool:
        return self.result is VerificationResult.VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "urn:literate-ai:schema:v1:git-signature-verification",
            "source_snapshot_id": self.source_snapshot_id,
            "source_tree_id": self.source_tree_id,
            "commit": self.commit,
            "result": self.result.value,
            "signer": self.signer,
            "key_fingerprint": self.key_fingerprint,
            "trust_code": self.trust_code,
            "verifier": self.verifier,
            "detail_digest": self.detail_digest,
            "reason": self.reason,
        }


__all__ = [
    "GitChange",
    "GitFacts",
    "GitSignatureVerification",
    "GitSubmoduleFact",
    "LfsPointerFact",
    "SourceCapture",
]
