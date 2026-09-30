"""Read-only semantic shadow comparison for current OVA artifacts.

The shadow layer deliberately has no persistence API.  It projects legacy OVA
documents into neutral immutable values, then compares those values by stable
semantic digest.  Source byte digests and reader diagnostics remain available
as evidence without becoming the definition of semantic equality.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import (
    CompatibilityDiagnostic,
    DiagnosticSeverity,
    FrozenValue,
    LifecycleState,
    OvaCompatibilityResult,
    freeze,
    thaw,
)
from .reader import OvaCompatibilityReader


class ProjectionKind(StrEnum):
    COMPONENT = "component"
    SETTINGS = "settings"
    CACHE = "cache"
    SOURCE_PACKAGE = "package:source"
    OBJECT_PACKAGE = "package:object"
    PROVENANCE = "provenance"
    PUBLICATION_SETTINGS = "publication:settings"
    PUBLICATION_RECORD = "publication:record"


class ShadowStatus(StrEnum):
    EXACT = "exact"
    DRIFTED = "drifted"
    MISSING = "missing"
    INVALID = "invalid"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class ShadowDiagnostic:
    """A path-stable diagnostic suitable for an immutable report."""

    code: str
    message: str
    side: str
    source_artifact: str
    value_path: str = "$"
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    line: int | None = None
    column: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "side": self.side,
            "source_artifact": self.source_artifact,
            "value_path": self.value_path,
            "severity": self.severity.value,
            "line": self.line,
            "column": self.column,
        }


@dataclass(frozen=True, slots=True)
class NeutralProjection:
    """One legacy record represented without OVA runtime object coupling."""

    key: str
    kind: ProjectionKind
    identity: str
    source_artifact: str
    source_digest: str | None
    record_index: int | None
    lifecycle_state: LifecycleState
    value: Mapping[str, FrozenValue]
    semantic_digest: str
    diagnostics: tuple[ShadowDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        expected = _semantic_digest(thaw(self.value))
        if self.semantic_digest != expected:
            raise ValueError("neutral projection semantic digest does not match value")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind.value,
            "identity": self.identity,
            "source_artifact": self.source_artifact,
            "source_digest": self.source_digest,
            "record_index": self.record_index,
            "lifecycle_state": self.lifecycle_state.value,
            "semantic_digest": self.semantic_digest,
            "value": thaw(self.value),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class ShadowInventory:
    """An immutable inventory from one read-only tree sweep."""

    label: str
    projections: tuple[NeutralProjection, ...]
    digest: str

    def __post_init__(self) -> None:
        keys = [item.key for item in self.projections]
        if keys != sorted(keys):
            raise ValueError("shadow inventory projections must be key-sorted")
        if len(keys) != len(set(keys)):
            raise ValueError("shadow inventory projection keys must be unique")
        expected = _semantic_digest(
            {
                "label": self.label,
                "projections": [item.to_dict() for item in self.projections],
            }
        )
        if self.digest != expected:
            raise ValueError("shadow inventory digest does not match projections")

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "digest": self.digest,
            "projections": [item.to_dict() for item in self.projections],
        }


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    key: str
    kind: ProjectionKind
    identity: str
    status: ShadowStatus
    baseline_digest: str | None
    candidate_digest: str | None
    diagnostics: tuple[ShadowDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind.value,
            "identity": self.identity,
            "status": self.status.value,
            "baseline_digest": self.baseline_digest,
            "candidate_digest": self.candidate_digest,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class OvaShadowReport:
    """Complete immutable evidence for one semantic shadow comparison."""

    baseline: ShadowInventory
    candidate: ShadowInventory
    comparisons: tuple[ShadowComparison, ...]
    digest: str

    def __post_init__(self) -> None:
        keys = [item.key for item in self.comparisons]
        if keys != sorted(keys):
            raise ValueError("shadow comparisons must be key-sorted")
        expected = _semantic_digest(self._content_dict())
        if self.digest != expected:
            raise ValueError("shadow report digest does not match report content")

    @property
    def exact(self) -> bool:
        return all(item.status is ShadowStatus.EXACT for item in self.comparisons)

    @property
    def summary(self) -> Mapping[str, int]:
        counts = Counter(item.status.value for item in self.comparisons)
        result = freeze({status.value: counts[status.value] for status in ShadowStatus})
        assert isinstance(result, Mapping)
        return result  # type: ignore[return-value]

    def _content_dict(self) -> dict[str, Any]:
        return {
            "format": "literate-ai.ova-shadow-report.v1",
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "comparisons": [item.to_dict() for item in self.comparisons],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._content_dict(),
            "digest": self.digest,
            "exact": self.exact,
            "summary": thaw(self.summary),
        }


_Reader = Callable[[str | Path], OvaCompatibilityResult]


class OvaShadowComparator:
    """Build and compare side-effect-free projections of known OVA records."""

    _SKIPPED_DIRECTORIES = frozenset(
        {".git", ".hg", ".svn", ".venv", "__pycache__", "node_modules"}
    )

    def __init__(self, reader: OvaCompatibilityReader | None = None) -> None:
        self._reader = reader or OvaCompatibilityReader()

    def inventory(self, root: str | Path, *, label: str) -> ShadowInventory:
        configured = Path(root).expanduser()
        artifacts = self._discover(configured)
        projections: list[NeutralProjection] = []
        for path, source_artifact, reader in artifacts:
            result = reader(path)
            projections.extend(
                self._project_result(
                    result, source_artifact=source_artifact, side=label
                )
            )
        ordered = tuple(sorted(projections, key=lambda item: item.key))
        payload = {
            "label": label,
            "projections": [item.to_dict() for item in ordered],
        }
        return ShadowInventory(label, ordered, _semantic_digest(payload))

    def compare(
        self,
        baseline_root: str | Path,
        candidate_root: str | Path,
        *,
        baseline_label: str = "baseline",
        candidate_label: str = "candidate",
    ) -> OvaShadowReport:
        return self.compare_inventories(
            self.inventory(baseline_root, label=baseline_label),
            self.inventory(candidate_root, label=candidate_label),
        )

    @staticmethod
    def compare_inventories(
        baseline: ShadowInventory,
        candidate: ShadowInventory,
    ) -> OvaShadowReport:
        before = {item.key: item for item in baseline.projections}
        after = {item.key: item for item in candidate.projections}
        comparisons = tuple(
            _compare_projection(
                key,
                before.get(key),
                after.get(key),
                baseline_label=baseline.label,
                candidate_label=candidate.label,
            )
            for key in sorted(before.keys() | after.keys())
        )
        content = {
            "format": "literate-ai.ova-shadow-report.v1",
            "baseline": baseline.to_dict(),
            "candidate": candidate.to_dict(),
            "comparisons": [item.to_dict() for item in comparisons],
        }
        return OvaShadowReport(
            baseline=baseline,
            candidate=candidate,
            comparisons=comparisons,
            digest=_semantic_digest(content),
        )

    def _discover(self, root: Path) -> tuple[tuple[Path, str, _Reader], ...]:
        if root.is_file() or root.is_symlink():
            reader = self._reader_for(root.name)
            if reader is None:
                raise ValueError(f"unsupported OVA artifact name: {root.name}")
            return ((root, root.name, reader),)
        if not root.is_dir():
            raise ValueError(f"OVA shadow root is not a readable directory: {root}")

        artifacts: list[tuple[Path, str, _Reader]] = []
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = sorted(
                name for name in directories if name not in self._SKIPPED_DIRECTORIES
            )
            base = Path(directory)
            for name in sorted(filenames):
                reader = self._reader_for(name)
                if reader is None:
                    continue
                path = base / name
                relative = path.relative_to(root).as_posix()
                artifacts.append((path, relative, reader))
        return tuple(sorted(artifacts, key=lambda item: item[1]))

    def _reader_for(self, name: str) -> _Reader | None:
        if name == "ova.yaml":
            return self._reader.read_component
        if name == "models.yaml" or name == "runtime-settings.json":
            return self._reader.read_settings
        if name.endswith("cache.json"):
            return self._reader.read_cache
        if name == "source-package.json":
            return self._reader.read_source_package
        if name == "object-package.json":
            return self._reader.read_object_package
        if name == "generation.json":
            return self._reader.read_provenance
        if name == "publication-settings.json":
            return self._reader.read_publication_settings
        if name == "publication-records.json":
            return self._reader.read_publications
        return None

    @staticmethod
    def _project_result(
        result: OvaCompatibilityResult,
        *,
        source_artifact: str,
        side: str,
    ) -> tuple[NeutralProjection, ...]:
        diagnostics = tuple(
            _normalize_diagnostic(item, side=side, source_artifact=source_artifact)
            for item in result.diagnostics
        )
        records: tuple[Mapping[str, FrozenValue] | None, ...]
        if result.records:
            records = tuple(result.records)
        else:
            # Preserve invalid/interrupted unreadable artifacts in the report.
            records = (None,)
        projected: list[NeutralProjection] = []
        for index, record in enumerate(records):
            value, kind, identity = _neutral_value(result.kind, record)
            record_index = index if result.root_shape == "sequence" else None
            suffix = f"[{index}]" if record_index is not None else ""
            key = f"{source_artifact}{suffix}#{kind.value}:{identity}"
            frozen = freeze(value)
            assert isinstance(frozen, Mapping)
            projected.append(
                NeutralProjection(
                    key=key,
                    kind=kind,
                    identity=identity,
                    source_artifact=source_artifact,
                    source_digest=result.source_digest,
                    record_index=record_index,
                    lifecycle_state=result.state,
                    value=frozen,
                    semantic_digest=_semantic_digest(value),
                    diagnostics=diagnostics,
                )
            )
        return tuple(projected)


def _semantic_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _normalize_diagnostic(
    diagnostic: CompatibilityDiagnostic,
    *,
    side: str,
    source_artifact: str,
) -> ShadowDiagnostic:
    return ShadowDiagnostic(
        code=diagnostic.code,
        message=diagnostic.message,
        side=side,
        source_artifact=source_artifact,
        value_path=diagnostic.value_path,
        severity=diagnostic.severity,
        line=diagnostic.line,
        column=diagnostic.column,
    )


def _extension_fields(
    value: Mapping[str, Any], known: frozenset[str]
) -> dict[str, Any]:
    return {key: value[key] for key in sorted(value.keys() - known)}


def _identity(value: Mapping[str, Any], field: str, fallback: str) -> str:
    candidate = value.get(field)
    return candidate if isinstance(candidate, str) and candidate else fallback


def _neutral_value(
    reader_kind: str,
    frozen_record: Mapping[str, FrozenValue] | None,
) -> tuple[dict[str, Any], ProjectionKind, str]:
    raw = {} if frozen_record is None else thaw(frozen_record)
    assert isinstance(raw, Mapping)

    if reader_kind == "component":
        component = raw.get("component", {})
        component = component if isinstance(component, Mapping) else {}
        identity = _identity(component, "id", "unknown")
        known = frozenset(
            {
                "schema_version",
                "component",
                "openspec",
                "spec_files",
                "skills",
                "model_selector",
                "requirements",
                "flavor_slots",
                "sample",
                "acceptance",
                "entrypoints",
            }
        )
        value = {
            "component": {
                "identity": identity,
                "name": component.get("name"),
                "kind": component.get("kind", "application"),
                "version": component.get("version"),
                "summary": component.get("summary"),
                "capabilities": component.get("provides", []),
                "extensions": _extension_fields(
                    component,
                    frozenset({"id", "name", "kind", "version", "summary", "provides"}),
                ),
            },
            "specification": raw.get("openspec"),
            "specification_files": raw.get("spec_files", []),
            "authoring_skills": raw.get("skills", []),
            "model_selection": raw.get("model_selector"),
            "dependencies": raw.get("requirements", []),
            "flavor_slots": raw.get("flavor_slots", {}),
            "sample": raw.get("sample"),
            "acceptance": raw.get("acceptance", []),
            "entrypoints": raw.get("entrypoints", {}),
            "extensions": _extension_fields(raw, known),
        }
        return value, ProjectionKind.COMPONENT, identity

    if reader_kind.startswith("settings:"):
        scope = reader_kind.removeprefix("settings:")
        identity = _identity(raw, "runtime_id", scope)
        if scope == "model-portfolio":
            known = frozenset({"schema_version", "endpoints", "groups"})
            value = {
                "scope": scope,
                "models": raw.get("endpoints", {}),
                "model_groups": raw.get("groups", {}),
                "extensions": _extension_fields(raw, known),
            }
        else:
            value = {"scope": scope, "settings": dict(raw)}
        return value, ProjectionKind.SETTINGS, identity

    if reader_kind == "cache":
        identity = _identity(raw, "component_id", "unknown")
        known = frozenset(
            {
                "schema_version",
                "component_id",
                "source_packages",
                "object_packages",
                "latest_source_package_id",
                "latest_object_package_id",
            }
        )
        value = {
            "component_identity": identity,
            "source_packages": raw.get("source_packages", {}),
            "object_packages": raw.get("object_packages", {}),
            "latest": {
                "source_package": raw.get("latest_source_package_id"),
                "object_package": raw.get("latest_object_package_id"),
            },
            "extensions": _extension_fields(raw, known),
        }
        return value, ProjectionKind.CACHE, identity

    if reader_kind == "package:source":
        identity = _identity(raw, "package_id", "unknown")
        known = frozenset(
            {
                "package_id",
                "component_id",
                "source_identity",
                "files",
                "dependencies",
                "spec_files",
                "skill_ids",
                "model_selector_digest",
                "content_digest",
                "created_at",
            }
        )
        value = {
            "package_identity": identity,
            "package_kind": "source",
            "component_identity": raw.get("component_id"),
            "source_identity": raw.get("source_identity"),
            "members": raw.get("files", []),
            "dependencies": raw.get("dependencies", []),
            "specification_files": raw.get("spec_files", []),
            "authoring_skills": raw.get("skill_ids", []),
            "model_selection_identity": raw.get("model_selector_digest"),
            "content_identity": raw.get("content_digest"),
            "created_at": raw.get("created_at"),
            "extensions": _extension_fields(raw, known),
        }
        return value, ProjectionKind.SOURCE_PACKAGE, identity

    if reader_kind == "package:object":
        identity = _identity(raw, "package_id", "unknown")
        known = frozenset(
            {
                "package_id",
                "component_id",
                "source_package_id",
                "artifacts",
                "dependency_packages",
                "dependency_components",
                "depending_components",
                "content_digest",
                "built_at",
            }
        )
        value = {
            "package_identity": identity,
            "package_kind": "object",
            "component_identity": raw.get("component_id"),
            "source_package_identity": raw.get("source_package_id"),
            "artifacts": raw.get("artifacts", []),
            "dependency_packages": raw.get("dependency_packages", []),
            "dependency_components": raw.get("dependency_components", {}),
            "depending_components": raw.get("depending_components", []),
            "content_identity": raw.get("content_digest"),
            "built_at": raw.get("built_at"),
            "extensions": _extension_fields(raw, known),
        }
        return value, ProjectionKind.OBJECT_PACKAGE, identity

    if reader_kind == "provenance:generation":
        identity = _identity(raw, "transaction_id", "unknown")
        known = frozenset(
            {
                "schema_version",
                "transaction_id",
                "parent_transaction_id",
                "model_selections",
                "prompt_template_digest",
                "dependency_lock_digest",
                "vocabulary_digest",
                "specification_digest",
                "spec_artifacts",
                "queries",
                "evidence",
                "files",
                "validation",
                "repair_attempts",
                "accepted_at",
            }
        )
        value = {
            "generation_identity": identity,
            "parent_generation_identity": raw.get("parent_transaction_id"),
            "model_selections": raw.get("model_selections", []),
            "input_identities": {
                "prompt_template": raw.get("prompt_template_digest"),
                "dependency_lock": raw.get("dependency_lock_digest"),
                "vocabulary": raw.get("vocabulary_digest"),
                "specification": raw.get("specification_digest"),
            },
            "specification_artifacts": raw.get("spec_artifacts", []),
            "source_queries": raw.get("queries", []),
            "source_evidence": raw.get("evidence", []),
            "generated_files": raw.get("files", []),
            "validation": raw.get("validation", []),
            "repair_attempts": raw.get("repair_attempts"),
            "accepted_at": raw.get("accepted_at"),
            "extensions": _extension_fields(raw, known),
        }
        return value, ProjectionKind.PROVENANCE, identity

    if reader_kind == "publication:settings":
        known = frozenset({"schema_version", "auto_publish", "targets"})
        value = {
            "auto_publish": raw.get("auto_publish"),
            "targets": raw.get("targets", {}),
            "extensions": _extension_fields(raw, known),
        }
        return value, ProjectionKind.PUBLICATION_SETTINGS, "publication-policy"

    if reader_kind == "publication:records":
        identity = _identity(raw, "publication_id", "unknown")
        known = frozenset(
            {
                "publication_id",
                "component_id",
                "source_package_id",
                "object_package_id",
                "target_id",
                "state",
                "published_digest",
                "error",
                "created_at",
                "updated_at",
            }
        )
        value = {
            "publication_identity": identity,
            "component_identity": raw.get("component_id"),
            "source_package_identity": raw.get("source_package_id"),
            "object_package_identity": raw.get("object_package_id"),
            "target_identity": raw.get("target_id"),
            "state": raw.get("state"),
            "published_identity": raw.get("published_digest"),
            "error": raw.get("error"),
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
            "extensions": _extension_fields(raw, known),
        }
        return value, ProjectionKind.PUBLICATION_RECORD, identity

    raise ValueError(f"unsupported OVA reader kind: {reader_kind}")


def _compare_projection(
    key: str,
    baseline: NeutralProjection | None,
    candidate: NeutralProjection | None,
    *,
    baseline_label: str,
    candidate_label: str,
) -> ShadowComparison:
    present = baseline or candidate
    assert present is not None
    diagnostics: list[ShadowDiagnostic] = []
    if baseline is None or candidate is None:
        missing_side = baseline_label if baseline is None else candidate_label
        diagnostics.append(
            ShadowDiagnostic(
                code="ova.shadow.missing",
                message=f"projection is absent from {missing_side}",
                side=missing_side,
                source_artifact=present.source_artifact,
                severity=DiagnosticSeverity.WARNING,
            )
        )
        status = ShadowStatus.MISSING
    else:
        diagnostics.extend(baseline.diagnostics)
        diagnostics.extend(candidate.diagnostics)
        states = {baseline.lifecycle_state, candidate.lifecycle_state}
        if LifecycleState.INVALID in states:
            status = ShadowStatus.INVALID
        elif LifecycleState.INTERRUPTED in states:
            status = ShadowStatus.INTERRUPTED
        elif LifecycleState.DRIFTED in states:
            status = ShadowStatus.DRIFTED
        elif baseline.semantic_digest == candidate.semantic_digest:
            status = ShadowStatus.EXACT
        else:
            status = ShadowStatus.DRIFTED
            diagnostics.append(
                ShadowDiagnostic(
                    code="ova.shadow.semantic_drift",
                    message="normalized projection digests differ",
                    side="comparison",
                    source_artifact=present.source_artifact,
                    severity=DiagnosticSeverity.WARNING,
                )
            )
    return ShadowComparison(
        key=key,
        kind=present.kind,
        identity=present.identity,
        status=status,
        baseline_digest=(None if baseline is None else baseline.semantic_digest),
        candidate_digest=(None if candidate is None else candidate.semantic_digest),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "NeutralProjection",
    "OvaShadowComparator",
    "OvaShadowReport",
    "ProjectionKind",
    "ShadowComparison",
    "ShadowDiagnostic",
    "ShadowInventory",
    "ShadowStatus",
]
