"""Migration of legacy inverse journals into the disjoint provenance closure."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.paths import canonical_relative_posix_path

from .promotion_materialization import (
    SourcePromotionError,
    _read_regular_file,
    paths_overlap,
)

LEGACY_SOURCE_TRANSLATION_PATH = ".literate/source-translation.json"
LEGACY_JOURNAL_MIGRATION_RECEIPT_SCHEMA = (
    "urn:literate-ai:schema:v2:legacy-source-promotion-journal-migration"
)


@dataclass(frozen=True, slots=True)
class LegacyJournalMigrationRequest:
    """Trusted filesystem boundaries for relocating one historical journal."""

    accepted_root: Path
    provenance_root: Path
    authority_roots: tuple[Path, ...] = ()
    legacy_relative_path: str = LEGACY_SOURCE_TRANSLATION_PATH
    retire_legacy: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted_root", Path(self.accepted_root))
        object.__setattr__(self, "provenance_root", Path(self.provenance_root))
        object.__setattr__(
            self, "authority_roots", tuple(Path(item) for item in self.authority_roots)
        )
        try:
            normalized = canonical_relative_posix_path(
                self.legacy_relative_path, label="legacy journal path"
            ).as_posix()
        except (TypeError, ValueError) as error:
            raise SourcePromotionError(
                "promotion.migration_traversal", str(error)
            ) from error
        if normalized != LEGACY_SOURCE_TRANSLATION_PATH:
            raise SourcePromotionError(
                "promotion.migration_wrong_source",
                "only the legacy .literate/source-translation.json journal may migrate",
            )
        if not isinstance(self.retire_legacy, bool):
            raise SourcePromotionError(
                "promotion.migration_invalid_request", "retire_legacy must be boolean"
            )


@dataclass(frozen=True, slots=True)
class LegacyJournalMigrationReceipt:
    """Portable result of relocating exact journal bytes to a content address."""

    journal_identity: str
    destination_relative_path: str
    byte_count: int
    legacy_retired: bool

    SCHEMA: ClassVar[str] = LEGACY_JOURNAL_MIGRATION_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        try:
            ContentIdentity.parse_uri(self.journal_identity)
            canonical_relative_posix_path(
                self.destination_relative_path,
                label="journal migration destination",
            )
        except (TypeError, ValueError) as error:
            raise SourcePromotionError(
                "promotion.migration_invalid_receipt", str(error)
            ) from error
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise SourcePromotionError(
                "promotion.migration_invalid_receipt",
                "journal migration byte_count must be a non-negative integer",
            )
        if not isinstance(self.legacy_retired, bool):
            raise SourcePromotionError(
                "promotion.migration_invalid_receipt",
                "journal migration legacy_retired must be boolean",
            )

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "journal_identity": self.journal_identity,
            "destination_relative_path": self.destination_relative_path,
            "byte_count": self.byte_count,
            "legacy_retired": self.legacy_retired,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LegacyJournalMigrationReceipt"
    ) -> LegacyJournalMigrationReceipt:
        if not isinstance(value, dict):
            raise SourcePromotionError(
                "promotion.migration_invalid_receipt", f"{path} must be an object"
            )
        expected = {
            "schema",
            "journal_identity",
            "destination_relative_path",
            "byte_count",
            "legacy_retired",
        }
        if set(value) != expected or value.get("schema") != cls.SCHEMA:
            raise SourcePromotionError(
                "promotion.migration_invalid_receipt",
                f"{path} has an unsupported schema or field set",
            )
        for field in ("journal_identity", "destination_relative_path"):
            if not isinstance(value[field], str) or not value[field]:
                raise SourcePromotionError(
                    "promotion.migration_invalid_receipt",
                    f"{path}.{field} must be a non-empty string",
                )
        return cls(
            journal_identity=value["journal_identity"],
            destination_relative_path=value["destination_relative_path"],
            byte_count=value["byte_count"],
            legacy_retired=value["legacy_retired"],
        )


@dataclass(frozen=True, slots=True)
class LegacyJournalMigrationResult:
    destination: Path
    receipt: LegacyJournalMigrationReceipt


class LegacySourcePromotionJournalMigrator:
    """Move legacy accepted-tree journals into a content-addressed provenance root."""

    def __init__(self, *, max_journal_bytes: int = 64 * 1024 * 1024) -> None:
        if max_journal_bytes <= 0:
            raise ValueError("max_journal_bytes must be positive")
        self._max_journal_bytes = max_journal_bytes

    def migrate(
        self, request: LegacyJournalMigrationRequest
    ) -> LegacyJournalMigrationResult:
        if not isinstance(request, LegacyJournalMigrationRequest):
            raise SourcePromotionError(
                "promotion.migration_invalid_request",
                "journal migration request must be typed",
            )
        accepted_root = _direct_directory(
            request.accepted_root, label="accepted specification root"
        )
        provenance_root = _direct_directory(
            request.provenance_root, label="source promotion provenance root"
        )
        authority_roots = tuple(
            _direct_directory(path, label="generation authority root")
            for path in (accepted_root, *request.authority_roots)
        )
        for authority_root in authority_roots:
            if paths_overlap(provenance_root, authority_root):
                raise SourcePromotionError(
                    "promotion.migration_overlap",
                    "source promotion provenance must be outside every authority root",
                )

        relative = PurePosixPath(request.legacy_relative_path)
        legacy = _checked_regular_file(accepted_root, relative)
        content = _read_regular_file(
            accepted_root, relative, max_bytes=self._max_journal_bytes
        )
        _validate_journal_json(content)
        journal_identity = _bytes_identity(content)
        digest = ContentIdentity.parse_uri(journal_identity).digest
        destination_relative = (
            PurePosixPath("source-promotion") / digest / "source-translation.json"
        )
        destination = provenance_root.joinpath(*destination_relative.parts)
        if any(paths_overlap(destination, root) for root in authority_roots):
            raise SourcePromotionError(
                "promotion.migration_overlap",
                "journal migration destination overlaps a generation authority root",
            )

        content_root = _ensure_direct_child(provenance_root, "source-promotion")
        identity_root = _ensure_direct_child(content_root, digest)
        existing = _existing_regular_file(destination)
        if existing is not None:
            if (
                _read_regular_file(
                    provenance_root,
                    destination_relative,
                    max_bytes=self._max_journal_bytes,
                )
                != content
            ):
                raise SourcePromotionError(
                    "promotion.migration_destination_conflict",
                    "content-addressed journal destination contains different bytes",
                )
            if request.retire_legacy:
                _retire_exact_journal(
                    legacy, content, max_bytes=self._max_journal_bytes
                )
            return _migration_result(
                destination,
                destination_relative,
                content,
                journal_identity,
                request.retire_legacy,
            )

        file_descriptor, staging_name = tempfile.mkstemp(
            prefix=".source-translation.litai-", dir=identity_root
        )
        staging = Path(staging_name)
        published = False
        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if (
                _read_regular_file(
                    accepted_root, relative, max_bytes=self._max_journal_bytes
                )
                != content
            ):
                raise SourcePromotionError(
                    "promotion.migration_source_changed",
                    "legacy journal changed while migration was staged",
                )
            try:
                # Hard-link publication is atomic and refuses a concurrent file.
                os.link(staging, destination)
                published = True
                staging.unlink()
            except OSError as error:
                raise SourcePromotionError(
                    "promotion.migration_publish_failed",
                    f"could not publish legacy journal: {error}",
                ) from error
            if request.retire_legacy:
                try:
                    _retire_exact_journal(
                        legacy, content, max_bytes=self._max_journal_bytes
                    )
                except SourcePromotionError:
                    destination.unlink(missing_ok=True)
                    published = False
                    raise
        except SourcePromotionError:
            staging.unlink(missing_ok=True)
            if published:
                destination.unlink(missing_ok=True)
            _remove_empty(identity_root)
            _remove_empty(content_root)
            raise
        except OSError as error:
            staging.unlink(missing_ok=True)
            if published:
                destination.unlink(missing_ok=True)
            _remove_empty(identity_root)
            _remove_empty(content_root)
            raise SourcePromotionError(
                "promotion.migration_failed",
                f"legacy journal migration failed: {error}",
            ) from error

        return _migration_result(
            destination,
            destination_relative,
            content,
            journal_identity,
            request.retire_legacy,
        )


def _migration_result(
    destination: Path,
    relative: PurePosixPath,
    content: bytes,
    identity: str,
    retired: bool,
) -> LegacyJournalMigrationResult:
    return LegacyJournalMigrationResult(
        destination=destination,
        receipt=LegacyJournalMigrationReceipt(
            journal_identity=identity,
            destination_relative_path=relative.as_posix(),
            byte_count=len(content),
            legacy_retired=retired,
        ),
    )


def _direct_directory(path: Path, *, label: str) -> Path:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise SourcePromotionError(
            "promotion.migration_invalid_root",
            f"{label} must be an existing directory: {candidate}",
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SourcePromotionError(
            "promotion.migration_symlink",
            f"{label} must be a direct, non-symlink directory",
        )
    return candidate.resolve(strict=True)


def _checked_regular_file(root: Path, relative: PurePosixPath) -> Path:
    candidate = root
    for index, part in enumerate(relative.parts):
        candidate /= part
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise SourcePromotionError(
                "promotion.migration_source_missing",
                f"legacy journal is unavailable: {relative.as_posix()}",
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise SourcePromotionError(
                "promotion.migration_symlink",
                f"legacy journal traverses a symlink: {relative.as_posix()}",
            )
        final = index == len(relative.parts) - 1
        if final and not stat.S_ISREG(metadata.st_mode):
            raise SourcePromotionError(
                "promotion.migration_source_invalid",
                "legacy journal must be a regular file",
            )
        if not final and not stat.S_ISDIR(metadata.st_mode):
            raise SourcePromotionError(
                "promotion.migration_source_invalid",
                "legacy journal parent must be a directory",
            )
    return candidate


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
    except OSError as error:
        raise SourcePromotionError(
            "promotion.migration_read_failed", f"could not read journal: {error}"
        ) from error
    if len(content) > limit:
        raise SourcePromotionError(
            "promotion.migration_limit_exceeded",
            f"legacy journal exceeds {limit} bytes",
        )
    return content


def _validate_journal_json(content: bytes) -> None:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourcePromotionError(
            "promotion.migration_invalid_journal",
            "legacy source-translation journal must be UTF-8 JSON",
        ) from error
    if not isinstance(value, dict) or not isinstance(value.get("schema"), str):
        raise SourcePromotionError(
            "promotion.migration_invalid_journal",
            "legacy source-translation journal must be a schema-tagged object",
        )


def _ensure_direct_child(parent: Path, name: str) -> Path:
    child = parent / name
    try:
        child.mkdir()
    except FileExistsError:
        pass
    try:
        metadata = child.lstat()
    except OSError as error:
        raise SourcePromotionError(
            "promotion.migration_invalid_destination",
            f"could not inspect provenance directory {child}",
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SourcePromotionError(
            "promotion.migration_symlink",
            f"provenance path must be a direct directory: {child}",
        )
    return child


def _existing_regular_file(path: Path) -> Path | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SourcePromotionError(
            "promotion.migration_invalid_destination",
            f"could not inspect journal destination: {error}",
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SourcePromotionError(
            "promotion.migration_symlink",
            "journal destination must be absent or a direct regular file",
        )
    return path


def _retire_exact_journal(legacy: Path, expected: bytes, *, max_bytes: int) -> None:
    """Retire only the exact object held and re-read through one descriptor."""

    backup = legacy.with_name(f".{legacy.name}.litai-migration-{uuid.uuid4().hex}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    moved = False
    try:
        descriptor = os.open(legacy, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourcePromotionError(
                "promotion.migration_source_invalid",
                "legacy journal must remain a regular file",
            )
        content = bytearray()
        while len(content) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > max_bytes or bytes(content) != expected:
            raise SourcePromotionError(
                "promotion.migration_source_changed",
                "legacy journal changed before retirement",
            )
        if _object_signature(before) != _object_signature(os.fstat(descriptor)):
            raise SourcePromotionError(
                "promotion.migration_source_changed",
                "legacy journal changed while retirement was prepared",
            )
        # Windows denies renaming a file while this process holds a normal
        # descriptor to it.  Preserve the same fail-closed boundary by closing
        # only after the final descriptor-backed read/stat, then verifying that
        # the object actually moved still has the captured identity.
        if os.name == "nt":
            os.close(descriptor)
            descriptor = None
        os.replace(legacy, backup)
        moved = True
        moved_metadata = backup.lstat()
        if stat.S_ISLNK(moved_metadata.st_mode) or _object_signature(
            moved_metadata
        ) != _object_signature(before):
            raise SourcePromotionError(
                "promotion.migration_source_changed",
                "legacy journal path changed before retirement",
            )
        backup.unlink()
        moved = False
    except SourcePromotionError:
        if moved and backup.exists() and not legacy.exists():
            os.replace(backup, legacy)
        raise
    except OSError as error:
        if moved and backup.exists() and not legacy.exists():
            os.replace(backup, legacy)
        raise SourcePromotionError(
            "promotion.migration_retire_failed",
            f"could not retire legacy journal: {error}",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _object_signature(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _bytes_identity(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _remove_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


__all__ = [
    "LEGACY_JOURNAL_MIGRATION_RECEIPT_SCHEMA",
    "LEGACY_SOURCE_TRANSLATION_PATH",
    "LegacyJournalMigrationReceipt",
    "LegacyJournalMigrationRequest",
    "LegacyJournalMigrationResult",
    "LegacySourcePromotionJournalMigrator",
]
