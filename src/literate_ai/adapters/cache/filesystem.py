"""Safe immutable filesystem cache for fully accepted generated source."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from literate_ai.adapters.dependencies import (
    CycloneDxBomError,
    validate_cyclonedx_bom,
    validate_resolved_cyclonedx_bom,
)
from literate_ai.contracts import (
    AcceptedSourceCacheEntry,
    BlobRef,
    ContentIdentity,
    CycloneDxLifecycle,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceCacheTarget,
    SourceDerivationCacheKey,
    SourceIntelligenceArtifact,
    SourceIntelligenceAttachment,
    canonical_identity,
    canonical_json_bytes,
    generated_source_snapshot_identity,
    generated_source_tree_identity,
)
from literate_ai.contracts.generation_cache import (
    STANDARD_ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA,
    StandardAcceptedSourceCacheEntry,
)
from literate_ai.contracts.standard_source_admission import (
    STANDARD_SOURCE_ADMISSION_CACHE_ENTRY_SCHEMA,
    StandardSourceAdmissionCacheEntry,
)
from literate_ai.projects import PROJECT_FILENAME, LoadedProject
from literate_ai.storage import (
    BlobIntegrityError,
    BlobNotFoundError,
    FileSystemCAS,
    StorageError,
    StorageSafetyError,
)

_LAYOUT_SCHEMA = "literate-ai/source-cache-layout@2"
_LEGACY_LAYOUT_SCHEMA = "literate-ai/source-cache-layout@1"
_MEMBERSHIP_SCHEMA = "literate-ai/source-cache-membership@2"
_LEGACY_MEMBERSHIP_SCHEMA = "literate-ai/source-cache-membership@1"
_INTELLIGENCE_MEMBERSHIP_SCHEMA = (
    "literate-ai/source-intelligence-attachment-membership@1"
)
_ENTRY_MEDIA_TYPE = "application/vnd.literate-ai.accepted-source-cache-entry+json"
_EVIDENCE_SIZE_LIMIT = 8 * 1024 * 1024
_MANIFEST_SIZE_LIMIT = 16 * 1024 * 1024
_MAX_CACHE_CANDIDATES = 256
_MAX_SOURCE_INTELLIGENCE_ATTACHMENTS = 64
_MAX_SOURCE_FILES = 4096
_MAX_SOURCE_FILE_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_INTELLIGENCE_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_JSON_DEPTH = 128
_MAX_JSON_NODES = 200_000
_SHA256_HEX = frozenset("0123456789abcdef")
_PORTABLE_TARGET_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_URN_SCHEMA = re.compile(r"^urn:literate-ai:schema:v[1-9][0-9]*:[a-z0-9][a-z0-9._-]*$")


class SourceCacheError(RuntimeError):
    """Stable fail-closed cache error without untrusted object contents."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FinalPathSourceIntelligenceVerifier(Protocol):
    """Build current source-intelligence evidence at the final source location."""

    def finalize(
        self,
        source_root: Path,
        files: Mapping[str, bytes],
    ) -> SourceIntelligenceArtifact: ...

    def verify(
        self,
        source_root: Path,
        files: Mapping[str, bytes],
        evidence: SourceIntelligenceArtifact | Mapping[str, object],
    ) -> SourceIntelligenceArtifact: ...


@dataclass(frozen=True, slots=True)
class SourceCacheCandidate:
    """One structurally verified but acceptance-untrusted imported entry."""

    entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry
    target_ids: tuple[str, ...]
    store: FileSystemSourceCache = field(repr=False, compare=False)
    current_acceptance_trusted: bool = field(default=False, init=False)

    @property
    def identity(self) -> ContentIdentity:
        return self.entry.identity


@dataclass(frozen=True, slots=True)
class MaterializedCachedSource:
    entry_identity: ContentIdentity
    source_root: Path
    intelligence: SourceIntelligenceArtifact | None
    current_acceptance_trusted: bool = field(default=False, init=False)


