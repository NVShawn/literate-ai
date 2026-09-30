"""Safe project metadata storage and evidence checks for conversion authority."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath

from literate_ai._cache_lock import (
    CacheLockError,
    _is_windows,
    _windows_identity_matches,
    exclusive_cache_lock,
)
from literate_ai._filesystem import stat_is_link_or_reparse
from literate_ai.adapters.authority import (
    AuthorityProjectionStoreError,
    FileAuthorityProjectionStore,
)
from literate_ai.adapters.component_lock_application import (
    ProjectComponentLockSetError,
    current_rebuild_project_authority_identity,
)
from literate_ai.adapters.project_validation import (
    ProjectValidationError,
    validated_project_authority_identity,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.authority import ComponentAuthorityState
from literate_ai.contracts.operator_adoption import (
    ConversionAuthorityStage,
    ConversionAuthorityState,
    advance_conversion_authority,
)
from literate_ai.projects import LoadedProject, ProjectError, load_project
from literate_ai.source_to_specification.promotion_materialization import (
    SourcePromotionError,
    verify_source_promotion_evidence,
)
from literate_ai.test_receipts import (
    inspect_project_test_receipt,
    require_current_project_test_receipt,
)

CONVERSION_AUTHORITY_FILE = ".literate/conversion-authority.json"
NATIVE_PROJECTS_FILE = ".literate/native-projects.json"
NATIVE_PROJECTS_SCHEMA = "literate-ai/adopted-native-projects@1"
_CONVERSION_AUTHORITY_LOCK = ".literate/conversion-authority.lock"
_MAXIMUM_STATE_BYTES = 256 * 1024


class ConversionAuthorityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def require_native_project_path(project: LoadedProject, path: Path) -> None:
    """Reject indirect metadata custody, including dangling links and junctions."""

    try:
        relative = path.relative_to(project.root)
        if (
            ".." in relative.parts
            or path.resolve().is_relative_to(project.root) is False
        ):
            raise ValueError
        current = project.root
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                continue
            if stat_is_link_or_reparse(metadata):
                raise ValueError
    except (OSError, ValueError, RuntimeError) as exc:
        raise ConversionAuthorityError(
            "conversion_authority.native_registry_invalid",
            "native project metadata must remain directly inside the adopted root",
        ) from exc


def _native_project_roots(project: LoadedProject) -> tuple[Path, ...]:
    """Return exact registered source-free child projects for an adopted root."""

    registry = project.root / NATIVE_PROJECTS_FILE
    require_native_project_path(project, registry)
    if not registry.exists():
        return ()
    if registry.is_symlink() or not registry.is_file():
        raise ConversionAuthorityError(
            "conversion_authority.native_registry_invalid",
            "native project registry is missing, unsafe, or invalid",
        )
    try:
        with registry.open("rb") as stream:
            content = stream.read(_MAXIMUM_STATE_BYTES + 1)
        if len(content) > _MAXIMUM_STATE_BYTES:
            raise ValueError
        value = json.loads(content)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "projects"}
            or value["schema"] != NATIVE_PROJECTS_SCHEMA
            or not isinstance(value["projects"], list)
        ):
            raise ValueError
        roots: list[Path] = []
        seen_paths: set[str] = set()
        seen_ids: set[str] = set()
        for entry in value["projects"]:
            if not isinstance(entry, dict) or set(entry) != {
                "path",
                "project_id",
                "project_definition_identity",
            }:
                raise ValueError
            relative = entry["path"]
            relative_path = (
                PurePosixPath(relative) if isinstance(relative, str) else None
            )
            if (
                relative_path is None
                or relative != relative_path.as_posix()
                or "\\" in relative
                or relative_path.parent != PurePosixPath(".literate/native-projects")
                or relative_path.name in {"", ".", ".."}
                or relative in seen_paths
            ):
                raise ValueError
            child_root = project.root.joinpath(*Path(relative).parts)
            require_native_project_path(project, child_root)
            child = load_project(child_root)
            if child.root != child_root.resolve(strict=True):
                raise ValueError
            definition_value = json.loads(
                (child.root / "literate.project.json").read_bytes()
            )
            project_id = entry["project_id"]
            if (
                not isinstance(project_id, str)
                or child.definition.project_id != project_id
                or project_id in seen_ids
                or canonical_identity(definition_value).uri
                != entry["project_definition_identity"]
            ):
                raise ValueError
            seen_paths.add(relative)
            seen_ids.add(project_id)
            roots.append(child.root)
        return tuple(roots)
    except ConversionAuthorityError:
        raise
    except (
        OSError,
        ProjectError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        raise ConversionAuthorityError(
            "conversion_authority.native_registry_invalid",
            "native project registry is missing, unsafe, or invalid",
        ) from exc


def register_native_project(project: LoadedProject, child_root: Path) -> None:
    """Atomically register one exact promoted child with an adopted project."""

    registry = project.root / NATIVE_PROJECTS_FILE
    require_native_project_path(project, registry)
    try:
        with exclusive_cache_lock(registry.with_suffix(".lock")):
            _register_native_project(project, child_root)
    except CacheLockError as exc:
        raise ConversionAuthorityError(
            "conversion_authority.native_registry_locked",
            "native project registration is already in progress or unsafe",
        ) from exc


def _register_native_project(project: LoadedProject, child_root: Path) -> None:
    require_native_project_path(project, child_root)
    require_native_project_path(project, project.root / NATIVE_PROJECTS_FILE)
    resolved = child_root.resolve(strict=True)
    expected_parent = project.root / ".literate" / "native-projects"
    if expected_parent.is_symlink() or resolved.parent != expected_parent.resolve(
        strict=True
    ):
        raise ConversionAuthorityError(
            "conversion_authority.native_project_location_invalid",
            "integrated native project must be a direct child of "
            ".literate/native-projects",
        )
    child = load_project(resolved)
    definition_value = json.loads((resolved / "literate.project.json").read_bytes())
    entry = {
        "path": resolved.relative_to(project.root).as_posix(),
        "project_id": child.definition.project_id,
        "project_definition_identity": canonical_identity(definition_value).uri,
    }
    registry = project.root / NATIVE_PROJECTS_FILE
    current_entries: list[dict[str, str]] = []
    if registry.exists():
        _native_project_roots(project)
        current = json.loads(registry.read_bytes())
        current_entries = list(current["projects"])
    if any(
        item["path"] == entry["path"] or item["project_id"] == entry["project_id"]
        for item in current_entries
    ):
        raise ConversionAuthorityError(
            "conversion_authority.native_project_collision",
            "native project path or project identifier is already registered",
        )
    document = {
        "schema": NATIVE_PROJECTS_SCHEMA,
        "projects": sorted((*current_entries, entry), key=lambda item: item["path"]),
    }
    content = canonical_json_bytes(document) + b"\n"
    if len(content) > _MAXIMUM_STATE_BYTES:
        raise ConversionAuthorityError(
            "conversion_authority.native_registry_invalid",
            "native project registry exceeds the supported metadata size",
        )
    registry.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".native-projects.", dir=registry.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, registry)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _named_file_matches(path: Path, descriptor: int, opened: os.stat_result) -> bool:
    named = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(named.st_mode):
        return False
    if _is_windows() and opened.st_ino == 0:
        return _windows_identity_matches(path, descriptor)
    return (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino)


class FilesystemConversionAuthorityStore:
    """Read or compare-and-swap one typed conversion-stage projection."""

    def __init__(self, project_root: Path) -> None:
        self.root = Path(project_root).resolve(strict=True)
        self.path = self.root.joinpath(*Path(CONVERSION_AUTHORITY_FILE).parts)
        self.lock_path = self.root.joinpath(*Path(_CONVERSION_AUTHORITY_LOCK).parts)

    def load_optional(self) -> ConversionAuthorityState | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        return self._read()

    def initialize(
        self, *, project_id: str, evidence_identity: ContentIdentity
    ) -> ConversionAuthorityState:
        state = ConversionAuthorityState(
            project_id=project_id,
            stage=ConversionAuthorityStage.WRAPPED,
            evidence_identities=(evidence_identity,),
        )
        self._replace(state, expected=None)
        return state

    def advance(
        self,
        stage: ConversionAuthorityStage,
        *,
        evidence_identities: tuple[ContentIdentity, ...],
    ) -> ConversionAuthorityState:
        current = self._read()
        try:
            replacement = advance_conversion_authority(
                current, stage, evidence_identities=evidence_identities
            )
        except (TypeError, ValueError) as exc:
            raise ConversionAuthorityError(
                "conversion_authority.transition_invalid", str(exc)
            ) from exc
        self._replace(replacement, expected=current.identity)
        return replacement

    def _read(self) -> ConversionAuthorityState:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size <= 0
                or before.st_size > _MAXIMUM_STATE_BYTES
            ):
                raise OSError
            content = os.read(descriptor, _MAXIMUM_STATE_BYTES + 1)
            after = os.fstat(descriptor)
            if (
                len(content) != before.st_size
                or len(content) > _MAXIMUM_STATE_BYTES
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or not _named_file_matches(self.path, descriptor, before)
            ):
                raise OSError
            value = json.loads(content.decode("utf-8"))
            state = ConversionAuthorityState.from_dict(value)
            if content != canonical_json_bytes(state.to_dict()) + b"\n":
                raise ValueError("conversion authority is not canonical JSON")
            return state
        except ConversionAuthorityError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ConversionAuthorityError(
                "conversion_authority.state_invalid",
                "conversion authority must be one stable canonical "
                "project-metadata file",
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _replace(
        self,
        state: ConversionAuthorityState,
        *,
        expected: ContentIdentity | None,
    ) -> None:
        metadata = self.path.parent
        if metadata.exists() and (metadata.is_symlink() or not metadata.is_dir()):
            raise ConversionAuthorityError(
                "conversion_authority.path_unsafe",
                "conversion authority metadata root must be a direct directory",
            )
        metadata.mkdir(parents=True, exist_ok=True)
        try:
            with exclusive_cache_lock(self.lock_path):
                current = self.load_optional()
                current_identity = None if current is None else current.identity
                if current_identity != expected:
                    raise ConversionAuthorityError(
                        "conversion_authority.concurrent_change",
                        "conversion authority changed before publication",
                    )
                self._atomic_write(
                    self.path, canonical_json_bytes(state.to_dict()) + b"\n"
                )
        except ConversionAuthorityError:
            raise
        except (CacheLockError, OSError) as exc:
            raise ConversionAuthorityError(
                "conversion_authority.write_failed",
                "conversion authority could not be published atomically",
            ) from exc

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _current_receipt_identity(project: LoadedProject) -> ContentIdentity:
    try:
        base = validated_project_authority_identity(
            project.root, synchronize_source_intelligence=False
        )
        try:
            report = require_current_project_test_receipt(
                project, project_revision_identity=base
            )
        except ProjectError as exc:
            if exc.code != "project.test_receipt_stale":
                raise
            locked = current_rebuild_project_authority_identity(project, base)
            report = require_current_project_test_receipt(
                project, project_revision_identity=locked
            )
        identity = report.get("identity")
        if not isinstance(identity, str):
            inspection = inspect_project_test_receipt(
                project, project_revision_identity=base
            )
            identity = inspection.get("identity")
        if not isinstance(identity, str):
            raise ConversionAuthorityError(
                "conversion_authority.retained_evidence_missing",
                "retained promotion requires a current project test receipt identity",
            )
        return ContentIdentity.parse_uri(identity)
    except ConversionAuthorityError:
        raise
    except (
        ProjectComponentLockSetError,
        ProjectError,
        ProjectValidationError,
        ValueError,
    ) as exc:
        raise ConversionAuthorityError(
            "conversion_authority.retained_evidence_missing",
            "retained promotion requires a current retained-harness receipt",
        ) from exc


def _component_projection_identities(
    project: LoadedProject, *, qualified: bool
) -> tuple[ContentIdentity, ...]:
    try:
        projections = []
        seen_coordinates: set[str] = set()
        for project_root in (project.root, *_native_project_roots(project)):
            store = FileAuthorityProjectionStore(project_root)
            for coordinate in store.coordinates():
                if coordinate in seen_coordinates:
                    raise ConversionAuthorityError(
                        "conversion_authority.component_coordinate_collision",
                        "Component coordinate occurs in more than one authority "
                        "project",
                    )
                seen_coordinates.add(coordinate)
                projections.append((project_root, store.current(coordinate)))
        if not projections:
            raise ConversionAuthorityError(
                "conversion_authority.component_evidence_missing",
                "conversion authority promotion requires Component projections",
            )
        required = (
            ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE
            if qualified
            else ComponentAuthorityState.DERIVED_SOURCE_RETAINED
        )
        if any(item.state is not required for _root, item in projections):
            raise ConversionAuthorityError(
                "conversion_authority.component_stage_incomplete",
                f"every Component must be {required.value} before promotion",
            )
        if qualified:
            for projection_root, projection in projections:
                verify_source_promotion_evidence(projection_root, projection)
        return tuple(
            sorted(
                (item.identity for _root, item in projections),
                key=lambda item: item.uri,
            )
        )
    except ConversionAuthorityError:
        raise
    except (AuthorityProjectionStoreError, SourcePromotionError) as exc:
        raise ConversionAuthorityError(
            "conversion_authority.component_evidence_invalid",
            "Component authority evidence is missing, stale, or invalid",
        ) from exc


def evidence_for_conversion_stage(
    project: LoadedProject, stage: ConversionAuthorityStage
) -> tuple[ContentIdentity, ...]:
    """Verify and return the exact evidence required by one explicit advance."""

    # Refresh preserves recorded stages and conversion evidence as history. No
    # later promotion may use that history in place of a freshly earned receipt.
    if any((project.root / ".literate/retained-scope-refresh").glob("*.json")) or any(
        (project.root / ".literate/retained-harness-readmission").glob("*.json")
    ):
        _current_receipt_identity(project)
    if stage is ConversionAuthorityStage.RETAINED:
        return (_current_receipt_identity(project),)
    if stage is ConversionAuthorityStage.DRAFTED:
        return _component_projection_identities(project, qualified=False)
    if stage is ConversionAuthorityStage.QUALIFIED:
        return _component_projection_identities(project, qualified=True)
    raise ConversionAuthorityError(
        "conversion_authority.transition_invalid",
        "wrapped is established only by successful repository conversion",
    )


def require_current_qualified_conversion_authority(
    project: LoadedProject, state: ConversionAuthorityState
) -> tuple[ContentIdentity, ...]:
    """Require a qualified claim to still name every current qualified projection."""

    if state.project_id != project.definition.project_id:
        raise ConversionAuthorityError(
            "conversion_authority.project_mismatch",
            "conversion authority identifies another project",
        )
    if state.stage is not ConversionAuthorityStage.QUALIFIED:
        raise ConversionAuthorityError(
            "conversion_authority.not_qualified",
            "adopted project remains original-source authoritative",
        )
    current = evidence_for_conversion_stage(project, ConversionAuthorityStage.QUALIFIED)
    if current != state.evidence_identities:
        raise ConversionAuthorityError(
            "conversion_authority.qualification_stale",
            "qualified conversion authority no longer names the current Component "
            "qualification projections",
        )
    return current


__all__ = [
    "CONVERSION_AUTHORITY_FILE",
    "NATIVE_PROJECTS_FILE",
    "ConversionAuthorityError",
    "FilesystemConversionAuthorityStore",
    "evidence_for_conversion_stage",
    "register_native_project",
    "require_current_qualified_conversion_authority",
]
