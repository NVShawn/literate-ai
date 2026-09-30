"""Source-free materialization of reviewed promotion inputs.

This application service is intentionally narrower than a tree copier.  A caller must
name every admitted file, classify it as a forward-generation authority, and pin its
exact bytes before the service creates a Component closure.  Provenance, prompts,
inverse journals, and retained source have no representable input kind here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from errno import ELOOP
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar

from literate_ai.contracts import (
    ComponentAuthorityProjection,
    ComponentGenerationClosure,
    ContentIdentity,
    canonical_identity,
    canonical_relative_posix_path,
    canonical_relative_posix_paths,
)

from .errors import SourceToSpecificationError

if TYPE_CHECKING:
    from .qualification_lifecycle import QualificationLifecycleResult
from .inventory import SourceInventory

GENERATION_INPUT_AUDIT_ENTRY_SCHEMA = (
    "urn:literate-ai:schema:v2:generation-input-audit-entry"
)
GENERATION_INPUT_AUDIT_SCHEMA = "urn:literate-ai:schema:v2:generation-input-audit"
MATERIALIZED_PROMOTION_TREE_SCHEMA = (
    "urn:literate-ai:schema:v2:materialized-promotion-tree"
)
PREVIOUS_SOURCE_PROMOTION_PROVENANCE_SCHEMA = (
    "urn:literate-ai:schema:v2:source-promotion-provenance"
)
SOURCE_PROMOTION_PROVENANCE_SCHEMA = (
    "urn:literate-ai:schema:v3:source-promotion-provenance"
)

_ROOT_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_FORBIDDEN_CLOSURE_SEGMENTS = frozenset(
    {".codegraph", ".git", ".literate", "provenance"}
)


class SourcePromotionError(SourceToSpecificationError):
    """A stable failure at the source-free promotion boundary."""


class PromotionInputKind(StrEnum):
    """The complete allowlist of authority that forward generation may consume."""

    SPECIFICATION = "specification"
    COMPONENT_INTENT = "component-intent"
    REVIEWED_FLAVOR = "reviewed-flavor"
    FORWARD_SKILL = "forward-skill"
    WORKFLOW = "workflow"
    ROUTING_POLICY = "routing-policy"
    PROJECT_CONFIGURATION = "project-configuration"


@dataclass(frozen=True, slots=True)
class SourcePromotionInput:
    """One exact file admitted from a trusted logical root into a Component closure."""

    kind: PromotionInputKind
    source_root: Path
    source_root_label: str
    source_path: str
    target_path: str
    expected_content_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PromotionInputKind):
            raise SourcePromotionError(
                "promotion.invalid_input_kind",
                "promotion input kind must be a PromotionInputKind",
            )
        object.__setattr__(self, "source_root", Path(self.source_root))
        _validate_root_label(self.source_root_label)
        _portable_path(self.source_path, label="promotion source path")
        target = _portable_path(self.target_path, label="promotion target path")
        if any(part.casefold() in _FORBIDDEN_CLOSURE_SEGMENTS for part in target.parts):
            raise SourcePromotionError(
                "promotion.forbidden_target",
                f"promotion target {self.target_path!r} enters a "
                "non-generatable closure",
            )
        _content_identity(self.expected_content_identity, "expected content identity")


@dataclass(frozen=True, slots=True)
class GenerationInputAuditEntry:
    """Portable evidence for one and only one file read during materialization."""

    kind: PromotionInputKind
    source_root_label: str
    source_path: str
    target_path: str
    content_identity: str
    byte_count: int

    SCHEMA: ClassVar[str] = GENERATION_INPUT_AUDIT_ENTRY_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PromotionInputKind):
            raise SourcePromotionError(
                "promotion.invalid_audit", "audit kind must be a PromotionInputKind"
            )
        _validate_root_label(self.source_root_label)
        _portable_path(self.source_path, label="audit source path")
        target = _portable_path(self.target_path, label="audit target path")
        if any(part.casefold() in _FORBIDDEN_CLOSURE_SEGMENTS for part in target.parts):
            raise SourcePromotionError(
                "promotion.forbidden_target",
                f"audit target {self.target_path!r} enters a non-generatable closure",
            )
        _content_identity(self.content_identity, "audit content identity")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise SourcePromotionError(
                "promotion.invalid_audit",
                "audit byte_count must be a non-negative integer",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind.value,
            "source_root_label": self.source_root_label,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "content_identity": self.content_identity,
            "byte_count": self.byte_count,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, path: str = "GenerationInputAuditEntry"
    ) -> GenerationInputAuditEntry:
        data = _exact_mapping(
            value,
            path=path,
            fields={
                "schema",
                "kind",
                "source_root_label",
                "source_path",
                "target_path",
                "content_identity",
                "byte_count",
            },
            schema=cls.SCHEMA,
        )
        try:
            kind = PromotionInputKind(data["kind"])
        except (TypeError, ValueError) as error:
            raise SourcePromotionError(
                "promotion.invalid_audit", f"{path}.kind is not an admitted input kind"
            ) from error
        return cls(
            kind=kind,
            source_root_label=_string(
                data["source_root_label"], path, "source_root_label"
            ),
            source_path=_string(data["source_path"], path, "source_path"),
            target_path=_string(data["target_path"], path, "target_path"),
            content_identity=_string(
                data["content_identity"], path, "content_identity"
            ),
            byte_count=data["byte_count"],
        )


@dataclass(frozen=True, slots=True)
class GenerationInputAudit:
    """Complete, content-addressed account of a materializer's read closure."""

    entries: tuple[GenerationInputAuditEntry, ...]
    materialized_tree_identity: str

    SCHEMA: ClassVar[str] = GENERATION_INPUT_AUDIT_SCHEMA

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        if not entries:
            raise SourcePromotionError(
                "promotion.empty_allowlist", "generation input audit must not be empty"
            )
        if any(not isinstance(item, GenerationInputAuditEntry) for item in entries):
            raise SourcePromotionError(
                "promotion.invalid_audit", "audit entries must be typed audit entries"
            )
        canonical = tuple(
            sorted(
                entries,
                key=lambda item: (
                    item.target_path,
                    item.kind.value,
                    item.source_root_label,
                    item.source_path,
                ),
            )
        )
        if canonical != entries:
            raise SourcePromotionError(
                "promotion.noncanonical_audit", "audit entries must use canonical order"
            )
        _validate_audit_paths(entries)
        expected = _materialized_tree_identity(entries)
        _content_identity(self.materialized_tree_identity, "materialized tree identity")
        if self.materialized_tree_identity != expected:
            raise SourcePromotionError(
                "promotion.invalid_tree_identity",
                "materialized tree identity does not match the audited files",
            )

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "entries": [entry.to_dict() for entry in self.entries],
            "materialized_tree_identity": self.materialized_tree_identity,
        }

    def verify(self, project_root: Path) -> None:
        """Resolve every audited target and require the exact recorded bytes."""

        root = _require_direct_directory(Path(project_root), label="promoted project")
        for entry in self.entries:
            content = _read_regular_file(
                root,
                _portable_path(entry.target_path, label="audit target path"),
                max_bytes=max(entry.byte_count, 1),
            )
            if (
                len(content) != entry.byte_count
                or _bytes_identity(content) != entry.content_identity
            ):
                raise SourcePromotionError(
                    "promotion.audit_target_changed",
                    f"audited forward input changed: {entry.target_path}",
                )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, path: str = "GenerationInputAudit"
    ) -> GenerationInputAudit:
        data = _exact_mapping(
            value,
            path=path,
            fields={"schema", "entries", "materialized_tree_identity"},
            schema=cls.SCHEMA,
        )
        raw_entries = data["entries"]
        if not isinstance(raw_entries, list):
            raise SourcePromotionError(
                "promotion.invalid_audit", f"{path}.entries must be an array"
            )
        return cls(
            entries=tuple(
                GenerationInputAuditEntry.from_dict(
                    item, path=f"{path}.entries[{index}]"
                )
                for index, item in enumerate(raw_entries)
            ),
            materialized_tree_identity=_string(
                data["materialized_tree_identity"],
                path,
                "materialized_tree_identity",
            ),
        )