class FileSystemSourceCache:
    """One filesystem-v2 target with detached source-intelligence attachments."""

    def __init__(
        self,
        target_id: str,
        root: Path,
        *,
        protected_paths: Iterable[Path] = (),
        writable: bool = True,
    ) -> None:
        if (
            not isinstance(target_id, str)
            or _PORTABLE_TARGET_ID.fullmatch(target_id) is None
        ):
            raise ValueError("cache target ID must be a portable identifier")
        if not isinstance(writable, bool):
            raise TypeError("cache target writable mode must be boolean")
        self.target_id = target_id
        self.writable = writable
        candidate = _candidate_root(root, protected_paths=protected_paths)
        self.available = candidate.exists()
        if not self.available and not writable:
            self.root = candidate
            self.entries = self._io_root / "entries" / "sha256"
            self.keys = self._io_root / "keys" / "sha256"
            self.intelligence = self._io_root / "intelligence" / "sha256"
            self.intelligence_by_source = (
                self._io_root / "intelligence-by-source" / "sha256"
            )
            self.staging = self._io_root / "staging"
            self.cas: FileSystemCAS | None = None
            return
        self.root = (
            _prepare_managed_root(candidate, protected_paths=protected_paths)
            if writable
            else _open_managed_root(candidate, protected_paths=protected_paths)
        )
        self.available = True
        self.entries = self._io_root / "entries" / "sha256"
        self.keys = self._io_root / "keys" / "sha256"
        self.intelligence = self._io_root / "intelligence" / "sha256"
        self.intelligence_by_source = (
            self._io_root / "intelligence-by-source" / "sha256"
        )
        self.staging = self._io_root / "staging"
        format_bytes = canonical_json_bytes(
            {"schema": _LAYOUT_SCHEMA, "format": "filesystem-v2"}
        )
        if writable:
            managed_directories = (
                self.entries,
                self.keys,
                self.intelligence,
                self.intelligence_by_source,
                self.staging,
            )
            for directory in managed_directories:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._require_directory(directory)
            self.cas = FileSystemCAS(self.root / "cas")
            self._publish_immutable_bytes(self._io_root / "format.json", format_bytes)
            return

        if (
            _read_regular_file(
                self._io_root / "format.json",
                maximum_bytes=4096,
                code="source-cache.layout-invalid",
            )
            != format_bytes
        ):
            raise SourceCacheError(
                "source-cache.layout-invalid",
                "read-only cache target has another layout format",
            )
        namespace_roots = (
            self.entries.parent,
            self.keys.parent,
            self.intelligence.parent,
            self.intelligence_by_source.parent,
            self._io_root / "cas",
        )
        if not any(path.exists() or path.is_symlink() for path in namespace_roots):
            # Git cannot preserve the empty directories created by a writable cache.
            # A canonical format marker alone is therefore the portable representation
            # of an empty committed target.  Opening it read-only must not mutate it.
            self.available = False
            self.cas = None
            return
        self._require_directory(self.entries)
        self._require_directory(self.keys)
        for directory in (self.intelligence, self.intelligence_by_source):
            if directory.parent.exists() or directory.parent.is_symlink():
                self._require_directory(directory)
        self.cas = FileSystemCAS(self.root / "cas", create=False)

    def publish(
        self,
        entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
        *,
        caller_cas: FileSystemCAS,
        intelligence_attachments: Sequence[SourceIntelligenceAttachment] = (),
    ) -> ContentIdentity:
        """Verify a caller CAS, copy immutable objects, then publish membership last."""

        if type(entry) not in {
            AcceptedSourceCacheEntry,
            StandardAcceptedSourceCacheEntry,
            StandardSourceAdmissionCacheEntry,
        }:
            raise TypeError("cache publication requires an accepted source entry")
        if isinstance(entry, StandardAcceptedSourceCacheEntry):
            try:
                reparsed = StandardAcceptedSourceCacheEntry.from_dict(entry.to_dict())
            except (TypeError, ValueError) as exc:
                raise SourceCacheError(
                    "source-cache.entry-invalid",
                    "Standard cache entry violates its complete resume bindings",
                ) from exc
            if reparsed != entry:
                raise SourceCacheError(
                    "source-cache.entry-invalid",
                    "Standard cache entry changed during invariant verification",
                )
        elif isinstance(entry, StandardSourceAdmissionCacheEntry):
            try:
                reparsed_admission = StandardSourceAdmissionCacheEntry.from_dict(
                    entry.to_dict()
                )
            except (TypeError, ValueError) as exc:
                raise SourceCacheError(
                    "source-cache.entry-invalid",
                    "source-admission entry violates its complete verifier bindings",
                ) from exc
            if reparsed_admission != entry:
                raise SourceCacheError(
                    "source-cache.entry-invalid",
                    "source-admission entry changed during invariant verification",
                )
        if not isinstance(caller_cas, FileSystemCAS):
            raise TypeError("cache publication requires a FileSystemCAS")
        attachments = tuple(intelligence_attachments)
        if len(attachments) > _MAX_SOURCE_INTELLIGENCE_ATTACHMENTS:
            raise SourceCacheError(
                "source-cache.intelligence-limit",
                "source cache publication contains too many intelligence attachments",
            )
        if any(
            not isinstance(item, SourceIntelligenceAttachment) for item in attachments
        ):
            raise TypeError(
                "cache publication intelligence must contain typed attachments"
            )
        if len({item.identity.uri for item in attachments}) != len(attachments):
            raise SourceCacheError(
                "source-cache.intelligence-duplicate",
                "source cache publication repeats an intelligence attachment",
            )
        if not self.writable or self.cas is None:
            raise SourceCacheError(
                "source-cache.write-disabled",
                "this cache target was opened without write authority",
            )
        self._verify_entry_objects(entry, caller_cas)
        for attachment in attachments:
            self._verify_intelligence_attachment(
                attachment,
                caller_cas,
                source_tree_identity=_entry_source_tree_identity(entry),
            )
        references = (
            *_entry_blob_refs(entry),
            *(attachment.artifact for attachment in attachments),
        )
        for reference in references:
            try:
                stored = self.cas.put_file(
                    caller_cas.path_for(reference), media_type=reference.media_type
                )
            except StorageError as exc:
                raise SourceCacheError(
                    "source-cache.object-copy-failed",
                    "an accepted cache object could not be copied",
                ) from exc
            if stored != reference:
                raise SourceCacheError(
                    "source-cache.object-copy-mismatch",
                    "a copied cache object changed identity or metadata",
                )
        self._verify_entry_objects(entry, self.cas)
        for attachment in attachments:
            self._verify_intelligence_attachment(
                attachment,
                self.cas,
                source_tree_identity=_entry_source_tree_identity(entry),
            )
        self._publish_entry_manifest(entry)
        for attachment in sorted(attachments, key=lambda item: item.identity.uri):
            self._publish_intelligence_attachment(attachment)
        self._publish_membership(entry)
        return entry.identity

    def intelligence_attachments(
        self, source_tree_identity: ContentIdentity
    ) -> tuple[SourceIntelligenceAttachment, ...]:
        """Return verified provider artifacts without changing source membership."""

        if not isinstance(source_tree_identity, ContentIdentity):
            raise TypeError("intelligence lookup requires a source-tree identity")
        if not self.available:
            return ()
        if self.cas is None:  # pragma: no cover - guarded by construction
            raise SourceCacheError(
                "source-cache.layout-invalid", "available cache target has no CAS"
            )
        directory = self.intelligence_by_source / source_tree_identity.digest
        if directory.is_symlink():
            raise SourceCacheError(
                "source-cache.path-unsafe",
                "intelligence membership directory is symbolic",
            )
        if not directory.exists():
            return ()
        self._require_directory(directory)
        memberships = sorted(directory.iterdir())
        if len(memberships) > _MAX_SOURCE_INTELLIGENCE_ATTACHMENTS:
            raise SourceCacheError(
                "source-cache.intelligence-limit",
                "source tree exceeds the bounded intelligence attachment limit",
            )
        attachments: list[SourceIntelligenceAttachment] = []
        for membership in memberships:
            if membership.is_symlink() or not membership.is_file():
                raise SourceCacheError(
                    "source-cache.intelligence-membership-invalid",
                    "intelligence membership must be a regular file",
                )
            if not membership.name.endswith(".json"):
                raise SourceCacheError(
                    "source-cache.intelligence-membership-invalid",
                    "intelligence membership contains an unknown object",
                )
            digest = membership.name.removesuffix(".json")
            _require_hex_digest(digest, label="intelligence attachment")
            document, _raw = _read_canonical_json_object(
                membership,
                maximum_bytes=4096,
                code="source-cache.intelligence-membership-invalid",
            )
            expected = {
                "schema": _INTELLIGENCE_MEMBERSHIP_SCHEMA,
                "source_tree_identity": source_tree_identity.uri,
                "attachment_identity": f"sha256:{digest}",
            }
            if document != expected:
                raise SourceCacheError(
                    "source-cache.intelligence-membership-invalid",
                    "intelligence membership does not bind its path and source",
                )
            attachment = self._intelligence_attachment(digest)
            self._verify_intelligence_attachment(
                attachment,
                self.cas,
                source_tree_identity=source_tree_identity,
            )
            attachments.append(attachment)
        return tuple(sorted(attachments, key=lambda item: item.identity.uri))

    def candidates(
        self,
        key: SourceDerivationCacheKey,
        *,
        component_lock_identity: ContentIdentity | None = None,
    ) -> tuple[AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry, ...]:
        """Return structurally verified entries without asserting current acceptance."""

        if not isinstance(key, SourceDerivationCacheKey):
            raise TypeError("cache lookup requires a SourceDerivationCacheKey")
        if component_lock_identity is not None and not isinstance(
            component_lock_identity, ContentIdentity
        ):
            raise TypeError("component_lock_identity must be a ContentIdentity")
        if not self.available:
            return ()
        if self.cas is None:  # pragma: no cover - guarded by construction
            raise SourceCacheError(
                "source-cache.layout-invalid", "available cache target has no CAS"
            )
        lookup_identity = key.accepted_source_lookup_identity
        key_directory = self.keys / lookup_identity.digest
        if key_directory.is_symlink():
            raise SourceCacheError(
                "source-cache.path-unsafe", "cache key directory is symbolic"
            )
        if not key_directory.exists():
            return ()
        self._require_directory(key_directory)
        memberships: list[Path] = []
        try:
            for membership in key_directory.iterdir():
                if len(memberships) >= _MAX_CACHE_CANDIDATES:
                    raise SourceCacheError(
                        "source-cache.candidate-limit",
                        "cache key exceeds the bounded candidate limit",
                    )
                memberships.append(membership)
        except OSError as exc:
            raise SourceCacheError(
                "source-cache.membership-invalid",
                "cache key memberships are unavailable",
            ) from exc
        entries: list[AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry] = []
        for membership in sorted(memberships):
            if membership.is_symlink() or not membership.is_file():
                raise SourceCacheError(
                    "source-cache.membership-invalid",
                    "cache key membership must be a regular file",
                )
            if not membership.name.endswith(".json"):
                raise SourceCacheError(
                    "source-cache.membership-invalid",
                    "cache key directory contains an unknown object",
                )
            entry_digest = membership.name.removesuffix(".json")
            _require_hex_digest(entry_digest, label="entry membership")
            document, _raw = _read_canonical_json_object(
                membership,
                maximum_bytes=4096,
                code="source-cache.membership-invalid",
            )
            expected_membership = {
                "schema": _MEMBERSHIP_SCHEMA,
                "accepted_source_lookup_identity": lookup_identity.uri,
                "entry_identity": f"sha256:{entry_digest}",
            }
            if document != expected_membership:
                raise SourceCacheError(
                    "source-cache.membership-invalid",
                    "cache key membership does not bind its path and key",
                )
            entry = self._entry(entry_digest)
            if (
                component_lock_identity is not None
                and _entry_component_lock_identity(entry) != component_lock_identity
            ):
                raise SourceCacheError(
                    "source-cache.component-lock-mismatch",
                    "cache entry names another exact Component lock",
                )
            if (
                _entry_cache_key(entry).accepted_source_lookup_identity
                != lookup_identity
            ):
                raise SourceCacheError(
                    "source-cache.key-mismatch",
                    "cache entry names another accepted-source lookup identity",
                )
            self._verify_entry_objects(entry, self.cas)
            entries.append(entry)
        return tuple(sorted(entries, key=lambda item: item.identity.uri))

    def published_entries(
        self,
    ) -> tuple[AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry, ...]:
        """Read every published entry, so one target can be promoted into another.

        Ordinary lookup is key-directed because that is how the lifecycle asks. Copying
        a runtime cache into a committed one needs the opposite: everything present,
        verified the same way a lookup would verify it.
        """

        if not self.available or self.cas is None:
            return ()
        if not self.entries.is_dir():
            return ()
        published: list[
            AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry
        ] = []
        for path in sorted(self.entries.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise SourceCacheError(
                    "source-cache.path-unsafe", "cache entry must be a regular file"
                )
            published.append(self._entry(path.stem))
        return tuple(published)

    def verified_published_entries(
        self,
    ) -> tuple[AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry, ...]:
        """Read entries that retain exact key membership and all referenced objects."""

        entries = self.published_entries()
        verified: list[
            AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry
        ] = []
        candidates_by_key: dict[
            str,
            tuple[AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry, ...],
        ] = {}
        for entry in entries:
            key = _entry_cache_key(entry)
            lookup_uri = key.accepted_source_lookup_identity.uri
            candidates = candidates_by_key.get(lookup_uri)
            if candidates is None:
                candidates = self.candidates(key)
                candidates_by_key[lookup_uri] = candidates
            if entry not in candidates:
                raise SourceCacheError(
                    "source-cache.membership-invalid",
                    "published cache entry has no valid accepted-source key membership",
                )
            verified.append(entry)
        return tuple(verified)

    def _entry(
        self, digest: str
    ) -> AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry:
        _require_hex_digest(digest, label="entry")
        path = self.entries / f"{digest}.json"
        document, raw = _read_canonical_json_object(
            path,
            maximum_bytes=_MANIFEST_SIZE_LIMIT,
            code="source-cache.entry-invalid",
        )
        if hashlib.sha256(raw).hexdigest() != digest:
            raise SourceCacheError(
                "source-cache.entry-identity-mismatch",
                "cache entry manifest does not match its content path",
            )
        try:
            schema = document.get("schema")
            if schema == STANDARD_ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA:
                entry = StandardAcceptedSourceCacheEntry.from_dict(document)
            elif schema == STANDARD_SOURCE_ADMISSION_CACHE_ENTRY_SCHEMA:
                entry = StandardSourceAdmissionCacheEntry.from_dict(document)
            else:
                entry = AcceptedSourceCacheEntry.from_dict(document)
        except (TypeError, ValueError) as exc:
            raise SourceCacheError(
                "source-cache.entry-invalid",
                "cache entry manifest violates its public schema",
            ) from exc
        if entry.identity.digest != digest:
            raise SourceCacheError(
                "source-cache.entry-identity-mismatch",
                "cache entry identity does not match its content path",
            )
        return entry

    def _publish_entry_manifest(
        self, entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry
    ) -> None:
        content = canonical_json_bytes(entry.to_dict())
        if hashlib.sha256(content).hexdigest() != entry.identity.digest:
            raise SourceCacheError(
                "source-cache.entry-identity-mismatch",
                "cache entry serialization changed its identity",
            )
        self._publish_immutable_bytes(
            self.entries / f"{entry.identity.digest}.json", content
        )

    def _publish_membership(
        self, entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry
    ) -> None:
        key = _entry_cache_key(entry)
        lookup_identity = key.accepted_source_lookup_identity
        key_directory = self.keys / lookup_identity.digest
        self._require_directory(self.keys)
        if key_directory.is_symlink():
            raise SourceCacheError(
                "source-cache.path-unsafe", "cache key directory is symbolic"
            )
        key_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        self._require_directory(key_directory)
        document = {
            "schema": _MEMBERSHIP_SCHEMA,
            "accepted_source_lookup_identity": lookup_identity.uri,
            "entry_identity": entry.identity.uri,
        }
        self._publish_immutable_bytes(
            key_directory / f"{entry.identity.digest}.json",
            canonical_json_bytes(document),
        )

    def retire_other_memberships(
        self, key: SourceDerivationCacheKey, *, keep: ContentIdentity
    ) -> None:
        """Prune stale exact-key membership pointers after a deliberate override.

        Only the membership index under ``keys/`` is pruned; the immutable entry
        manifest and its CAS objects are left exactly as published and are never
        deleted, so a ``published_entries()`` enumeration can still observe a
        retired entry directly. A retired entry stops being an exact-key
        candidate, though, so ordinary lookup and explicit ``entry_identity``
        selection through ``resolve()`` no longer reach it -- both go through the
        same membership index this prunes. This restores the single-current-
        candidate invariant a caller-initiated forced regeneration is meant to
        establish, without mutating or discarding any immutable accepted-source
        content.
        """

        if not isinstance(key, SourceDerivationCacheKey):
            raise TypeError("membership retirement requires a SourceDerivationCacheKey")
        if not isinstance(keep, ContentIdentity):
            raise TypeError(
                "membership retirement requires the ContentIdentity to keep"
            )
        if not self.writable:
            raise SourceCacheError(
                "source-cache.write-disabled",
                "this cache target was opened without write authority",
            )
        if not self.available or self.cas is None:
            return
        lookup_identity = key.accepted_source_lookup_identity
        key_directory = self.keys / lookup_identity.digest
        if key_directory.is_symlink():
            raise SourceCacheError(
                "source-cache.path-unsafe", "cache key directory is symbolic"
            )
        if not key_directory.exists():
            return
        self._require_directory(key_directory)
        retired = False
        for membership in sorted(key_directory.iterdir()):
            if membership.is_symlink() or not membership.is_file():
                raise SourceCacheError(
                    "source-cache.membership-invalid",
                    "cache key membership must be a regular file",
                )
            if not membership.name.endswith(".json"):
                raise SourceCacheError(
                    "source-cache.membership-invalid",
                    "cache key directory contains an unknown object",
                )
            if membership.name == f"{keep.digest}.json":
                continue
            membership.unlink()
            retired = True
        if retired:
            _fsync_directory(key_directory)

    def _intelligence_attachment(self, digest: str) -> SourceIntelligenceAttachment:
        _require_hex_digest(digest, label="intelligence attachment")
        path = self.intelligence / f"{digest}.json"
        document, raw = _read_canonical_json_object(
            path,
            maximum_bytes=_MANIFEST_SIZE_LIMIT,
            code="source-cache.intelligence-invalid",
        )
        if hashlib.sha256(raw).hexdigest() != digest:
            raise SourceCacheError(
                "source-cache.intelligence-identity-mismatch",
                "intelligence attachment does not match its content path",
            )
        try:
            attachment = SourceIntelligenceAttachment.from_dict(document)
        except (TypeError, ValueError) as exc:
            raise SourceCacheError(
                "source-cache.intelligence-invalid",
                "intelligence attachment violates its public schema",
            ) from exc
        if attachment.identity.digest != digest:
            raise SourceCacheError(
                "source-cache.intelligence-identity-mismatch",
                "intelligence attachment identity does not match its content path",
            )
        return attachment

    def _publish_intelligence_attachment(
        self, attachment: SourceIntelligenceAttachment
    ) -> None:
        content = canonical_json_bytes(attachment.to_dict())
        if hashlib.sha256(content).hexdigest() != attachment.identity.digest:
            raise SourceCacheError(
                "source-cache.intelligence-identity-mismatch",
                "intelligence attachment serialization changed its identity",
            )
        self._publish_immutable_bytes(
            self.intelligence / f"{attachment.identity.digest}.json", content
        )
        source_directory = (
            self.intelligence_by_source / attachment.source_tree_identity.digest
        )
        self._require_directory(self.intelligence_by_source)
        if source_directory.is_symlink():
            raise SourceCacheError(
                "source-cache.path-unsafe",
                "intelligence source membership directory is symbolic",
            )
        source_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        self._require_directory(source_directory)
        membership = {
            "schema": _INTELLIGENCE_MEMBERSHIP_SCHEMA,
            "source_tree_identity": attachment.source_tree_identity.uri,
            "attachment_identity": attachment.identity.uri,
        }
        self._publish_immutable_bytes(
            source_directory / f"{attachment.identity.digest}.json",
            canonical_json_bytes(membership),
        )

    @staticmethod
    def _verify_intelligence_attachment(
        attachment: SourceIntelligenceAttachment,
        cas: FileSystemCAS,
        *,
        source_tree_identity: ContentIdentity,
    ) -> None:
        if attachment.source_tree_identity != source_tree_identity:
            raise SourceCacheError(
                "source-cache.intelligence-source-mismatch",
                "intelligence attachment describes another source tree",
            )
        if attachment.artifact.size > _MAX_SOURCE_INTELLIGENCE_ARTIFACT_BYTES:
            raise SourceCacheError(
                "source-cache.intelligence-limit",
                "source-intelligence artifact exceeds the schema-v1 limit",
            )
        try:
            cas.verify(attachment.artifact)
        except (BlobIntegrityError, BlobNotFoundError, StorageSafetyError) as exc:
            raise SourceCacheError(
                "source-cache.intelligence-object-invalid",
                "source-intelligence artifact is missing or corrupt",
            ) from exc

    def _verify_entry_objects(
        self,
        entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
        cas: FileSystemCAS,
    ) -> None:
        _require_entry_resource_limits(entry)
        files: dict[str, bytes] = {}
        try:
            for source_file in entry.source_files:
                files[source_file.path] = cas.get_bytes(source_file.blob)
        except (BlobIntegrityError, BlobNotFoundError, StorageSafetyError) as exc:
            raise SourceCacheError(
                "source-cache.object-invalid",
                "cache source bytes are missing or corrupt",
            ) from exc
        if (
            generated_source_tree_identity(files)
            != _entry_source_tree_identity(entry).uri
        ):
            raise SourceCacheError(
                "source-cache.source-tree-mismatch",
                "cached source files do not form the accepted source tree",
            )
        if isinstance(entry, StandardSourceAdmissionCacheEntry):
            self._verify_source_admission_objects(entry, cas, files)
            return
        evidence = (
            ("generated-test", entry.generated_test_suite),
            ("build", entry.build_evidence),
            ("test", entry.test_evidence),
            ("acceptance", entry.acceptance_evidence),
            ("provenance", entry.provenance_evidence),
        )
        for label, reference in evidence:
            self._verify_evidence(cas, reference, label=label)
        try:
            source_sbom = cas.get_bytes(entry.source_sbom)
            resolved_sbom = cas.get_bytes(entry.resolved_sbom)
            source_binding = validate_cyclonedx_bom(
                source_sbom,
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=entry.managed_sbom_graph,
            )
            checked_source, resolved_binding = validate_resolved_cyclonedx_bom(
                resolved_sbom,
                source_content=source_sbom,
                source_managed_graph=entry.managed_sbom_graph,
                repository_resolutions=entry.repository_resolutions,
            )
            if (
                checked_source != source_binding
                or source_binding != entry.derivation.source_sbom_binding
                or resolved_binding != entry.derivation.resolved_sbom_binding
            ):
                raise CycloneDxBomError(
                    "sbom.binding-mismatch",
                    "cache BOM bytes differ from their accepted graph bindings",
                )
        except (
            BlobIntegrityError,
            BlobNotFoundError,
            CycloneDxBomError,
            StorageSafetyError,
        ) as exc:
            raise SourceCacheError(
                "source-cache.sbom-invalid",
                "cache CycloneDX source or resolved SBOM is missing or invalid",
            ) from exc

    def _verify_source_admission_objects(
        self,
        entry: StandardSourceAdmissionCacheEntry,
        cas: FileSystemCAS,
        files: Mapping[str, bytes],
    ) -> None:
        for label, reference in (
            ("source-manifest", entry.source_manifest),
            ("generated-test", entry.generated_test_suite),
            ("provenance", entry.provenance_evidence),
            ("source-admission", entry.admission_evidence),
        ):
            self._verify_evidence(cas, reference, label=label)
        try:
            source_bom = cas.get_bytes(entry.source_bom)
        except (BlobIntegrityError, BlobNotFoundError, StorageSafetyError) as exc:
            raise SourceCacheError(
                "source-cache.sbom-invalid",
                "admitted source CycloneDX evidence is missing or corrupt",
            ) from exc
        source_bom_members = tuple(
            value
            for path, value in files.items()
            if path == "source/.literate/sbom.cdx.json"
        )
        if source_bom_members != (source_bom,):
            raise SourceCacheError(
                "source-cache.sbom-invalid",
                "admitted source tree does not contain its exact source SBOM",
            )
        expected_evidence = (
            (
                entry.provenance_evidence,
                entry.membership.generation.output.provenance.to_dict(),
            ),
            (entry.admission_evidence, entry.membership.evidence.semantic_dict()),
        )
        for reference, expected in expected_evidence:
            try:
                actual = cas.get_bytes(reference)
            except (BlobIntegrityError, BlobNotFoundError, StorageSafetyError) as exc:
                raise SourceCacheError(
                    "source-cache.evidence-invalid",
                    "source-admission evidence is missing or corrupt",
                ) from exc
            if actual != canonical_json_bytes(expected):
                raise SourceCacheError(
                    "source-cache.evidence-mismatch",
                    "source-admission evidence differs from membership authority",
                )

    @staticmethod
    def _verify_evidence(cas: FileSystemCAS, reference: BlobRef, *, label: str) -> None:
        if reference.size > _EVIDENCE_SIZE_LIMIT:
            raise SourceCacheError(
                "source-cache.evidence-too-large",
                f"cached {label} evidence exceeds the schema-v1 limit",
            )
        try:
            raw = cas.get_bytes(reference)
        except (BlobIntegrityError, BlobNotFoundError, StorageSafetyError) as exc:
            raise SourceCacheError(
                "source-cache.evidence-invalid",
                f"cached {label} evidence is missing or corrupt",
            ) from exc
        try:
            value = json.loads(raw)
            _require_bounded_json(value)
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise SourceCacheError(
                "source-cache.evidence-invalid",
                f"cached {label} evidence is not JSON",
            ) from exc
        try:
            valid = (
                isinstance(value, dict)
                and isinstance(value.get("schema"), str)
                and _versioned_schema(str(value["schema"]))
                and canonical_json_bytes(value) == raw
                and canonical_identity(value).uri == reference.identity
            )
        except (TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            raise SourceCacheError(
                "source-cache.evidence-invalid",
                f"cached {label} evidence is not one canonical versioned object",
            )

    def _publish_immutable_bytes(self, destination: Path, content: bytes) -> None:
        if not self.writable:
            raise SourceCacheError(
                "source-cache.write-disabled",
                "read-only cache target cannot publish immutable bytes",
            )
        self._require_directory(destination.parent)
        if destination.is_symlink():
            raise SourceCacheError(
                "source-cache.path-unsafe", "immutable cache path is symbolic"
            )
        if destination.exists():
            if (
                _read_regular_file(
                    destination,
                    maximum_bytes=max(len(content), 1) + 1,
                    code="source-cache.immutable-collision",
                )
                != content
            ):
                raise SourceCacheError(
                    "source-cache.immutable-collision",
                    "immutable cache path contains different bytes",
                )
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".publish-", dir=self.staging
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(
                    _native_filesystem_path(temporary),
                    _native_filesystem_path(destination),
                )
            except FileExistsError:
                if (
                    _read_regular_file(
                        destination,
                        maximum_bytes=max(len(content), 1) + 1,
                        code="source-cache.immutable-collision",
                    )
                    != content
                ):
                    raise SourceCacheError(
                        "source-cache.immutable-collision",
                        "immutable cache path contains different bytes",
                    ) from None
            else:
                _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def _io_root(self) -> Path:
        """Keep configured roots stable while native operations share one namespace."""
        return Path(_native_filesystem_path(self.root))

    def _require_directory(self, directory: Path) -> None:
        directory = Path(_native_filesystem_path(directory.absolute()))
        if directory.is_symlink() or not directory.is_dir():
            raise SourceCacheError(
                "source-cache.path-unsafe",
                f"managed cache path is not a directory: {directory}",
            )
        try:
            resolved = directory.resolve(strict=True)
        except OSError as exc:
            raise SourceCacheError(
                "source-cache.path-unsafe",
                f"managed cache path is unavailable: {directory}",
            ) from exc
        if resolved != self._io_root and not resolved.is_relative_to(self._io_root):
            raise SourceCacheError(
                "source-cache.path-unsafe",
                f"managed cache path escaped its root: {directory}",
            )


class LegacyFileSystemV1SourceCache(FileSystemSourceCache):
    """Read a genuine filesystem-v1 root without granting legacy write authority."""

    def __init__(
        self,
        target_id: str,
        root: Path,
        *,
        protected_paths: Iterable[Path] = (),
        writable: bool = False,
    ) -> None:
        if writable:
            raise SourceCacheError(
                "source-cache.legacy-write-disabled",
                "filesystem-v1 cache targets are compatibility readers only",
            )
        if (
            not isinstance(target_id, str)
            or _PORTABLE_TARGET_ID.fullmatch(target_id) is None
        ):
            raise ValueError("cache target ID must be a portable identifier")
        self.target_id = target_id
        self.writable = False
        candidate = _candidate_root(root, protected_paths=protected_paths)
        self.available = candidate.exists()
        self.root = candidate
        self.entries = self._io_root / "entries" / "sha256"
        self.keys = self._io_root / "keys" / "sha256"
        self.intelligence = self._io_root / "intelligence" / "sha256"
        self.intelligence_by_source = (
            self._io_root / "intelligence-by-source" / "sha256"
        )
        self.staging = self._io_root / "staging"
        self.cas: FileSystemCAS | None = None
        if not self.available:
            return
        self.root = _open_managed_root(candidate, protected_paths=protected_paths)
        self.entries = self._io_root / "entries" / "sha256"
        self.keys = self._io_root / "keys" / "sha256"
        self.intelligence = self._io_root / "intelligence" / "sha256"
        self.intelligence_by_source = (
            self._io_root / "intelligence-by-source" / "sha256"
        )
        self.staging = self._io_root / "staging"
        self._require_directory(self.entries)
        self._require_directory(self.keys)
        self.cas = FileSystemCAS(self.root / "cas", create=False)
        expected = canonical_json_bytes(
            {"schema": _LEGACY_LAYOUT_SCHEMA, "format": "filesystem-v1"}
        )
        if (
            _read_regular_file(
                self._io_root / "format.json",
                maximum_bytes=4096,
                code="source-cache.layout-invalid",
            )
            != expected
        ):
            raise SourceCacheError(
                "source-cache.layout-invalid",
                "legacy cache target has another layout format",
            )

    def publish(
        self,
        entry: AcceptedSourceCacheEntry,
        *,
        caller_cas: FileSystemCAS,
        intelligence_attachments: Sequence[SourceIntelligenceAttachment] = (),
    ) -> ContentIdentity:
        raise SourceCacheError(
            "source-cache.legacy-write-disabled",
            "filesystem-v1 cache targets are compatibility readers only",
        )

    def intelligence_attachments(
        self, source_tree_identity: ContentIdentity
    ) -> tuple[SourceIntelligenceAttachment, ...]:
        if not isinstance(source_tree_identity, ContentIdentity):
            raise TypeError("intelligence lookup requires a source-tree identity")
        return ()

    def candidates(
        self,
        key: SourceDerivationCacheKey,
        *,
        component_lock_identity: ContentIdentity | None = None,
    ) -> tuple[AcceptedSourceCacheEntry, ...]:
        if not isinstance(key, SourceDerivationCacheKey):
            raise TypeError("cache lookup requires a SourceDerivationCacheKey")
        if not isinstance(component_lock_identity, ContentIdentity):
            raise SourceCacheError(
                "source-cache.legacy-lock-context-required",
                "filesystem-v1 cache lookup requires an exact Component lock "
                "migration identity",
            )
        if not self.available:
            return ()
        if self.cas is None:  # pragma: no cover - guarded by construction
            raise SourceCacheError(
                "source-cache.layout-invalid", "available cache target has no CAS"
            )
        legacy_key = _legacy_cache_key_document(key)
        legacy_key_identity = canonical_identity(legacy_key)
        key_directory = self.keys / legacy_key_identity.digest
        if key_directory.is_symlink():
            raise SourceCacheError(
                "source-cache.path-unsafe", "cache key directory is symbolic"
            )
        if not key_directory.exists():
            return ()
        self._require_directory(key_directory)
        memberships: list[Path] = []
        try:
            for membership in key_directory.iterdir():
                if len(memberships) >= _MAX_CACHE_CANDIDATES:
                    raise SourceCacheError(
                        "source-cache.candidate-limit",
                        "cache key exceeds the bounded candidate limit",
                    )
                memberships.append(membership)
        except OSError as exc:
            raise SourceCacheError(
                "source-cache.membership-invalid",
                "cache key memberships are unavailable",
            ) from exc
        entries: list[AcceptedSourceCacheEntry] = []
        for membership in sorted(memberships):
            if membership.is_symlink() or not membership.is_file():
                raise SourceCacheError(
                    "source-cache.membership-invalid",
                    "cache key membership must be a regular file",
                )
            if not membership.name.endswith(".json"):
                raise SourceCacheError(
                    "source-cache.membership-invalid",
                    "cache key directory contains an unknown object",
                )
            entry_digest = membership.name.removesuffix(".json")
            _require_hex_digest(entry_digest, label="entry membership")
            document, _raw = _read_canonical_json_object(
                membership,
                maximum_bytes=4096,
                code="source-cache.membership-invalid",
            )
            expected_membership = {
                "schema": _LEGACY_MEMBERSHIP_SCHEMA,
                "cache_key_identity": legacy_key_identity.uri,
                "entry_identity": f"sha256:{entry_digest}",
            }
            if document != expected_membership:
                raise SourceCacheError(
                    "source-cache.membership-invalid",
                    "cache key membership does not bind its path and key",
                )
            entry_document, raw_entry = _read_canonical_json_object(
                self.entries / f"{entry_digest}.json",
                maximum_bytes=_MANIFEST_SIZE_LIMIT,
                code="source-cache.entry-invalid",
            )
            if hashlib.sha256(raw_entry).hexdigest() != entry_digest:
                raise SourceCacheError(
                    "source-cache.entry-identity-mismatch",
                    "cache entry manifest does not match its content path",
                )
            if entry_document.get("schema") != (
                "urn:literate-ai:schema:v1:accepted-source-cache-entry"
            ):
                raise SourceCacheError(
                    "source-cache.entry-invalid",
                    "legacy cache entry does not use the frozen v1 wire",
                )
            try:
                entry = AcceptedSourceCacheEntry.from_dict(
                    entry_document,
                    legacy_component_lock_identity=component_lock_identity,
                )
                legacy_artifact = BlobRef.from_dict(
                    entry_document["codegraph_database"],
                    path="legacy cache entry.codegraph_database",
                )
                legacy_intelligence = SourceIntelligenceArtifact.from_dict(
                    entry_document["source_index"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceCacheError(
                    "source-cache.entry-invalid",
                    "cache entry manifest violates its frozen v1 schema",
                ) from exc
            if entry.derivation.cache_key != key:
                raise SourceCacheError(
                    "source-cache.key-mismatch",
                    "cache entry names another exact derivation key",
                )
            try:
                self.cas.verify(legacy_artifact)
                files = {
                    source_file.path: self.cas.get_bytes(source_file.blob)
                    for source_file in entry.source_files
                }
            except (BlobIntegrityError, BlobNotFoundError, StorageSafetyError) as exc:
                raise SourceCacheError(
                    "source-cache.object-invalid",
                    "legacy cache source or index bytes are missing or corrupt",
                ) from exc
            if generated_source_snapshot_identity(files) != (
                legacy_intelligence.source_snapshot_identity
            ):
                raise SourceCacheError(
                    "source-cache.source-snapshot-mismatch",
                    "legacy cache files do not form the indexed source snapshot",
                )
            self._verify_entry_objects(entry, self.cas)
            entries.append(entry)
        return tuple(sorted(entries, key=lambda item: item.identity.uri))


def _legacy_cache_key_document(key: SourceDerivationCacheKey) -> dict[str, object]:
    document = key.to_dict()
    document["schema"] = "urn:literate-ai:schema:v1:source-derivation-cache-key"
    model = dict(document["model_binding"])
    model["schema"] = "urn:literate-ai:schema:v1:source-cache-model-binding"
    document["model_binding"] = model
    return document


class SourceCacheResolver:
    """Mode-aware set-valued lookup and single-target publication policy."""

    def __init__(
        self,
        configuration: SourceCacheConfiguration,
        stores: Mapping[str, FileSystemSourceCache],
    ) -> None:
        if not isinstance(configuration, SourceCacheConfiguration):
            raise TypeError("cache resolver requires SourceCacheConfiguration")
        expected = (
            set()
            if configuration.mode is SourceCacheMode.OFF
            else {target.target_id for target in configuration.targets}
        )
        if set(stores) != expected:
            raise SourceCacheError(
                "source-cache.target-binding-mismatch",
                "resolved cache stores do not match configured target IDs",
            )
        for target_id, store in stores.items():
            if (
                not isinstance(store, FileSystemSourceCache)
                or store.target_id != target_id
            ):
                raise SourceCacheError(
                    "source-cache.target-binding-mismatch",
                    "resolved cache store names another target ID",
                )
        self.configuration = configuration
        self.stores = dict(stores)

    @classmethod
    def from_configuration(
        cls,
        configuration: SourceCacheConfiguration,
        *,
        project: LoadedProject | None = None,
        operator_roots: Mapping[str, Path] | None = None,
    ) -> SourceCacheResolver:
        if configuration.mode is SourceCacheMode.OFF:
            return cls(configuration, {})
        bindings = {} if operator_roots is None else operator_roots
        protected = project_source_cache_protected_paths(project) if project else ()
        resolved: list[tuple[SourceCacheTarget, Path]] = []
        for target in configuration.targets:
            if target.root_kind is SourceCacheRootKind.PROJECT_RELATIVE:
                if project is None:
                    raise SourceCacheError(
                        "source-cache.project-required",
                        "project-relative cache target requires a loaded project",
                    )
                relative = PurePosixPath(target.root_reference)
                root = project.root.joinpath(*relative.parts)
            else:
                binding = bindings.get(target.root_reference)
                if binding is None:
                    raise SourceCacheError(
                        "source-cache.operator-binding-missing",
                        "operator-bound cache root has no runtime binding",
                    )
                root = Path(binding)
                if not root.is_absolute():
                    raise SourceCacheError(
                        "source-cache.operator-binding-invalid",
                        "operator-bound cache root must be absolute",
                    )
            resolved.append((target, _candidate_root(root, protected_paths=protected)))
        for index, (_target, root) in enumerate(resolved):
            for _other_target, other in resolved[index + 1 :]:
                if _paths_overlap(root, other):
                    raise SourceCacheError(
                        "source-cache.target-overlap",
                        "configured cache target roots overlap",
                    )
        stores = {
            target.target_id: (
                LegacyFileSystemV1SourceCache
                if target.format == "filesystem-v1"
                else FileSystemSourceCache
            )(
                target.target_id,
                root,
                protected_paths=protected,
                writable=(
                    configuration.mode.can_write
                    and target.target_id == configuration.write_target_id
                ),
            )
            for target, root in resolved
        }
        return cls(configuration, stores)

    def resolve(
        self,
        key: SourceDerivationCacheKey,
        *,
        force_regeneration: bool = False,
        entry_identity: ContentIdentity | None = None,
        component_lock_identity: ContentIdentity | None = None,
    ) -> tuple[SourceCacheCandidate, ...]:
        if not isinstance(key, SourceDerivationCacheKey):
            raise TypeError("cache lookup requires a SourceDerivationCacheKey")
        if not isinstance(force_regeneration, bool):
            raise TypeError("force_regeneration must be boolean")
        if entry_identity is not None and not isinstance(
            entry_identity, ContentIdentity
        ):
            raise TypeError("entry_identity must be a ContentIdentity")
        if component_lock_identity is not None and not isinstance(
            component_lock_identity, ContentIdentity
        ):
            raise TypeError("component_lock_identity must be a ContentIdentity")
        if force_regeneration or not self.configuration.mode.can_read:
            return ()
        found: dict[
            str,
            tuple[AcceptedSourceCacheEntry, list[str], FileSystemSourceCache],
        ] = {}
        for target in self.configuration.targets:
            store = self.stores[target.target_id]
            for entry in store.candidates(
                key, component_lock_identity=component_lock_identity
            ):
                identity = entry.identity.uri
                existing = found.get(identity)
                if existing is None:
                    found[identity] = (entry, [target.target_id], store)
                else:
                    if existing[0] != entry:
                        raise SourceCacheError(
                            "source-cache.identity-collision",
                            "cache targets disagree on one entry identity",
                        )
                    existing[1].append(target.target_id)
        candidates = tuple(
            SourceCacheCandidate(entry, tuple(target_ids), store)
            for _identity, (entry, target_ids, store) in sorted(found.items())
        )
        if entry_identity is not None:
            selected = tuple(
                candidate
                for candidate in candidates
                if candidate.identity == entry_identity
            )
            if not selected:
                raise SourceCacheError(
                    "source-cache.entry-not-found",
                    "explicit cache entry identity is not an exact candidate",
                )
            return selected
        if self.configuration.require_unique and len(candidates) > 1:
            raise SourceCacheError(
                "source-cache.ambiguous",
                "exact cache key has multiple accepted entries; select one identity",
            )
        return candidates

    def publish(
        self,
        entry: AcceptedSourceCacheEntry,
        *,
        caller_cas: FileSystemCAS,
        intelligence_attachments: Sequence[SourceIntelligenceAttachment] = (),
    ) -> ContentIdentity:
        if not self.configuration.mode.can_write:
            raise SourceCacheError(
                "source-cache.write-disabled",
                "configured cache mode does not permit publication",
            )
        target_id = self.configuration.write_target_id
        assert target_id is not None
        return self.stores[target_id].publish(
            entry,
            caller_cas=caller_cas,
            intelligence_attachments=intelligence_attachments,
        )

    def retire_other_memberships(
        self, key: SourceDerivationCacheKey, *, keep: ContentIdentity
    ) -> None:
        if not self.configuration.mode.can_write:
            raise SourceCacheError(
                "source-cache.write-disabled",
                "configured cache mode does not permit publication",
            )
        target_id = self.configuration.write_target_id
        assert target_id is not None
        self.stores[target_id].retire_other_memberships(key, keep=keep)


class SourceCacheMaterializer:
    """Install one verified candidate and admit it only after final-path indexing."""

    def __init__(self, *, protected_paths: Iterable[Path] = ()) -> None:
        self.protected_paths = tuple(Path(path) for path in protected_paths)

    def materialize(
        self,
        candidate: SourceCacheCandidate,
        destination: Path,
        *,
        intelligence_verifier: FinalPathSourceIntelligenceVerifier | None = None,
        replace_empty_destination: bool = False,
    ) -> MaterializedCachedSource:
        if not isinstance(candidate, SourceCacheCandidate):
            raise TypeError("materialization requires a SourceCacheCandidate")
        if not isinstance(replace_empty_destination, bool):
            raise TypeError("replace_empty_destination must be boolean")
        final = _candidate_destination(
            destination,
            protected_paths=(*self.protected_paths, candidate.store.root),
            allow_existing_empty=replace_empty_destination,
        )
        _require_entry_resource_limits(candidate.entry)
        cas = candidate.store.cas
        if cas is None:
            raise SourceCacheError(
                "source-cache.materialization-invalid",
                "cache candidate has no available immutable object store",
            )
        try:
            files = {
                source_file.path: cas.get_bytes(source_file.blob)
                for source_file in candidate.entry.source_files
            }
        except (BlobIntegrityError, BlobNotFoundError, StorageSafetyError) as exc:
            raise SourceCacheError(
                "source-cache.materialization-invalid",
                "cached source bytes changed after resolution",
            ) from exc
        staging = Path(
            tempfile.mkdtemp(prefix=f".{final.name}.source-cache-", dir=final.parent)
        )
        installed = False
        installed_identity: tuple[int, int] | None = None
        try:
            _materialize_source_files(staging, files)
            if final.exists() or final.is_symlink():
                if not replace_empty_destination:
                    raise SourceCacheError(
                        "source-cache.destination-exists",
                        "materialization destination appeared during staging",
                    )
                try:
                    if (
                        final.is_symlink()
                        or not final.is_dir()
                        or final.resolve(strict=True) != final
                        or any(final.iterdir())
                    ):
                        raise SourceCacheError(
                            "source-cache.destination-exists",
                            "allocated materialization destination is no longer empty",
                        )
                    final.rmdir()
                except OSError as exc:
                    raise SourceCacheError(
                        "source-cache.destination-exists",
                        "allocated materialization destination changed before install",
                    ) from exc
            os.replace(staging, final)
            installed = True
            local_intelligence: SourceIntelligenceArtifact | None = None
            try:
                installed_identity = _directory_identity(final)
                if intelligence_verifier is not None:
                    local_intelligence = intelligence_verifier.finalize(final, files)
                    if not isinstance(local_intelligence, SourceIntelligenceArtifact):
                        raise TypeError(
                            "final-path intelligence verifier returned another type"
                        )
                    _require_directory_identity(final, installed_identity)
                    _write_intelligence_marker(final, local_intelligence)
                    local_intelligence = intelligence_verifier.verify(
                        final, files, local_intelligence
                    )
                    _require_directory_identity(final, installed_identity)
            except Exception as exc:
                _remove_new_destination(final, expected_identity=installed_identity)
                installed = False
                raise SourceCacheError(
                    "source-cache.final-index-invalid",
                    "final-path source-intelligence verification failed",
                ) from exc
            return MaterializedCachedSource(
                candidate.identity,
                final,
                local_intelligence,
            )
        finally:
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging)
            if installed and not final.exists():
                installed = False


def project_source_cache_protected_paths(project: LoadedProject) -> tuple[Path, ...]:
    """Return authority, receipt, reserved-index, and Git metadata paths the
    cache must avoid."""

    if not isinstance(project, LoadedProject):
        raise TypeError("project protection requires a LoadedProject")
    protected: list[Path] = [
        project.root / PROJECT_FILENAME,
        project.agent_skill,
        project.root / ".codegraph",
        project.root / ".git",
    ]
    for catalog in (
        "component",
        "flavor",
        "skill",
        "workflow",
        "routing",
        "documentation",
    ):
        protected.extend(project.roots(catalog))
    if project.definition.test_receipt is not None:
        protected.append(
            project.root.joinpath(*PurePosixPath(project.definition.test_receipt).parts)
        )
    return tuple(protected)


def _entry_cache_key(
    entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
) -> SourceDerivationCacheKey:
    if isinstance(entry, StandardSourceAdmissionCacheEntry):
        return entry.cache_key
    return entry.derivation.cache_key


def _entry_source_tree_identity(
    entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
) -> ContentIdentity:
    if isinstance(entry, StandardSourceAdmissionCacheEntry):
        return entry.source_tree_identity
    return entry.derivation.source_tree_identity


def _entry_component_lock_identity(
    entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
) -> ContentIdentity:
    if isinstance(entry, StandardSourceAdmissionCacheEntry):
        return entry.component_lock_identity
    return entry.derivation.component_lock_identity


def _entry_blob_refs(
    entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
) -> tuple[BlobRef, ...]:
    if isinstance(entry, StandardSourceAdmissionCacheEntry):
        return (
            *(source_file.blob for source_file in entry.source_files),
            entry.source_manifest,
            entry.source_bom,
            entry.generated_test_suite,
            entry.provenance_evidence,
            entry.admission_evidence,
        )
    return (
        *(source_file.blob for source_file in entry.source_files),
        entry.source_sbom,
        entry.resolved_sbom,
        entry.generated_test_suite,
        entry.build_evidence,
        entry.test_evidence,
        entry.acceptance_evidence,
        entry.provenance_evidence,
    )


def _versioned_schema(value: str) -> bool:
    if _URN_SCHEMA.fullmatch(value) is not None:
        return True
    name, separator, version = value.rpartition("@")
    return bool(separator and name.startswith("literate-ai/") and version.isdigit())


def _require_hex_digest(value: str, *, label: str) -> None:
    if len(value) != 64 or any(character not in _SHA256_HEX for character in value):
        raise SourceCacheError(
            "source-cache.path-invalid", f"{label} path is not a sha256 identity"
        )


def _require_entry_resource_limits(
    entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
) -> None:
    if len(entry.source_files) > _MAX_SOURCE_FILES:
        raise SourceCacheError(
            "source-cache.source-limit",
            "cache entry exceeds the bounded generated-source file count",
        )
    total = 0
    for source_file in entry.source_files:
        size = source_file.blob.size
        if size > _MAX_SOURCE_FILE_BYTES:
            raise SourceCacheError(
                "source-cache.source-limit",
                "cache entry contains an oversized generated-source file",
            )
        total += size
        if total > _MAX_SOURCE_TOTAL_BYTES:
            raise SourceCacheError(
                "source-cache.source-limit",
                "cache entry exceeds the aggregate generated-source byte limit",
            )
    if isinstance(entry, StandardSourceAdmissionCacheEntry):
        oversized = any(
            reference.size > _EVIDENCE_SIZE_LIMIT
            for reference in (
                entry.source_manifest,
                entry.source_bom,
                entry.generated_test_suite,
                entry.provenance_evidence,
                entry.admission_evidence,
            )
        )
    else:
        oversized = entry.source_sbom.size > _EVIDENCE_SIZE_LIMIT or (
            entry.resolved_sbom.size > _EVIDENCE_SIZE_LIMIT
        )
    if oversized:
        raise SourceCacheError(
            "source-cache.evidence-too-large",
            "cache CycloneDX evidence exceeds the bounded artifact limit",
        )


def _require_bounded_json(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise ValueError("JSON object exceeds the cache node limit")
        if depth > _MAX_JSON_DEPTH:
            raise ValueError("JSON object exceeds the cache depth limit")
        if isinstance(current, dict):
            nodes += len(current)
            if nodes > _MAX_JSON_NODES:
                raise ValueError("JSON object exceeds the cache node limit")
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _read_json_object(
    path: Path, *, maximum_bytes: int, code: str
) -> dict[str, object]:
    raw = _read_regular_file(path, maximum_bytes=maximum_bytes, code=code)
    try:
        value = json.loads(raw)
        _require_bounded_json(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SourceCacheError(code, "cache JSON object is malformed") from exc
    if not isinstance(value, dict):
        raise SourceCacheError(code, "cache JSON object must be an object")
    return value


def _read_canonical_json_object(
    path: Path, *, maximum_bytes: int, code: str
) -> tuple[dict[str, object], bytes]:
    raw = _read_regular_file(path, maximum_bytes=maximum_bytes, code=code)
    try:
        value = json.loads(raw)
        _require_bounded_json(value)
        canonical = canonical_json_bytes(value)
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        raise SourceCacheError(code, "cache JSON object is malformed") from exc
    if not isinstance(value, dict) or canonical != raw:
        raise SourceCacheError(code, "cache JSON object is not canonical")
    return value, raw


def _read_regular_file(path: Path, *, maximum_bytes: int, code: str) -> bytes:
    path = Path(_native_filesystem_path(path.absolute()))
    if path.is_symlink():
        raise SourceCacheError(code, "cache object must not be symbolic")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SourceCacheError(code, "cache object is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise SourceCacheError(code, "cache object size or type is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            # read(n) allocates against n, even for a tiny regular file. The
            # aggregate caller budget is a ceiling, not a per-file allocation.
            content = source.read(metadata.st_size + 1)
        if len(content) != metadata.st_size:
            raise SourceCacheError(code, "cache object changed size during read")
        return content
    finally:
        os.close(descriptor)


def _candidate_root(root: Path, *, protected_paths: Iterable[Path]) -> Path:
    candidate = _absolute_lexical(root)
    if candidate == Path(candidate.anchor):
        raise SourceCacheError(
            "source-cache.root-unsafe", "cache target cannot be a filesystem root"
        )
    _reject_symlink_components(candidate)
    for protected in protected_paths:
        if _paths_overlap(candidate, _absolute_lexical(protected)):
            raise SourceCacheError(
                "source-cache.authority-overlap",
                "cache target overlaps project authority or metadata",
            )
    if candidate.exists() and not candidate.is_dir():
        raise SourceCacheError(
            "source-cache.root-unsafe", "cache target root must be a directory"
        )
    return candidate


def _prepare_managed_root(root: Path, *, protected_paths: Iterable[Path]) -> Path:
    candidate = _candidate_root(root, protected_paths=protected_paths)
    candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
    if candidate.is_symlink() or not candidate.is_dir():
        raise SourceCacheError(
            "source-cache.root-unsafe", "cache target root must be a directory"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SourceCacheError(
            "source-cache.root-unsafe", "cache target root is unavailable"
        ) from exc
    if resolved != candidate:
        raise SourceCacheError(
            "source-cache.root-unsafe", "cache target root crossed a symbolic path"
        )
    return resolved


def _open_managed_root(root: Path, *, protected_paths: Iterable[Path]) -> Path:
    candidate = _candidate_root(root, protected_paths=protected_paths)
    if candidate.is_symlink() or not candidate.is_dir():
        raise SourceCacheError(
            "source-cache.root-unsafe",
            "read-only cache target root must already be a directory",
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SourceCacheError(
            "source-cache.root-unsafe", "read-only cache target root is unavailable"
        ) from exc
    if resolved != candidate:
        raise SourceCacheError(
            "source-cache.root-unsafe", "cache target root crossed a symbolic path"
        )
    return resolved


def _candidate_destination(
    destination: Path,
    *,
    protected_paths: Iterable[Path],
    allow_existing_empty: bool = False,
) -> Path:
    final = _absolute_lexical(destination)
    if final == Path(final.anchor) or final.name in {"", ".", ".."}:
        raise SourceCacheError(
            "source-cache.destination-unsafe", "materialization destination is unsafe"
        )
    if final.exists() or final.is_symlink():
        if (
            not allow_existing_empty
            or final.is_symlink()
            or not final.is_dir()
            or final.resolve(strict=True) != final
            or any(final.iterdir())
        ):
            raise SourceCacheError(
                "source-cache.destination-exists",
                "materialization requires a new or explicitly allocated empty "
                "directory",
            )
    for protected in protected_paths:
        if _paths_overlap(final, _absolute_lexical(protected)):
            raise SourceCacheError(
                "source-cache.authority-overlap",
                "materialization destination overlaps authority or cache state",
            )
    parent = final.parent
    _reject_symlink_components(parent)
    if parent.is_symlink() or not parent.is_dir():
        raise SourceCacheError(
            "source-cache.destination-unsafe",
            "materialization parent must be an existing regular directory",
        )
    if parent.resolve(strict=True) != parent:
        raise SourceCacheError(
            "source-cache.destination-unsafe",
            "materialization parent crossed a symbolic path",
        )
    return final


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _reject_symlink_components(path: Path) -> None:
    absolute = _absolute_lexical(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise SourceCacheError(
                "source-cache.path-unsafe", "configured path crosses a symbolic link"
            )
        if current.exists() and current != absolute and not current.is_dir():
            raise SourceCacheError(
                "source-cache.path-unsafe",
                "configured path crosses a non-directory component",
            )


def _paths_overlap(left: Path, right: Path) -> bool:
    left_alias = _portable_path_parts(left)
    right_alias = _portable_path_parts(right)
    if _parts_overlap(left_alias, right_alias):
        return True
    return _physical_existing_ancestor_overlap(left, right)


def _portable_path_parts(path: Path) -> tuple[str, ...]:
    absolute = _absolute_lexical(path)
    return tuple(
        unicodedata.normalize("NFC", part.casefold()) for part in absolute.parts
    )


def _parts_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    common = min(len(left), len(right))
    return left[:common] == right[:common]


def _existing_ancestor_chain(
    path: Path,
) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    current = _absolute_lexical(path)
    missing: tuple[str, ...] = ()
    while not current.exists() and current != Path(current.anchor):
        missing = (current.name, *missing)
        current = current.parent
    result: list[tuple[Path, tuple[str, ...]]] = []
    relative = missing
    while True:
        result.append((current, relative))
        if current == Path(current.anchor):
            break
        relative = (current.name, *relative)
        current = current.parent
    return tuple(result)


def _physical_existing_ancestor_overlap(left: Path, right: Path) -> bool:
    left_chain = _existing_ancestor_chain(left)
    right_chain = _existing_ancestor_chain(right)
    for left_ancestor, left_relative in left_chain:
        for right_ancestor, right_relative in right_chain:
            try:
                same = left_ancestor.samefile(right_ancestor)
            except OSError:
                continue
            if not same:
                continue
            left_alias = tuple(
                unicodedata.normalize("NFC", part.casefold()) for part in left_relative
            )
            right_alias = tuple(
                unicodedata.normalize("NFC", part.casefold()) for part in right_relative
            )
            return _parts_overlap(left_alias, right_alias)
    return False


def _materialize_source_files(root: Path, files: Mapping[str, bytes]) -> None:
    for relative, content in sorted(files.items()):
        path = PurePosixPath(relative)
        target = root.joinpath(*path.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            raise SourceCacheError(
                "source-cache.materialization-collision",
                "staged source path already exists",
            )
        target.write_bytes(content)
        os.chmod(target, 0o600)


def _write_intelligence_marker(
    root: Path, intelligence: SourceIntelligenceArtifact
) -> None:
    marker = root / ".literate-source-intelligence.json"
    content = canonical_json_bytes(intelligence.to_dict())
    temporary = marker.with_name(f".{marker.name}.tmp-{os.getpid()}")
    temporary.write_bytes(content)
    os.chmod(temporary, 0o600)
    os.replace(temporary, marker)


def _directory_identity(destination: Path) -> tuple[int, int]:
    try:
        metadata = destination.lstat()
    except OSError as exc:
        raise SourceCacheError(
            "source-cache.destination-changed",
            "materialized destination became unavailable",
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SourceCacheError(
            "source-cache.destination-changed",
            "materialized destination changed type",
        )
    return metadata.st_dev, metadata.st_ino


def _require_directory_identity(destination: Path, expected: tuple[int, int]) -> None:
    if _directory_identity(destination) != expected:
        raise SourceCacheError(
            "source-cache.destination-changed",
            "materialized destination was replaced during final verification",
        )


def _remove_new_destination(
    destination: Path, *, expected_identity: tuple[int, int] | None
) -> bool:
    if expected_identity is None:
        return False
    try:
        if _directory_identity(destination) != expected_identity:
            return False
    except SourceCacheError:
        return False
    shutil.rmtree(destination)
    return True


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _native_filesystem_path(path: Path) -> str:
    """Return a path suitable for native calls, including long Windows paths."""

    native = os.fspath(path)
    if os.name != "nt" or native.startswith("\\\\?\\"):
        return native
    if native.startswith("\\\\"):
        return "\\\\?\\UNC\\" + native[2:]
    return "\\\\?\\" + native


__all__ = [
    "FileSystemSourceCache",
    "FinalPathSourceIntelligenceVerifier",
    "MaterializedCachedSource",
    "SourceCacheCandidate",
    "SourceCacheError",
    "SourceCacheMaterializer",
    "SourceCacheResolver",
    "project_source_cache_protected_paths",
]
