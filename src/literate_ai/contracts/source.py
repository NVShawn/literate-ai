"""Canonical source-tree and independent origin-attestation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    fields,
    int_value,
    parse_tuple,
    string_value,
    unique,
)
from .identity import (
    SCHEMA_PREFIX,
    ContentIdentity,
    canonical_identity,
    contract_identity,
)

ORIGIN_ATTESTATION_SCHEMA = f"{SCHEMA_PREFIX}origin-attestation"
SOURCE_SNAPSHOT_SCHEMA = f"{SCHEMA_PREFIX}source-snapshot"
SOURCE_TREE_MANIFEST_SCHEMA = f"{SCHEMA_PREFIX}source-tree-manifest"


def _portable_path(value: str, path: str) -> str:
    raw = string_value(value, path)
    if raw.startswith("/") or "\\" in raw:
        fail(path, "must be a relative POSIX path")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail(
            path, "must be normalized and must not contain empty, '.' or '..' segments"
        )
    return raw


class SourceEntryType(StrEnum):
    FILE = "file"
    SYMLINK = "symlink"
    SUBMODULE = "submodule"
    LFS_OBJECT = "lfs-object"
    ARCHIVE_MEMBER = "archive-member"


class VerificationResult(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class RevocationStatus(StrEnum):
    GOOD = "good"
    REVOKED = "revoked"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True, slots=True)
class AttestationFact:
    name: str
    value: str

    def __post_init__(self) -> None:
        string_value(self.name, "AttestationFact.name")
        string_value(self.value, "AttestationFact.value", max_length=16384)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "AttestationFact") -> AttestationFact:
        data = fields(
            value,
            path=path,
            required=frozenset({"name", "value"}),
        )
        return cls(
            name=string_value(data["name"], f"{path}.name"),
            value=string_value(data["value"], f"{path}.value", max_length=16384),
        )


@dataclass(frozen=True, slots=True)
class SourceEntry:
    path: str
    entry_type: SourceEntryType
    mode: int
    size: int
    identity: ContentIdentity

    def __post_init__(self) -> None:
        _portable_path(self.path, "SourceEntry.path")
        if not isinstance(self.entry_type, SourceEntryType):
            fail("SourceEntry.entry_type", "must be a SourceEntryType")
        int_value(self.mode, "SourceEntry.mode", maximum=0o7777)
        int_value(self.size, "SourceEntry.size")
        if not isinstance(self.identity, ContentIdentity):
            fail("SourceEntry.identity", "must be a ContentIdentity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "entry_type": self.entry_type.value,
            "mode": self.mode,
            "size": self.size,
            "identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "SourceEntry") -> SourceEntry:
        data = fields(
            value,
            path=path,
            required=frozenset({"path", "entry_type", "mode", "size", "identity"}),
        )
        return cls(
            path=_portable_path(data["path"], f"{path}.path"),
            entry_type=enum_value(
                SourceEntryType, data["entry_type"], f"{path}.entry_type"
            ),
            mode=int_value(data["mode"], f"{path}.mode", maximum=0o7777),
            size=int_value(data["size"], f"{path}.size"),
            identity=ContentIdentity.from_dict(
                data["identity"], path=f"{path}.identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class AggregateSourceMember:
    member_id: str
    mount_path: str
    snapshot_identity: ContentIdentity

    def __post_init__(self) -> None:
        string_value(self.member_id, "AggregateSourceMember.member_id")
        _portable_path(self.mount_path, "AggregateSourceMember.mount_path")
        if not isinstance(self.snapshot_identity, ContentIdentity):
            fail("AggregateSourceMember.snapshot_identity", "must be a ContentIdentity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "mount_path": self.mount_path,
            "snapshot_identity": self.snapshot_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "AggregateSourceMember"
    ) -> AggregateSourceMember:
        data = fields(
            value,
            path=path,
            required=frozenset({"member_id", "mount_path", "snapshot_identity"}),
        )
        return cls(
            member_id=string_value(data["member_id"], f"{path}.member_id"),
            mount_path=_portable_path(data["mount_path"], f"{path}.mount_path"),
            snapshot_identity=ContentIdentity.from_dict(
                data["snapshot_identity"], path=f"{path}.snapshot_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class OriginAttestation:
    signer: str
    trust_root: str
    verification_method: str
    tree_identity: ContentIdentity
    source_provider: str
    source_facts: tuple[AttestationFact, ...]
    revocation_status: RevocationStatus
    result: VerificationResult

    SCHEMA: ClassVar[str] = ORIGIN_ATTESTATION_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.signer, "OriginAttestation.signer")
        string_value(self.trust_root, "OriginAttestation.trust_root")
        string_value(self.verification_method, "OriginAttestation.verification_method")
        string_value(self.source_provider, "OriginAttestation.source_provider")
        unique(
            tuple(fact.name for fact in self.source_facts),
            "OriginAttestation.source_facts",
            "fact names",
        )
        if not isinstance(self.revocation_status, RevocationStatus):
            fail("OriginAttestation.revocation_status", "must be a RevocationStatus")
        if not isinstance(self.result, VerificationResult):
            fail("OriginAttestation.result", "must be a VerificationResult")
        if (
            self.result is VerificationResult.VERIFIED
            and self.revocation_status
            not in (
                RevocationStatus.GOOD,
                RevocationStatus.NOT_APPLICABLE,
            )
        ):
            fail(
                "OriginAttestation.result",
                "verified result requires good or not-applicable revocation status",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "signer": self.signer,
            "trust_root": self.trust_root,
            "verification_method": self.verification_method,
            "tree_identity": self.tree_identity.to_dict(),
            "source_provider": self.source_provider,
            "source_facts": [fact.to_dict() for fact in self.source_facts],
            "revocation_status": self.revocation_status.value,
            "result": self.result.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "OriginAttestation"
    ) -> OriginAttestation:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "signer",
                    "trust_root",
                    "verification_method",
                    "tree_identity",
                    "source_provider",
                    "source_facts",
                    "revocation_status",
                    "result",
                }
            ),
        )
        return cls(
            signer=string_value(data["signer"], f"{path}.signer"),
            trust_root=string_value(data["trust_root"], f"{path}.trust_root"),
            verification_method=string_value(
                data["verification_method"], f"{path}.verification_method"
            ),
            tree_identity=ContentIdentity.from_dict(
                data["tree_identity"], path=f"{path}.tree_identity"
            ),
            source_provider=string_value(
                data["source_provider"], f"{path}.source_provider"
            ),
            source_facts=parse_tuple(
                data["source_facts"], f"{path}.source_facts", AttestationFact.from_dict
            ),
            revocation_status=enum_value(
                RevocationStatus,
                data["revocation_status"],
                f"{path}.revocation_status",
            ),
            result=enum_value(VerificationResult, data["result"], f"{path}.result"),
        )


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    source_provider: str
    provider_configuration: ContentIdentity
    resolved_revision: str
    entries: tuple[SourceEntry, ...]
    aggregate_members: tuple[AggregateSourceMember, ...]
    tree_identity: ContentIdentity
    dirty: bool
    dirty_policy_decision: ContentIdentity | None
    origin_attestations: tuple[OriginAttestation, ...]

    SCHEMA: ClassVar[str] = SOURCE_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.source_provider, "SourceSnapshot.source_provider")
        string_value(self.resolved_revision, "SourceSnapshot.resolved_revision")
        if not isinstance(self.dirty, bool):
            fail("SourceSnapshot.dirty", "must be a boolean")
        if self.dirty and self.dirty_policy_decision is None:
            fail(
                "SourceSnapshot.dirty_policy_decision",
                "is required for a dirty source snapshot",
            )
        if not self.entries and not self.aggregate_members:
            fail("SourceSnapshot", "must contain entries or aggregate source members")
        unique(
            tuple(entry.path for entry in self.entries),
            "SourceSnapshot.entries",
            "entry paths",
        )
        unique(
            tuple(member.member_id for member in self.aggregate_members),
            "SourceSnapshot.aggregate_members",
            "member IDs",
        )
        expected = self.compute_tree_identity(self.entries, self.aggregate_members)
        if self.tree_identity != expected:
            fail(
                "SourceSnapshot.tree_identity",
                f"does not match canonical tree manifest {expected.uri}",
            )
        for index, attestation in enumerate(self.origin_attestations):
            if attestation.tree_identity != self.tree_identity:
                fail(
                    f"SourceSnapshot.origin_attestations[{index}].tree_identity",
                    "must match the snapshot tree identity",
                )

    @staticmethod
    def compute_tree_identity(
        entries: tuple[SourceEntry, ...],
        aggregate_members: tuple[AggregateSourceMember, ...],
    ) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": SOURCE_TREE_MANIFEST_SCHEMA,
                "entries": [entry.to_dict() for entry in entries],
                "aggregate_members": [member.to_dict() for member in aggregate_members],
            }
        )

    @classmethod
    def create(
        cls,
        *,
        source_provider: str,
        provider_configuration: ContentIdentity,
        resolved_revision: str,
        entries: tuple[SourceEntry, ...] = (),
        aggregate_members: tuple[AggregateSourceMember, ...] = (),
        dirty: bool = False,
        dirty_policy_decision: ContentIdentity | None = None,
        origin_attestations: tuple[OriginAttestation, ...] = (),
    ) -> SourceSnapshot:
        tree_identity = cls.compute_tree_identity(entries, aggregate_members)
        return cls(
            source_provider=source_provider,
            provider_configuration=provider_configuration,
            resolved_revision=resolved_revision,
            entries=entries,
            aggregate_members=aggregate_members,
            tree_identity=tree_identity,
            dirty=dirty,
            dirty_policy_decision=dirty_policy_decision,
            origin_attestations=origin_attestations,
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "source_provider": self.source_provider,
            "provider_configuration": self.provider_configuration.to_dict(),
            "resolved_revision": self.resolved_revision,
            "entries": [entry.to_dict() for entry in self.entries],
            "aggregate_members": [
                member.to_dict() for member in self.aggregate_members
            ],
            "tree_identity": self.tree_identity.to_dict(),
            "dirty": self.dirty,
            "dirty_policy_decision": (
                None
                if self.dirty_policy_decision is None
                else self.dirty_policy_decision.to_dict()
            ),
            "origin_attestations": [
                attestation.to_dict() for attestation in self.origin_attestations
            ],
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "SourceSnapshot") -> SourceSnapshot:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "source_provider",
                    "provider_configuration",
                    "resolved_revision",
                    "entries",
                    "aggregate_members",
                    "tree_identity",
                    "dirty",
                    "dirty_policy_decision",
                    "origin_attestations",
                }
            ),
        )
        policy = data["dirty_policy_decision"]
        return cls(
            source_provider=string_value(
                data["source_provider"], f"{path}.source_provider"
            ),
            provider_configuration=ContentIdentity.from_dict(
                data["provider_configuration"], path=f"{path}.provider_configuration"
            ),
            resolved_revision=string_value(
                data["resolved_revision"], f"{path}.resolved_revision"
            ),
            entries=parse_tuple(
                data["entries"], f"{path}.entries", SourceEntry.from_dict
            ),
            aggregate_members=parse_tuple(
                data["aggregate_members"],
                f"{path}.aggregate_members",
                AggregateSourceMember.from_dict,
            ),
            tree_identity=ContentIdentity.from_dict(
                data["tree_identity"], path=f"{path}.tree_identity"
            ),
            dirty=bool_value(data["dirty"], f"{path}.dirty"),
            dirty_policy_decision=(
                None
                if policy is None
                else ContentIdentity.from_dict(
                    policy, path=f"{path}.dirty_policy_decision"
                )
            ),
            origin_attestations=parse_tuple(
                data["origin_attestations"],
                f"{path}.origin_attestations",
                OriginAttestation.from_dict,
            ),
        )


__all__ = [
    "ORIGIN_ATTESTATION_SCHEMA",
    "SOURCE_SNAPSHOT_SCHEMA",
    "AggregateSourceMember",
    "AttestationFact",
    "OriginAttestation",
    "RevocationStatus",
    "SourceEntry",
    "SourceEntryType",
    "SourceSnapshot",
    "VerificationResult",
]