@dataclass(frozen=True, slots=True)
class SourcePromotionMaterializationResult:
    """Materialized filesystem location plus its portable audit."""

    output_root: Path
    audit: GenerationInputAudit


@dataclass(frozen=True, slots=True)
class SourceExclusionAssessment:
    """Derived comparison between an audited generation closure and source baseline."""

    generation_input_audit_identity: str
    materialized_tree_identity: str
    source_inventory_identity: str
    overlapping_paths: tuple[str, ...]
    overlapping_content_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        for value, label in (
            (self.generation_input_audit_identity, "generation input audit identity"),
            (self.materialized_tree_identity, "materialized tree identity"),
            (self.source_inventory_identity, "source inventory identity"),
        ):
            _content_identity(value, label)
        for field in ("overlapping_paths", "overlapping_content_identities"):
            values = getattr(self, field)
            if values != tuple(sorted(set(values))):
                raise SourcePromotionError(
                    "promotion.source_exclusion_invalid",
                    f"{field} must be canonical and unique",
                )
        for path in self.overlapping_paths:
            _portable_path(path, label="source-exclusion overlap path")
        for identity in self.overlapping_content_identities:
            _content_identity(identity, "source-exclusion overlap identity")

    @property
    def source_excluded(self) -> bool:
        """Return the measured result; callers cannot supply this as a claim."""

        return not self.overlapping_paths and not self.overlapping_content_identities

    def to_dict(self) -> dict[str, object]:
        return {
            "generation_input_audit_identity": self.generation_input_audit_identity,
            "materialized_tree_identity": self.materialized_tree_identity,
            "source_inventory_identity": self.source_inventory_identity,
            "overlapping_paths": list(self.overlapping_paths),
            "overlapping_content_identities": list(self.overlapping_content_identities),
            "source_excluded": self.source_excluded,
        }


def assess_source_exclusion(
    audit: GenerationInputAudit, source_inventory: SourceInventory
) -> SourceExclusionAssessment:
    """Derive source exclusion from exact path and byte identities, never a flag."""

    if not isinstance(audit, GenerationInputAudit) or not isinstance(
        source_inventory, SourceInventory
    ):
        raise SourcePromotionError(
            "promotion.source_exclusion_invalid",
            "source exclusion requires a typed audit and source inventory",
        )
    source_paths = {entry.path for entry in source_inventory.entries}
    source_identities = {entry.content_digest for entry in source_inventory.entries}
    overlapping_paths = tuple(
        sorted(
            source_paths.intersection(
                {
                    path
                    for entry in audit.entries
                    for path in (entry.source_path, entry.target_path)
                }
            )
        )
    )
    overlapping_content_identities = tuple(
        sorted(
            source_identities.intersection(
                {entry.content_identity for entry in audit.entries}
            )
        )
    )
    return SourceExclusionAssessment(
        generation_input_audit_identity=audit.identity,
        materialized_tree_identity=audit.materialized_tree_identity,
        source_inventory_identity=source_inventory.identity,
        overlapping_paths=overlapping_paths,
        overlapping_content_identities=overlapping_content_identities,
    )


