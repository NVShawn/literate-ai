"""Immutable quarantine materialization and exact trust promotion."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.contracts import (
    SourceEntry,
    SourceEntryType,
    SourceIntelligenceArtifact,
    canonical_relative_posix_path,
    generated_source_snapshot_identity,
    generated_source_tree_identity,
)
from literate_ai.storage import BlobRef, FileSystemCAS, canonical_json_bytes

from .models import GitSignatureVerification, SourceCapture

_BUFFER_SIZE = 1024 * 1024
_QUARANTINE_RECORD_SCHEMA = "urn:literate-ai:schema:v2:quarantine-record"
_LEGACY_QUARANTINE_RECORD_SCHEMA = "urn:literate-ai:schema:v1:quarantine-record"


class QuarantineError(RuntimeError):
    """Source materialization or trust promotion failed closed."""


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    capture: SourceCapture
    tree_path: Path
    manifest_ref: BlobRef
    materialized_entries: tuple[SourceEntry, ...]
    source_intelligence: SourceIntelligenceArtifact | None = None

    @property
    def source_snapshot_id(self) -> str:
        return self.capture.snapshot_id

    def verify(self) -> None:
        _verify_entries(
            self.materialized_entries,
            self.tree_path,
            derived_metadata_paths=(
                (self.source_intelligence.artifact_path.split("/", 1)[0],)
                if self.source_intelligence
                else ()
            ),
        )
        if self.source_intelligence is not None:
            source_files = {
                entry.path: self.tree_path.joinpath(*entry.path.split("/")).read_bytes()
                for entry in self.materialized_entries
            }
            if self.source_intelligence.source_tree_identity != (
                generated_source_tree_identity(source_files)
            ):
                raise QuarantineError(
                    "quarantine source intelligence names another source tree"
                )
            if self.source_intelligence.source_snapshot_identity != (
                generated_source_snapshot_identity(source_files)
            ):
                raise QuarantineError(
                    "quarantine source intelligence names another source snapshot"
                )
            _require_intelligence_artifact(self.tree_path, self.source_intelligence)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": _QUARANTINE_RECORD_SCHEMA,
            "source_snapshot_id": self.capture.snapshot_id,
            "source_tree_id": self.capture.tree_id,
            "manifest_ref": self.manifest_ref.to_dict(),
            "materialized_entries": [
                entry.to_dict() for entry in self.materialized_entries
            ],
            "state": "quarantined",
        }
        if self.source_intelligence is not None:
            value["derived_source_intelligence"] = self.source_intelligence.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class TrustedSource:
    """A verified exact snapshot still resident in immutable quarantine."""

    capture: SourceCapture
    tree_path: Path
    verification: GitSignatureVerification
    decision_ref: BlobRef

    @property
    def source_snapshot_id(self) -> str:
        return self.capture.snapshot_id

    @property
    def source_tree_id(self) -> str:
        return self.capture.tree_id

    def verify(self) -> None:
        if not self.verification.verified:
            raise QuarantineError("trusted source has a rejected verification")
        if self.verification.source_snapshot_id != self.source_snapshot_id:
            raise QuarantineError("trusted source verification binding is invalid")
        _verify_entries(
            tuple(
                entry
                for entry in self.capture.snapshot.entries
                if entry.entry_type is SourceEntryType.FILE
            ),
            self.tree_path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "urn:literate-ai:schema:v1:trusted-source",
            "source_snapshot_id": self.source_snapshot_id,
            "source_tree_id": self.source_tree_id,
            "verification": self.verification.to_dict(),
            "decision_ref": self.decision_ref.to_dict(),
            "state": "trusted",
        }


class QuarantineStore:
    """Copy verified bytes into identity-named, non-overwriting directories."""

    def __init__(self, root: str | Path, cas: FileSystemCAS) -> None:
        configured = Path(root).expanduser()
        if configured.is_symlink():
            raise QuarantineError("quarantine root must not be a symbolic link")
        configured.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = configured.resolve(strict=True)
        self.quarantine_root = self.root / "quarantine"
        self.ready_root = self.root / "ready"
        self.index_locks = self.root / "index-locks"
        for path in (self.quarantine_root, self.ready_root, self.index_locks):
            if path.is_symlink():
                raise QuarantineError("managed quarantine directories cannot be links")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cas = cas

    def materialize(
        self,
        capture: SourceCapture,
        source_root: str | Path,
    ) -> QuarantineRecord:
        """Copy local or Git source files into quarantine and verify every digest."""

        if capture.snapshot.aggregate_members:
            raise QuarantineError("aggregate captures require materialize_aggregate")
        root = _safe_root(source_root)
        materialized_entries = tuple(
            entry
            for entry in capture.snapshot.entries
            if entry.entry_type is SourceEntryType.FILE
        )

        def populate(tree: Path) -> None:
            for entry in capture.snapshot.entries:
                if entry.entry_type is SourceEntryType.SUBMODULE:
                    continue
                if entry.entry_type is not SourceEntryType.FILE:
                    raise QuarantineError(
                        f"unsupported source entry type: {entry.entry_type.value}"
                    )
                _copy_entry(root / entry.path, tree / entry.path, entry)

        return self._materialize(capture, materialized_entries, populate)

    def attach_source_intelligence(
        self,
        record: QuarantineRecord,
        indexer: Callable[[Path], Any],
    ) -> tuple[QuarantineRecord, Any]:
        """Derive intelligence at the final path under an exclusive lock."""

        if record.source_intelligence is not None:
            record.verify()
            return record, record.source_intelligence
        _verify_entries(
            record.materialized_entries,
            record.tree_path,
            derived_metadata_paths=(),
        )
        target = record.tree_path.parent
        if target.parent != self.quarantine_root or target.is_symlink():
            raise QuarantineError("quarantine index target is unmanaged")
        lock = self.index_locks / f"{record.capture.snapshot.identity.digest}.lock"
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    lock,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError as exc:
                raise QuarantineError(
                    "quarantine source-intelligence refresh is already in progress"
                ) from exc
            os.chmod(target, 0o700)
            intelligence = _derive_source_intelligence(record.tree_path, indexer)
            if not isinstance(intelligence, SourceIntelligenceArtifact):
                raise QuarantineError(
                    "quarantine provider returned invalid source intelligence"
                )
            result = QuarantineRecord(
                record.capture,
                record.tree_path,
                record.manifest_ref,
                record.materialized_entries,
                intelligence,
            )
            result.verify()
            marker = target / "record.json"
            temporary = target / f".record.json.tmp-{os.getpid()}"
            try:
                temporary.write_bytes(canonical_json_bytes(result.to_dict()))
                if os.name == "nt":
                    # Windows refuses to replace a read-only destination. The
                    # publication lock serializes cooperating refreshes; the
                    # same-user named-path boundary remains trusted. Restore the
                    # marker even when replacement fails.
                    os.chmod(marker, 0o600)
                    try:
                        os.replace(temporary, marker)
                    finally:
                        os.chmod(marker, 0o400)
                else:
                    os.chmod(temporary, 0o400)
                    os.replace(temporary, marker)
            finally:
                temporary.unlink(missing_ok=True)
            return result, intelligence
        finally:
            if descriptor is not None:
                os.close(descriptor)
                lock.unlink(missing_ok=True)
                os.chmod(target, 0o500)

    def detach_source_intelligence(
        self,
        record: QuarantineRecord,
        *,
        artifact_path: str | None = None,
    ) -> QuarantineRecord:
        """Remove recorded or interrupted derived data without changing source."""

        if record.source_intelligence is None and artifact_path is None:
            record.verify()
            return record
        if record.source_intelligence is not None:
            record.verify()
            recorded_path = record.source_intelligence.artifact_path
            if artifact_path is not None and artifact_path != recorded_path:
                raise QuarantineError(
                    "source-intelligence cleanup names another artifact"
                )
            artifact_path = recorded_path
        assert artifact_path is not None
        try:
            relative = canonical_relative_posix_path(
                artifact_path, label="source intelligence artifact"
            )
        except (TypeError, ValueError) as exc:
            raise QuarantineError(
                "source-intelligence cleanup path is invalid"
            ) from exc
        top_level = relative.parts[0]
        if record.source_intelligence is None:
            _verify_entries(
                record.materialized_entries,
                record.tree_path,
                derived_metadata_paths=(top_level,),
            )
        target = record.tree_path.parent
        if target.parent != self.quarantine_root or target.is_symlink():
            raise QuarantineError("quarantine intelligence target is unmanaged")
        if top_level in {
            entry.path.split("/", 1)[0] for entry in record.materialized_entries
        }:
            raise QuarantineError(
                "source-intelligence artifact overlaps authoritative source"
            )
        artifact = record.tree_path / top_level
        lock = self.index_locks / f"{record.capture.snapshot.identity.digest}.lock"
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    lock,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError as exc:
                raise QuarantineError(
                    "quarantine intelligence refresh is already in progress"
                ) from exc
            os.chmod(target, 0o700)
            os.chmod(record.tree_path, 0o700)
            if artifact.is_symlink():
                raise QuarantineError(
                    "source-intelligence artifact cannot be a symbolic link"
                )
            if artifact.is_dir():
                os.chmod(artifact, 0o700)
                for current, directories, filenames in os.walk(
                    artifact, topdown=False, followlinks=False
                ):
                    current_path = Path(current)
                    for name in filenames:
                        path = current_path / name
                        if path.is_symlink() or not path.is_file():
                            raise QuarantineError(
                                "source-intelligence artifact tree is unsafe"
                            )
                        os.chmod(path, 0o600)
                    for name in directories:
                        path = current_path / name
                        if path.is_symlink() or not path.is_dir():
                            raise QuarantineError(
                                "source-intelligence artifact tree is unsafe"
                            )
                        os.chmod(path, 0o700)
                shutil.rmtree(artifact)
            elif artifact.exists():
                if not artifact.is_file():
                    raise QuarantineError("source-intelligence artifact path is unsafe")
                os.chmod(artifact, 0o600)
                artifact.unlink()
            result = QuarantineRecord(
                record.capture,
                record.tree_path,
                record.manifest_ref,
                record.materialized_entries,
            )
            result.verify()
            marker = target / "record.json"
            temporary = target / f".record.json.tmp-{os.getpid()}"
            try:
                temporary.write_bytes(canonical_json_bytes(result.to_dict()))
                if os.name == "nt":
                    os.chmod(marker, 0o600)
                os.replace(temporary, marker)
                os.chmod(marker, 0o400)
            finally:
                temporary.unlink(missing_ok=True)
            return result
        finally:
            if descriptor is not None:
                os.close(descriptor)
                lock.unlink(missing_ok=True)
                os.chmod(record.tree_path, 0o500)
                os.chmod(target, 0o500)

    def materialize_aggregate(
        self,
        capture: SourceCapture,
        members: Mapping[str, QuarantineRecord | TrustedSource],
    ) -> QuarantineRecord:
        """Materialize exact child quarantine trees under canonical mount paths."""

        if not capture.snapshot.aggregate_members:
            raise QuarantineError("capture is not an aggregate source")
        expected_ids = {item.member_id for item in capture.snapshot.aggregate_members}
        if set(members) != expected_ids:
            raise QuarantineError(
                "aggregate quarantine members do not match the snapshot"
            )
        for member in members.values():
            member.verify()

        materialized_entries = tuple(
            SourceEntry(
                path=f"{aggregate_member.mount_path}/{entry.path}",
                entry_type=SourceEntryType.FILE,
                mode=entry.mode,
                size=entry.size,
                identity=entry.identity,
            )
            for aggregate_member in capture.snapshot.aggregate_members
            for entry in members[aggregate_member.member_id].capture.snapshot.entries
            if entry.entry_type is SourceEntryType.FILE
        )

        def populate(tree: Path) -> None:
            for aggregate_member in capture.snapshot.aggregate_members:
                record = members[aggregate_member.member_id]
                if (
                    record.capture.snapshot.identity
                    != aggregate_member.snapshot_identity
                ):
                    raise QuarantineError(
                        f"aggregate member {aggregate_member.member_id!r} has changed"
                    )
                mount = tree / aggregate_member.mount_path
                for entry in record.capture.snapshot.entries:
                    if entry.entry_type is SourceEntryType.SUBMODULE:
                        continue
                    if entry.entry_type is not SourceEntryType.FILE:
                        raise QuarantineError(
                            "aggregate child entry is not a regular file"
                        )
                    _copy_entry(
                        record.tree_path / entry.path,
                        mount / entry.path,
                        entry,
                    )

        return self._materialize(capture, materialized_entries, populate)

    def promote(
        self,
        record: QuarantineRecord,
        verification: GitSignatureVerification,
    ) -> TrustedSource:
        """Publish a ready marker only for a verified exact Git capture."""

        record.verify()
        capture = record.capture
        if capture.git is None:
            raise QuarantineError("schema v1 trust promotion requires Git verification")
        if capture.git.dirty:
            raise QuarantineError("dirty source cannot be promoted")
        if capture.lfs_pointers:
            raise QuarantineError("unhydrated Git LFS objects cannot be promoted")
        if not verification.verified:
            raise QuarantineError("source signature was not verified")
        if (
            verification.source_snapshot_id != capture.snapshot_id
            or verification.source_tree_id != capture.tree_id
            or verification.commit != capture.git.head_commit
        ):
            raise QuarantineError(
                "signature verification does not bind this exact source"
            )

        decision = {
            "schema": "urn:literate-ai:schema:v1:trusted-source-decision",
            "source_snapshot_id": capture.snapshot_id,
            "source_tree_id": capture.tree_id,
            "quarantine_manifest_ref": record.manifest_ref.to_dict(),
            "verification": verification.to_dict(),
            "state": "trusted",
        }
        decision_ref = self.cas.put_manifest(
            decision,
            media_type="application/vnd.literate-ai.trusted-source+json",
        )
        marker = self.ready_root / f"{capture.snapshot.identity.digest}.json"
        _create_immutable(marker, canonical_json_bytes(decision))
        return TrustedSource(capture, record.tree_path, verification, decision_ref)

    def _materialize(
        self,
        capture: SourceCapture,
        materialized_entries: tuple[SourceEntry, ...],
        populate: Callable[[Path], None],
    ) -> QuarantineRecord:
        manifest_ref = self.cas.put_manifest(
            capture.to_dict(),
            media_type="application/vnd.literate-ai.source-capture+json",
        )
        target = self.quarantine_root / capture.snapshot.identity.digest
        if target.exists():
            source_intelligence = _record_source_intelligence(target / "record.json")
            record = QuarantineRecord(
                capture,
                target / "tree",
                manifest_ref,
                materialized_entries,
                source_intelligence,
            )
            self._verify_existing(target, record)
            return record

        temporary = Path(tempfile.mkdtemp(prefix=".source-", dir=self.quarantine_root))
        try:
            tree = temporary / "tree"
            tree.mkdir(mode=0o700)
            populate(tree)
            _verify_entries(materialized_entries, tree)
            record = QuarantineRecord(
                capture,
                tree,
                manifest_ref,
                materialized_entries,
            )
            record.verify()
            (temporary / "record.json").write_bytes(
                canonical_json_bytes(record.to_dict())
            )
            _make_read_only(tree)
            os.chmod(temporary / "record.json", 0o400)
            os.chmod(temporary, 0o500)
            try:
                os.rename(temporary, target)
            except FileExistsError:
                shutil.rmtree(temporary)
            record = QuarantineRecord(
                capture,
                target / "tree",
                manifest_ref,
                materialized_entries,
            )
            self._verify_existing(target, record)
            return record
        except Exception:
            if temporary.exists():
                os.chmod(temporary, 0o700)
                for current, directories, files in os.walk(temporary):
                    for name in directories:
                        os.chmod(Path(current) / name, 0o700)
                    for name in files:
                        os.chmod(Path(current) / name, 0o600)
                shutil.rmtree(temporary)
            raise

    @staticmethod
    def _verify_existing(target: Path, record: QuarantineRecord) -> None:
        if target.is_symlink() or not target.is_dir():
            raise QuarantineError("quarantine identity is not a regular directory")
        marker = target / "record.json"
        if marker.is_symlink() or not marker.is_file():
            raise QuarantineError("quarantine record is missing")
        try:
            stored = _normalized_quarantine_record(marker.read_bytes())
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise QuarantineError("quarantine record is malformed") from exc
        if stored != record.to_dict():
            raise QuarantineError(
                "quarantine record conflicts with the source identity"
            )
        if record.source_intelligence is not None:
            record.verify()
        else:
            _verify_entries(
                record.materialized_entries,
                record.tree_path,
                derived_metadata_paths=(),
            )


def _safe_root(value: str | Path) -> Path:
    configured = Path(value).expanduser()
    if configured.is_symlink():
        raise QuarantineError("materialization source root must not be a link")
    try:
        root = configured.resolve(strict=True)
    except OSError as exc:
        raise QuarantineError("materialization source root does not exist") from exc
    if not root.is_dir():
        raise QuarantineError("materialization source root must be a directory")
    return root


def _record_source_intelligence(marker: Path) -> SourceIntelligenceArtifact | None:
    if marker.is_symlink() or not marker.is_file():
        raise QuarantineError("quarantine record is missing")
    try:
        value = json.loads(marker.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QuarantineError("quarantine record is malformed") from exc
    if not isinstance(value, Mapping):
        raise QuarantineError("quarantine record is malformed")
    schema = value.get("schema")
    if schema not in {_QUARANTINE_RECORD_SCHEMA, _LEGACY_QUARANTINE_RECORD_SCHEMA}:
        raise QuarantineError("quarantine record schema is unsupported")
    source_intelligence = value.get(
        "derived_source_intelligence",
        value.get("derived_source_index")
        if schema == _LEGACY_QUARANTINE_RECORD_SCHEMA
        else None,
    )
    if source_intelligence is None:
        return None
    if not isinstance(source_intelligence, Mapping):
        raise QuarantineError("quarantine source-intelligence evidence is malformed")
    try:
        return SourceIntelligenceArtifact.from_dict(source_intelligence)
    except (TypeError, ValueError) as exc:
        raise QuarantineError(
            "quarantine source-intelligence evidence is inconsistent"
        ) from exc


def _normalized_quarantine_record(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("quarantine record must be an object")
    if canonical_json_bytes(value) != raw:
        raise ValueError("quarantine record must use canonical JSON bytes")
    schema = value.get("schema")
    if schema not in {_QUARANTINE_RECORD_SCHEMA, _LEGACY_QUARANTINE_RECORD_SCHEMA}:
        raise ValueError("quarantine record schema is unsupported")
    normalized = dict(value)
    if schema == _LEGACY_QUARANTINE_RECORD_SCHEMA:
        if "derived_source_intelligence" in normalized:
            raise ValueError("legacy quarantine record uses a current-only field")
        raw_intelligence = normalized.pop("derived_source_index", None)
        normalized["schema"] = _QUARANTINE_RECORD_SCHEMA
    else:
        if "derived_source_index" in normalized:
            raise ValueError("current quarantine record uses a legacy-only field")
        raw_intelligence = normalized.get("derived_source_intelligence")
    if raw_intelligence is not None:
        if not isinstance(raw_intelligence, Mapping):
            raise ValueError("quarantine source intelligence must be an object")
        normalized["derived_source_intelligence"] = (
            SourceIntelligenceArtifact.from_dict(raw_intelligence).to_dict()
        )
    return normalized


def _copy_entry(source: Path, target: Path, entry: SourceEntry) -> None:
    if source.is_symlink():
        raise QuarantineError(f"source entry became a symbolic link: {entry.path}")
    try:
        source_stat = source.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise QuarantineError(f"source entry disappeared: {entry.path}") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise QuarantineError(f"source entry is not a regular file: {entry.path}")
    actual_mode = 0o755 if source_stat.st_mode & 0o111 else 0o644
    if actual_mode != entry.mode:
        raise QuarantineError(f"source executable mode changed: {entry.path}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            os.fdopen(source_fd, "rb") as input_file,
            os.fdopen(target_fd, "wb") as output_file,
        ):
            if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
                raise QuarantineError(f"source entry is not regular: {entry.path}")
            while chunk := input_file.read(_BUFFER_SIZE):
                output_file.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if size != entry.size or digest.hexdigest() != entry.identity.digest:
        target.unlink(missing_ok=True)
        raise QuarantineError(
            f"source bytes changed during materialization: {entry.path}"
        )
    os.chmod(target, 0o500 if entry.mode & 0o111 else 0o400)


def _verify_entries(
    entries: tuple[SourceEntry, ...],
    tree: Path,
    *,
    derived_metadata_paths: tuple[str, ...] = (),
) -> None:
    if tree.is_symlink() or not tree.is_dir():
        raise QuarantineError("quarantine tree is missing or symbolic")
    expected: set[str] = set()
    derived = set(derived_metadata_paths)
    try:
        if any(
            len(canonical_relative_posix_path(path, label="derived metadata").parts)
            != 1
            for path in derived
        ):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise QuarantineError("quarantine names invalid derived metadata") from exc
    for entry in entries:
        if entry.entry_type is not SourceEntryType.FILE:
            raise QuarantineError("quarantine contains an unsupported entry contract")
        expected.add(entry.path)
        if any(
            entry.path == metadata or entry.path.startswith(f"{metadata}/")
            for metadata in derived
        ):
            raise QuarantineError(
                "source entries collide with reserved derived metadata"
            )
        path = tree / entry.path
        if path.is_symlink() or not path.is_file():
            raise QuarantineError(f"quarantine entry is missing: {entry.path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(_BUFFER_SIZE):
                digest.update(chunk)
                size += len(chunk)
        if size != entry.size or digest.hexdigest() != entry.identity.digest:
            raise QuarantineError(f"quarantine entry failed verification: {entry.path}")
        executable = bool(path.stat(follow_symlinks=False).st_mode & 0o111)
        if executable != bool(entry.mode & 0o111):
            raise QuarantineError(f"quarantine entry mode changed: {entry.path}")

    actual: set[str] = set()
    for current, directories, files in os.walk(tree, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise QuarantineError("quarantine directory contains a symbolic link")
            relative = path.relative_to(tree).as_posix()
            if relative in derived:
                continue
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise QuarantineError("quarantine tree contains a symbolic link")
            actual.add(path.relative_to(tree).as_posix())
    if actual != expected:
        raise QuarantineError("quarantine tree contains unexpected or missing files")


def _derive_source_intelligence(tree: Path, provider: Callable[[Path], Any]) -> Any:
    """Run a provider against immutable source, then restore cache permissions."""

    os.chmod(tree, 0o700)
    try:
        result = provider(tree)
        if not isinstance(result, SourceIntelligenceArtifact):
            raise QuarantineError(
                "source-intelligence provider returned invalid evidence"
            )
        _require_intelligence_artifact(tree, result)
        return result
    finally:
        _make_read_only(tree)


def _require_intelligence_artifact(
    tree: Path, intelligence: SourceIntelligenceArtifact
) -> None:
    relative = canonical_relative_posix_path(
        intelligence.artifact_path, label="source intelligence artifact"
    )
    artifact = tree.joinpath(*relative.parts)
    if artifact.is_symlink() or not artifact.is_file():
        raise QuarantineError("source-intelligence artifact is missing or unsafe")
    actual = f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}"
    if actual != intelligence.artifact_identity:
        raise QuarantineError("source-intelligence artifact changed after derivation")


def _make_read_only(tree: Path) -> None:
    for current, directories, files in os.walk(tree, topdown=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise QuarantineError("quarantine contains an unsafe file")
            executable = bool(path.stat(follow_symlinks=False).st_mode & 0o111)
            os.chmod(path, 0o500 if executable else 0o400)
        for name in directories:
            path = current_path / name
            if path.is_symlink() or not path.is_dir():
                raise QuarantineError("quarantine contains an unsafe directory")
            os.chmod(path, 0o500)
    os.chmod(tree, 0o500)


def _create_immutable(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError as exc:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise QuarantineError(
                "trusted-source marker conflicts with existing state"
            ) from exc
        return
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


__all__ = [
    "QuarantineError",
    "QuarantineRecord",
    "QuarantineStore",
    "TrustedSource",
]