@dataclass(frozen=True, slots=True)
class VerifiedSourcePromotionEvidence:
    """Exact current inputs recovered from persisted promotion evidence."""

    promotion_input_audits: tuple[GenerationInputAudit, ...]
    authority_projection: ComponentAuthorityProjection | None = None
    target_lock_identity: ContentIdentity | None = None
    generation_closure: ComponentGenerationClosure | None = None
    verifier_identity: ContentIdentity | None = None
    policy_identity: ContentIdentity | None = None
    qualification_lifecycle_result: QualificationLifecycleResult | None = None
    translation: Mapping[str, Any] | None = None
    translation_identity: ContentIdentity | None = None
    inverse_evidence_custody: Mapping[str, Any] | None = None
    inverse_evidence_custody_identity: ContentIdentity | None = None

    def __post_init__(self) -> None:
        if not self.promotion_input_audits or any(
            not isinstance(item, GenerationInputAudit)
            for item in self.promotion_input_audits
        ):
            raise SourcePromotionError(
                "promotion.evidence_invalid",
                "verified promotion evidence requires typed input audits",
            )
        audit_identities = tuple(item.identity for item in self.promotion_input_audits)
        if audit_identities != tuple(sorted(audit_identities)) or len(
            set(audit_identities)
        ) != len(audit_identities):
            raise SourcePromotionError(
                "promotion.evidence_invalid",
                "verified promotion audits must be canonical and unique",
            )
        if self.authority_projection is not None and not isinstance(
            self.authority_projection, ComponentAuthorityProjection
        ):
            raise SourcePromotionError(
                "promotion.lifecycle_evidence_invalid",
                "promotion authority projection must be a v2 typed projection",
            )
        for name in (
            "target_lock_identity",
            "verifier_identity",
            "policy_identity",
            "translation_identity",
            "inverse_evidence_custody_identity",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, ContentIdentity):
                raise SourcePromotionError(
                    "promotion.lifecycle_evidence_invalid",
                    f"promotion {name} must be a ContentIdentity",
                )
        if self.qualification_lifecycle_result is not None:
            from .qualification_lifecycle import QualificationLifecycleResult

            if not isinstance(
                self.qualification_lifecycle_result, QualificationLifecycleResult
            ):
                raise SourcePromotionError(
                    "promotion.lifecycle_evidence_invalid",
                    "promotion qualification evidence must be a lifecycle result",
                )
        for value, identity, label in (
            (self.translation, self.translation_identity, "translation"),
            (
                self.inverse_evidence_custody,
                self.inverse_evidence_custody_identity,
                "inverse evidence custody",
            ),
        ):
            if (value is None) != (identity is None):
                raise SourcePromotionError(
                    "promotion.evidence_invalid",
                    f"promotion {label} and identity must be retained together",
                )
            if value is not None and (
                not isinstance(value, Mapping) or canonical_identity(value) != identity
            ):
                raise SourcePromotionError(
                    "promotion.evidence_mismatch",
                    f"promotion {label} does not match its content identity",
                )
        if self.generation_closure is not None:
            if not isinstance(self.generation_closure, ComponentGenerationClosure):
                raise SourcePromotionError(
                    "promotion.lifecycle_evidence_invalid",
                    "promotion generation closure must be typed",
                )
            matching_audits = tuple(
                audit
                for audit in self.promotion_input_audits
                if audit.identity
                == self.generation_closure.promotion_input_audit_identity.uri
            )
            if len(matching_audits) != 1 or (
                matching_audits[0].materialized_tree_identity
                != self.generation_closure.promotion_tree_identity.uri
            ):
                raise SourcePromotionError(
                    "promotion.lifecycle_evidence_drift",
                    "generation closure does not bind its exact promotion "
                    "audit and tree",
                )

    @property
    def qualification_evidence_identities(self) -> tuple[ContentIdentity, ...]:
        identities: dict[str, ContentIdentity] = {}
        for audit in self.promotion_input_audits:
            identity = ContentIdentity.parse_uri(audit.identity)
            identities[identity.uri] = identity
        if self.generation_closure is not None:
            qualification = self.generation_closure.qualification_evidence_identity
            identities[qualification.uri] = qualification
        for identity in (
            self.translation_identity,
            self.inverse_evidence_custody_identity,
        ):
            if identity is not None:
                identities[identity.uri] = identity
        return tuple(identities[uri] for uri in sorted(identities))

    def audit_containing(self, target_path: str) -> GenerationInputAudit:
        target = _portable_path(target_path, label="qualification Component path")
        matches = tuple(
            audit
            for audit in self.promotion_input_audits
            if any(entry.target_path == target.as_posix() for entry in audit.entries)
        )
        if len(matches) != 1:
            raise SourcePromotionError(
                "promotion.qualification_audit_ambiguous",
                "qualification requires exactly one promotion audit for the Component",
            )
        return matches[0]


class SourcePromotionMaterializer:
    """Atomically create a source-free tree from exact allowlisted files."""

    def __init__(
        self,
        *,
        max_files: int = 4096,
        max_file_bytes: int = 16 * 1024 * 1024,
        max_total_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        if min(max_files, max_file_bytes, max_total_bytes) <= 0:
            raise ValueError("materialization limits must be positive")
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes

    def materialize(
        self, inputs: Iterable[SourcePromotionInput], output_root: Path
    ) -> SourcePromotionMaterializationResult:
        admitted = tuple(inputs)
        if not admitted:
            raise SourcePromotionError(
                "promotion.empty_allowlist", "at least one promotion input is required"
            )
        if len(admitted) > self._max_files:
            raise SourcePromotionError(
                "promotion.limit_exceeded",
                f"promotion input count exceeds {self._max_files}",
            )
        if any(not isinstance(item, SourcePromotionInput) for item in admitted):
            raise SourcePromotionError(
                "promotion.invalid_input", "all promotion inputs must be typed"
            )
        target = Path(output_root)
        parent = target.parent
        _require_direct_directory(parent, label="promotion output parent")
        if target.exists() or target.is_symlink():
            raise SourcePromotionError(
                "promotion.target_exists", f"promotion output already exists: {target}"
            )

        source_roots = _validated_source_roots(admitted)
        for root in source_roots.values():
            if paths_overlap(root, target):
                raise SourcePromotionError(
                    "promotion.path_overlap",
                    "promotion output must not overlap a source authority root",
                )
        _validate_input_paths(admitted, source_roots)

        loaded: list[tuple[SourcePromotionInput, bytes]] = []
        total = 0
        for item in sorted(admitted, key=lambda value: value.target_path):
            root = source_roots[item.source_root_label]
            content = _read_regular_file(
                root,
                _portable_path(item.source_path, label="promotion source path"),
                max_bytes=self._max_file_bytes,
            )
            identity = _bytes_identity(content)
            if identity != item.expected_content_identity:
                raise SourcePromotionError(
                    "promotion.input_identity_mismatch",
                    f"promotion input {item.source_root_label}:{item.source_path} "
                    "does not match its content pin",
                )
            total += len(content)
            if total > self._max_total_bytes:
                raise SourcePromotionError(
                    "promotion.limit_exceeded",
                    f"promotion input bytes exceed {self._max_total_bytes}",
                )
            loaded.append((item, content))

        entries = tuple(
            GenerationInputAuditEntry(
                kind=item.kind,
                source_root_label=item.source_root_label,
                source_path=item.source_path,
                target_path=item.target_path,
                content_identity=item.expected_content_identity,
                byte_count=len(content),
            )
            for item, content in loaded
        )
        audit = GenerationInputAudit(
            entries=entries,
            materialized_tree_identity=_materialized_tree_identity(entries),
        )

        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.litai-", dir=parent))
        try:
            for item, content in loaded:
                destination = staging.joinpath(*PurePosixPath(item.target_path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())

            # Do not publish a tree from inputs that changed while it was staged.
            for item, original in loaded:
                current = _read_regular_file(
                    source_roots[item.source_root_label],
                    _portable_path(item.source_path, label="promotion source path"),
                    max_bytes=self._max_file_bytes,
                )
                if current != original:
                    raise SourcePromotionError(
                        "promotion.input_changed",
                        f"promotion input changed while staging: {item.source_path}",
                    )
            if target.exists() or target.is_symlink():
                raise SourcePromotionError(
                    "promotion.target_exists",
                    f"promotion output appeared while staging: {target}",
                )
            os.rename(staging, target)
        except SourcePromotionError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError as error:
            shutil.rmtree(staging, ignore_errors=True)
            raise SourcePromotionError(
                "promotion.materialization_failed",
                f"could not publish promotion tree: {error}",
            ) from error
        return SourcePromotionMaterializationResult(output_root=target, audit=audit)

    def audit(self, inputs: Iterable[SourcePromotionInput]) -> GenerationInputAudit:
        """Capture an exact allowlist without creating a second copy of its tree."""

        admitted = tuple(inputs)
        if not admitted:
            raise SourcePromotionError(
                "promotion.empty_allowlist", "at least one promotion input is required"
            )
        if len(admitted) > self._max_files:
            raise SourcePromotionError(
                "promotion.limit_exceeded",
                f"promotion input count exceeds {self._max_files}",
            )
        if any(not isinstance(item, SourcePromotionInput) for item in admitted):
            raise SourcePromotionError(
                "promotion.invalid_input", "all promotion inputs must be typed"
            )
        source_roots = _validated_source_roots(admitted)
        _validate_input_paths(admitted, source_roots)
        loaded: list[tuple[SourcePromotionInput, bytes]] = []
        total = 0
        for item in sorted(admitted, key=lambda value: value.target_path):
            content = _read_regular_file(
                source_roots[item.source_root_label],
                _portable_path(item.source_path, label="promotion source path"),
                max_bytes=self._max_file_bytes,
            )
            if _bytes_identity(content) != item.expected_content_identity:
                raise SourcePromotionError(
                    "promotion.input_identity_mismatch",
                    f"promotion input {item.source_root_label}:{item.source_path} "
                    "does not match its content pin",
                )
            total += len(content)
            if total > self._max_total_bytes:
                raise SourcePromotionError(
                    "promotion.limit_exceeded",
                    f"promotion input bytes exceed {self._max_total_bytes}",
                )
            loaded.append((item, content))
        entries = tuple(
            GenerationInputAuditEntry(
                kind=item.kind,
                source_root_label=item.source_root_label,
                source_path=item.source_path,
                target_path=item.target_path,
                content_identity=item.expected_content_identity,
                byte_count=len(content),
            )
            for item, content in loaded
        )
        return GenerationInputAudit(entries, _materialized_tree_identity(entries))


def paths_overlap(left: Path, right: Path) -> bool:
    """Return whether two paths alias, contain, or are contained portably."""

    left_key = _filesystem_key(Path(left).resolve(strict=False))
    right_key = _filesystem_key(Path(right).resolve(strict=False))
    return _is_prefix(left_key, right_key) or _is_prefix(right_key, left_key)


def _filesystem_key(path: Path) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def _is_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and left == right[: len(left)]


def _validate_root_label(value: str) -> None:
    if not isinstance(value, str) or _ROOT_LABEL.fullmatch(value) is None:
        raise SourcePromotionError(
            "promotion.invalid_root_label",
            "source_root_label must be a lower-case portable token",
        )


def _portable_path(value: str, *, label: str) -> PurePosixPath:
    try:
        return canonical_relative_posix_path(value, label=label)
    except (TypeError, ValueError) as error:
        raise SourcePromotionError("promotion.invalid_path", str(error)) from error


def _content_identity(value: str, label: str) -> ContentIdentity:
    try:
        return ContentIdentity.parse_uri(value)
    except (TypeError, ValueError) as error:
        raise SourcePromotionError(
            "promotion.invalid_identity", f"{label} must be an exact sha256 identity"
        ) from error


def _bytes_identity(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _require_direct_directory(path: Path, *, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SourcePromotionError(
            "promotion.invalid_root", f"{label} must be an existing directory: {path}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SourcePromotionError(
            "promotion.invalid_root", f"{label} must be a direct, non-symlink directory"
        )
    return path.resolve(strict=True)


def _validated_source_roots(
    inputs: tuple[SourcePromotionInput, ...],
) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for item in inputs:
        resolved = _require_direct_directory(
            item.source_root, label=f"source root {item.source_root_label!r}"
        )
        previous = roots.get(item.source_root_label)
        if previous is not None and previous != resolved:
            raise SourcePromotionError(
                "promotion.root_label_collision",
                f"source root label {item.source_root_label!r} names multiple roots",
            )
        roots[item.source_root_label] = resolved
    return roots


def _validate_input_paths(
    inputs: tuple[SourcePromotionInput, ...], roots: Mapping[str, Path]
) -> None:
    try:
        canonical_relative_posix_paths(
            (item.target_path for item in inputs), label="promotion target path"
        )
    except (TypeError, ValueError) as error:
        raise SourcePromotionError("promotion.duplicate_target", str(error)) from error
    source_keys: set[tuple[str, tuple[str, ...]]] = set()
    resolved_sources: set[tuple[str, ...]] = set()
    for item in inputs:
        relative = _portable_path(item.source_path, label="promotion source path")
        key = (
            item.source_root_label,
            tuple(part.casefold() for part in relative.parts),
        )
        resolved_key = _filesystem_key(
            roots[item.source_root_label].joinpath(*relative.parts)
        )
        if key in source_keys or resolved_key in resolved_sources:
            raise SourcePromotionError(
                "promotion.duplicate_input",
                "one physical source file may appear only once in an allowlist",
            )
        source_keys.add(key)
        resolved_sources.add(resolved_key)


def _read_regular_file(root: Path, relative: PurePosixPath, *, max_bytes: int) -> bytes:
    if os.name == "nt":
        return _read_regular_file_windows(root, relative, max_bytes=max_bytes)
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise SourcePromotionError(
            "promotion.secure_open_unsupported",
            "host cannot provide descriptor-relative no-follow reads",
        )
    descriptors: list[int] = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors.append(os.open(root, directory_flags))
        for part in relative.parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=descriptors[-1],
        )
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SourcePromotionError(
                "promotion.input_not_regular",
                f"promotion input is not a regular file: {relative.as_posix()}",
            )
        content = bytearray()
        while len(content) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > max_bytes:
            raise SourcePromotionError(
                "promotion.limit_exceeded",
                f"promotion input exceeds {max_bytes} bytes: {relative.as_posix()}",
            )
        if _stat_signature(metadata) != _stat_signature(os.fstat(descriptor)):
            raise SourcePromotionError(
                "promotion.input_changed",
                f"promotion input changed while reading: {relative.as_posix()}",
            )
        return bytes(content)
    except SourcePromotionError:
        raise
    except OSError as error:
        candidate = Path(root)
        symlink_observed = False
        for part in relative.parts:
            candidate /= part
            try:
                symlink_observed = candidate.is_symlink()
            except OSError:
                break
            if symlink_observed:
                break
        code = (
            "promotion.source_symlink"
            if error.errno == ELOOP or symlink_observed
            else "promotion.input_unavailable"
        )
        raise SourcePromotionError(
            code, f"promotion input could not be opened safely: {relative.as_posix()}"
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_regular_file_windows(
    root: Path, relative: PurePosixPath, *, max_bytes: int
) -> bytes:
    """Read beneath one held root while detecting native path-identity drift."""

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    read_file = kernel.ReadFile
    read_file.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    read_file.restype = wintypes.BOOL
    get_attribute_info = kernel.GetFileInformationByHandleEx
    get_attribute_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    get_attribute_info.restype = wintypes.BOOL
    invalid = wintypes.HANDLE(-1).value
    file_attribute_reparse_point = 0x400
    file_attribute_directory = 0x10
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    open_existing = 3
    generic_read = 0x80000000
    file_share_read = 0x1

    class AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", FileTime),
            ("last_access_time", FileTime),
            ("last_write_time", FileTime),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    get_file_information = kernel.GetFileInformationByHandle
    get_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    get_file_information.restype = wintypes.BOOL

    def inspect_handle(
        handle: int, *, directory: bool
    ) -> tuple[int, int, int, int, int, int, int]:
        attribute_info = AttributeTagInfo()
        if not get_attribute_info(
            handle,
            9,
            ctypes.byref(attribute_info),
            ctypes.sizeof(attribute_info),
        ):
            raise OSError(ctypes.get_last_error(), "handle inspection failed")
        if attribute_info.attributes & file_attribute_reparse_point:
            raise SourcePromotionError(
                "promotion.source_symlink",
                f"promotion input traverses a reparse point: {relative.as_posix()}",
            )
        if bool(attribute_info.attributes & file_attribute_directory) != directory:
            raise SourcePromotionError(
                "promotion.input_not_regular",
                f"promotion input has an invalid file type: {relative.as_posix()}",
            )
        file_info = ByHandleFileInformation()
        if not get_file_information(handle, ctypes.byref(file_info)):
            raise OSError(ctypes.get_last_error(), "file identity inspection failed")
        return (
            int(file_info.volume_serial_number),
            int(file_info.file_index_high),
            int(file_info.file_index_low),
            int(file_info.file_size_high),
            int(file_info.file_size_low),
            int(file_info.last_write_time.high),
            int(file_info.last_write_time.low),
        )

    handles: list[int] = []
    snapshots: list[tuple[int, int, int, int, int, int, int]] = []
    expected_directories: list[bool] = []
    try:
        paths = [Path(root)]
        current = paths[0]
        for part in relative.parts:
            current /= part
            paths.append(current)
        for index, current in enumerate(paths):
            directory = index < len(paths) - 1
            flags = file_flag_open_reparse_point
            if directory:
                flags |= file_flag_backup_semantics
            handle = create_file(
                str(current),
                generic_read,
                file_share_read,
                None,
                open_existing,
                flags,
                None,
            )
            if handle == invalid:
                raise OSError(ctypes.get_last_error(), "CreateFileW failed")
            handles.append(handle)
            expected_directories.append(directory)
            snapshots.append(inspect_handle(handle, directory=directory))
        content = bytearray()
        while len(content) <= max_bytes:
            size = min(64 * 1024, max_bytes + 1 - len(content))
            buffer = ctypes.create_string_buffer(size)
            read = wintypes.DWORD()
            if not read_file(handles[-1], buffer, size, ctypes.byref(read), None):
                raise OSError(ctypes.get_last_error(), "ReadFile failed")
            if read.value == 0:
                break
            content.extend(buffer.raw[: read.value])
        if len(content) > max_bytes:
            raise SourcePromotionError(
                "promotion.limit_exceeded",
                f"promotion input exceeds {max_bytes} bytes: {relative.as_posix()}",
            )
        for handle, directory, snapshot in zip(
            handles, expected_directories, snapshots, strict=True
        ):
            if inspect_handle(handle, directory=directory) != snapshot:
                raise SourcePromotionError(
                    "promotion.input_changed",
                    "promotion root, ancestor, or input changed while reading: "
                    f"{relative.as_posix()}",
                )
        return bytes(content)
    except SourcePromotionError:
        raise
    except OSError as error:
        raise SourcePromotionError(
            "promotion.input_unavailable",
            f"promotion input could not be opened safely: {relative.as_posix()}",
        ) from error
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _materialized_tree_identity(
    entries: tuple[GenerationInputAuditEntry, ...],
) -> str:
    return canonical_identity(
        {
            "schema": MATERIALIZED_PROMOTION_TREE_SCHEMA,
            "files": [
                {
                    "path": entry.target_path,
                    "content_identity": entry.content_identity,
                    "byte_count": entry.byte_count,
                }
                for entry in entries
            ],
        }
    ).uri


def generation_input_subset_identity(
    audit: GenerationInputAudit,
    *,
    kinds: frozenset[PromotionInputKind],
    target_prefixes: tuple[str, ...] = (),
) -> ContentIdentity:
    """Identify an exact, non-empty typed subset of one verified promotion audit."""

    if (
        not isinstance(audit, GenerationInputAudit)
        or not kinds
        or any(not isinstance(kind, PromotionInputKind) for kind in kinds)
    ):
        raise SourcePromotionError(
            "promotion.qualification_closure_invalid",
            "qualification closure requires typed audit kinds",
        )
    prefixes = tuple(
        _portable_path(value, label="qualification audit prefix").as_posix() + "/"
        for value in target_prefixes
    )
    entries = tuple(
        entry
        for entry in audit.entries
        if entry.kind in kinds
        and (
            not prefixes
            or any(entry.target_path.startswith(prefix) for prefix in prefixes)
        )
    )
    if not entries:
        raise SourcePromotionError(
            "promotion.qualification_closure_invalid",
            "qualification closure did not resolve any audited files",
        )
    return canonical_identity(
        {
            "schema": "literate-ai/generation-input-subset@1",
            "entries": [entry.to_dict() for entry in entries],
        }
    )


def _validate_audit_paths(entries: tuple[GenerationInputAuditEntry, ...]) -> None:
    try:
        canonical_relative_posix_paths(
            (item.target_path for item in entries), label="audit target path"
        )
    except (TypeError, ValueError) as error:
        raise SourcePromotionError("promotion.invalid_audit", str(error)) from error
    sources: set[tuple[str, tuple[str, ...]]] = set()
    for entry in entries:
        path = _portable_path(entry.source_path, label="audit source path")
        key = (entry.source_root_label, tuple(part.casefold() for part in path.parts))
        if key in sources:
            raise SourcePromotionError(
                "promotion.invalid_audit", "audit repeats a logical source file"
            )
        sources.add(key)


def _exact_mapping(
    value: Mapping[str, Any], *, path: str, fields: set[str], schema: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourcePromotionError(
            "promotion.invalid_audit", f"{path} must be an object"
        )
    if set(value) != fields:
        raise SourcePromotionError(
            "promotion.invalid_audit", f"{path} has missing or unknown fields"
        )
    if value["schema"] != schema:
        raise SourcePromotionError(
            "promotion.invalid_audit", f"{path}.schema is not supported"
        )
    return value


def _string(value: Any, path: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SourcePromotionError(
            "promotion.invalid_audit", f"{path}.{field} must be a non-empty string"
        )
    return value


def verify_source_promotion_evidence(
    project_root: Path, projection: ComponentAuthorityProjection
) -> VerifiedSourcePromotionEvidence:
    """Resolve the typed promotion record and every audit bound by a projection."""

    if not isinstance(projection, ComponentAuthorityProjection):
        raise SourcePromotionError(
            "promotion.lifecycle_evidence_invalid",
            "promotion evidence requires a v2 Component authority projection",
        )
    promotion_identity = projection.provenance_reference_identity
    record_path = (
        PurePosixPath("provenance/source-promotion")
        / (promotion_identity.digest)
        / "reference.json"
    )
    content = _read_regular_file(
        _require_direct_directory(Path(project_root), label="authority project"),
        record_path,
        max_bytes=16 * 1024 * 1024,
    )
    try:
        record = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourcePromotionError(
            "promotion.evidence_invalid", "promotion record must be UTF-8 JSON"
        ) from error
    common_fields = {
        "schema",
        "source_snapshot_identity",
        "specification_set_identity",
        "review_identity",
        "component_graph_identity",
        "translation_identity",
        "generation_input_audit_references",
    }
    schema = record.get("schema") if isinstance(record, dict) else None
    expected_fields = (
        common_fields | {"inverse_evidence_reference"}
        if schema == SOURCE_PROMOTION_PROVENANCE_SCHEMA
        else common_fields
    )
    if (
        not isinstance(record, dict)
        or set(record) != expected_fields
        or schema
        not in {
            SOURCE_PROMOTION_PROVENANCE_SCHEMA,
            PREVIOUS_SOURCE_PROMOTION_PROVENANCE_SCHEMA,
        }
        or canonical_identity(record) != promotion_identity
    ):
        raise SourcePromotionError(
            "promotion.evidence_invalid",
            "promotion record schema, fields, or content identity is invalid",
        )
    for field in (
        "source_snapshot_identity",
        "specification_set_identity",
        "review_identity",
        "component_graph_identity",
    ):
        _content_identity(record[field], f"promotion record {field}")
    translation: Mapping[str, Any] | None = None
    translation_identity: ContentIdentity | None = None
    if record["translation_identity"] is not None:
        translation_identity = _content_identity(
            record["translation_identity"], "translation identity"
        )
        translation_path = (
            PurePosixPath("provenance/source-promotion")
            / promotion_identity.digest
            / "source-translation.json"
        )
        translation_content = _read_regular_file(
            Path(project_root), translation_path, max_bytes=16 * 1024 * 1024
        )
        try:
            translation_value = json.loads(translation_content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourcePromotionError(
                "promotion.evidence_invalid", "source translation is invalid JSON"
            ) from error
        if not isinstance(translation_value, Mapping) or (
            canonical_identity(translation_value).uri != record["translation_identity"]
        ):
            raise SourcePromotionError(
                "promotion.evidence_mismatch",
                "source translation does not match its content identity",
            )
        translation = translation_value
    inverse_evidence_custody: Mapping[str, Any] | None = None
    inverse_evidence_custody_identity: ContentIdentity | None = None
    if schema == SOURCE_PROMOTION_PROVENANCE_SCHEMA:
        reference = record["inverse_evidence_reference"]
        if reference is not None:
            if (
                not isinstance(reference, dict)
                or set(reference) != {"kind", "identity", "path"}
                or reference.get("kind") != "inverse-evidence-custody"
                or reference.get("path") != "inverse-evidence.json"
            ):
                raise SourcePromotionError(
                    "promotion.evidence_invalid",
                    "inverse evidence reference is not exact and typed",
                )
            inverse_evidence_custody_identity = _content_identity(
                reference.get("identity"), "inverse evidence custody identity"
            )
            inverse_content = _read_regular_file(
                Path(project_root),
                PurePosixPath("provenance/source-promotion")
                / promotion_identity.digest
                / "inverse-evidence.json",
                max_bytes=64 * 1024 * 1024,
            )
            try:
                inverse_value = json.loads(inverse_content)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SourcePromotionError(
                    "promotion.evidence_invalid",
                    "inverse evidence custody is invalid JSON",
                ) from error
            if not isinstance(inverse_value, Mapping) or (
                canonical_identity(inverse_value) != inverse_evidence_custody_identity
            ):
                raise SourcePromotionError(
                    "promotion.evidence_mismatch",
                    "inverse evidence custody does not match its content identity",
                )
            inverse_evidence_custody = inverse_value
    if record["source_snapshot_identity"] != projection.source_snapshot_identity.uri:
        raise SourcePromotionError(
            "promotion.evidence_mismatch", "promotion source snapshot does not match"
        )
    # The promotion record binds the reviewed graph-wide inverse SpecificationSet.
    # A v2 Component authority projection binds its node-local exact specification
    # set from the Component lock. Nested Components therefore must not be flattened
    # back into the graph-wide identity here; locked qualification checks the exact
    # node-local identity against current authority before admitting promotion.
    references = record["generation_input_audit_references"]
    if not isinstance(references, list) or not references:
        raise SourcePromotionError(
            "promotion.evidence_invalid",
            "promotion record must resolve at least one audit",
        )
    parsed_audits: dict[str, GenerationInputAudit] = {}
    canonical_references: list[dict[str, str]] = []
    for index, reference in enumerate(references):
        if (
            not isinstance(reference, dict)
            or set(reference) != {"kind", "identity", "path"}
            or reference.get("kind") != "generation-input-audit"
        ):
            raise SourcePromotionError(
                "promotion.evidence_invalid",
                f"promotion audit reference {index} is not typed",
            )
        identity = _content_identity(reference.get("identity"), "audit identity")
        relative = _portable_path(reference.get("path"), label="audit evidence path")
        expected = PurePosixPath("generation-input-audits") / f"{identity.digest}.json"
        if relative != expected:
            raise SourcePromotionError(
                "promotion.evidence_invalid",
                "audit reference is not at its content-addressed path",
            )
        project_relative = (
            PurePosixPath("provenance/source-promotion")
            / promotion_identity.digest
            / relative
        )
        audit_content = _read_regular_file(
            Path(project_root), project_relative, max_bytes=16 * 1024 * 1024
        )
        try:
            audit = GenerationInputAudit.from_dict(json.loads(audit_content))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourcePromotionError(
                "promotion.evidence_invalid", "generation input audit is invalid JSON"
            ) from error
        if audit.identity != identity.uri:
            raise SourcePromotionError(
                "promotion.evidence_mismatch",
                "generation input audit does not match its content identity",
            )
        audit.verify(Path(project_root))
        parsed_audits[identity.uri] = audit
        canonical_references.append(reference)
    if canonical_references != sorted(
        canonical_references, key=lambda item: (item["identity"], item["path"])
    ) or len(parsed_audits) != len(references):
        raise SourcePromotionError(
            "promotion.evidence_invalid",
            "audit references must be canonical and unique",
        )
    closure = projection.generation_closure
    audits = tuple(parsed_audits[identity] for identity in sorted(parsed_audits))
    if closure is None:
        return VerifiedSourcePromotionEvidence(
            audits,
            authority_projection=projection,
            translation=translation,
            translation_identity=translation_identity,
            inverse_evidence_custody=inverse_evidence_custody,
            inverse_evidence_custody_identity=inverse_evidence_custody_identity,
        )
    from .qualification_lifecycle import QualificationLifecycleResult

    qualification_relative = PurePosixPath("provenance/qualification") / (
        f"{closure.qualification_evidence_identity.digest}.json"
    )
    qualification_content = _read_regular_file(
        Path(project_root), qualification_relative, max_bytes=16 * 1024 * 1024
    )
    try:
        qualification = QualificationLifecycleResult.from_dict(
            json.loads(qualification_content)
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceToSpecificationError,
    ) as error:
        raise SourcePromotionError(
            "promotion.qualification_evidence_invalid",
            "lifecycle qualification evidence is invalid",
        ) from error
    if qualification.identity != closure.qualification_evidence_identity:
        raise SourcePromotionError(
            "promotion.qualification_evidence_mismatch",
            "lifecycle qualification evidence does not match its content identity",
        )
    expected_specification = projection.specification_set_identity
    if any(
        run.source_snapshot_identity != projection.source_snapshot_identity
        or run.component_lock_identity != projection.target_lock_identity
        or run.specification_set_identity != expected_specification
        or run.generation_input_audit_identity != closure.promotion_input_audit_identity
        or run.promotion_tree_identity != closure.promotion_tree_identity
        for run in qualification.runs
    ) or (
        projection.verifier_identity != qualification.case_map.verifier_identity
        or projection.policy_identity != qualification.runs[0].lifecycle_policy_identity
    ):
        raise SourcePromotionError(
            "promotion.qualification_evidence_mismatch",
            "lifecycle qualification evidence differs from retained authority",
        )
    return VerifiedSourcePromotionEvidence(
        audits,
        authority_projection=projection,
        target_lock_identity=projection.target_lock_identity,
        generation_closure=closure,
        verifier_identity=qualification.case_map.verifier_identity,
        policy_identity=qualification.runs[0].lifecycle_policy_identity,
        qualification_lifecycle_result=qualification,
        translation=translation,
        translation_identity=translation_identity,
        inverse_evidence_custody=inverse_evidence_custody,
        inverse_evidence_custody_identity=inverse_evidence_custody_identity,
    )


__all__ = [
    "GENERATION_INPUT_AUDIT_ENTRY_SCHEMA",
    "GENERATION_INPUT_AUDIT_SCHEMA",
    "MATERIALIZED_PROMOTION_TREE_SCHEMA",
    "SOURCE_PROMOTION_PROVENANCE_SCHEMA",
    "GenerationInputAudit",
    "GenerationInputAuditEntry",
    "generation_input_subset_identity",
    "PromotionInputKind",
    "SourcePromotionError",
    "SourcePromotionInput",
    "SourcePromotionMaterializationResult",
    "SourcePromotionMaterializer",
    "VerifiedSourcePromotionEvidence",
    "verify_source_promotion_evidence",
]
