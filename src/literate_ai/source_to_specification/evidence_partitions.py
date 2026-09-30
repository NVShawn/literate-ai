"""Complete path-disposition evidence for bounded inverse-model input."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from literate_ai.source_to_specification.contracts import (
    EvidenceReference,
    SourceToSpecificationError,
    canonical_digest,
)
from literate_ai.source_to_specification.inventory import (
    SourceFileClassification,
    SourceInventory,
)

EVIDENCE_PARTITION_MANIFEST_SCHEMA = (
    "urn:literate-ai:schema:v1:inverse-evidence-partition-manifest"
)
MODEL_EVIDENCE_BATCH_PLAN_SCHEMA = (
    "urn:literate-ai:schema:v1:inverse-model-evidence-batch-plan"
)

_SAFE = frozenset(
    {
        SourceFileClassification.CONFIGURATION,
        SourceFileClassification.DOCUMENTATION,
        SourceFileClassification.SOURCE,
        SourceFileClassification.TEST,
    }
)


class PartitionIntelligence(Protocol):
    authority_source_snapshot_id: str
    provider_id: str
    provider_version: str
    executable_identity: str
    evidence: tuple[object, ...]


class EvidencePathDisposition(StrEnum):
    FULL = "full"
    SLICED = "sliced"
    EMPTY = "empty"
    SENSITIVE_EXCLUDED = "sensitive-excluded"
    UNSUPPORTED = "unsupported"
    DUPLICATE = "duplicate"
    BUDGET_PARTITIONED = "budget-partitioned"
    BLOCKING_OMITTED = "blocking-omitted"


@dataclass(frozen=True, slots=True)
class ModelEvidenceBatch:
    language: str
    language_ordinal: int
    language_batch_count: int
    evidence_ids: tuple[str, ...]
    source_paths: tuple[str, ...]
    admitted_byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.language, str) or not self.language:
            raise SourceToSpecificationError(
                "evidence_batch.language_invalid", "batch language must be non-empty"
            )
        if (
            isinstance(self.language_ordinal, bool)
            or not isinstance(self.language_ordinal, int)
            or self.language_ordinal < 0
            or isinstance(self.language_batch_count, bool)
            or not isinstance(self.language_batch_count, int)
            or self.language_batch_count < 1
            or self.language_ordinal >= self.language_batch_count
            or isinstance(self.admitted_byte_count, bool)
            or not isinstance(self.admitted_byte_count, int)
            or self.admitted_byte_count < 1
        ):
            raise SourceToSpecificationError(
                "evidence_batch.bounds_invalid", "batch bounds are invalid"
            )
        if (
            not self.evidence_ids
            or self.evidence_ids != tuple(sorted(set(self.evidence_ids)))
            or not all(isinstance(item, str) and item for item in self.evidence_ids)
            or not self.source_paths
            or self.source_paths != tuple(sorted(set(self.source_paths)))
            or not all(isinstance(item, str) and item for item in self.source_paths)
        ):
            raise SourceToSpecificationError(
                "evidence_batch.members_invalid", "batch members are not canonical"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "language": self.language,
            "language_ordinal": self.language_ordinal,
            "language_batch_count": self.language_batch_count,
            "evidence_ids": list(self.evidence_ids),
            "source_paths": list(self.source_paths),
            "admitted_byte_count": self.admitted_byte_count,
        }

    @classmethod
    def from_dict(cls, value: object) -> ModelEvidenceBatch:
        fields = {
            "language",
            "language_ordinal",
            "language_batch_count",
            "evidence_ids",
            "source_paths",
            "admitted_byte_count",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise SourceToSpecificationError(
                "evidence_batch.record_invalid", "model evidence batch is invalid"
            )
        evidence_ids = value["evidence_ids"]
        paths = value["source_paths"]
        if not isinstance(evidence_ids, list) or not isinstance(paths, list):
            raise SourceToSpecificationError(
                "evidence_batch.record_invalid", "model evidence arrays are invalid"
            )
        return cls(
            language=value["language"],  # type: ignore[arg-type]
            language_ordinal=value["language_ordinal"],  # type: ignore[arg-type]
            language_batch_count=value["language_batch_count"],  # type: ignore[arg-type]
            evidence_ids=tuple(evidence_ids),  # type: ignore[arg-type]
            source_paths=tuple(paths),  # type: ignore[arg-type]
            admitted_byte_count=value["admitted_byte_count"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ModelEvidenceBatchPlan:
    source_snapshot_identity: str
    maximum_batch_bytes: int
    batches: tuple[ModelEvidenceBatch, ...]

    SCHEMA: ClassVar[str] = MODEL_EVIDENCE_BATCH_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(
            self.source_snapshot_identity, str
        ) or not self.source_snapshot_identity.startswith("sha256:"):
            raise SourceToSpecificationError(
                "evidence_batch.identity_invalid",
                "batch-plan source identity must be sha256",
            )
        if (
            isinstance(self.maximum_batch_bytes, bool)
            or not isinstance(self.maximum_batch_bytes, int)
            or self.maximum_batch_bytes < 1
            or not self.batches
            or any(
                item.admitted_byte_count > self.maximum_batch_bytes
                for item in self.batches
            )
        ):
            raise SourceToSpecificationError(
                "evidence_batch.budget_invalid", "batch-plan byte budget is invalid"
            )
        order = tuple((item.language, item.language_ordinal) for item in self.batches)
        if order != tuple(sorted(set(order))):
            raise SourceToSpecificationError(
                "evidence_batch.order_invalid", "batch-plan order is not canonical"
            )
        by_language: dict[str, list[ModelEvidenceBatch]] = {}
        for item in self.batches:
            by_language.setdefault(item.language, []).append(item)
        if any(
            tuple(item.language_ordinal for item in items) != tuple(range(len(items)))
            or any(item.language_batch_count != len(items) for item in items)
            for items in by_language.values()
        ):
            raise SourceToSpecificationError(
                "evidence_batch.sequence_invalid",
                "each language batch sequence must be complete",
            )
        all_ids = tuple(
            evidence_id for batch in self.batches for evidence_id in batch.evidence_ids
        )
        if len(all_ids) != len(set(all_ids)):
            raise SourceToSpecificationError(
                "evidence_batch.duplicate_evidence",
                "evidence may occur in exactly one model batch",
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "source_snapshot_identity": self.source_snapshot_identity,
            "maximum_batch_bytes": self.maximum_batch_bytes,
            "batches": [item.to_dict() for item in self.batches],
        }

    @classmethod
    def from_dict(cls, value: object) -> ModelEvidenceBatchPlan:
        fields = {
            "schema",
            "source_snapshot_identity",
            "maximum_batch_bytes",
            "batches",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise SourceToSpecificationError(
                "evidence_batch.record_invalid", "model evidence batch plan is invalid"
            )
        if value["schema"] != cls.SCHEMA or not isinstance(value["batches"], list):
            raise SourceToSpecificationError(
                "evidence_batch.schema_unsupported",
                "model evidence batch-plan schema is unsupported",
            )
        return cls(
            source_snapshot_identity=value["source_snapshot_identity"],  # type: ignore[arg-type]
            maximum_batch_bytes=value["maximum_batch_bytes"],  # type: ignore[arg-type]
            batches=tuple(
                ModelEvidenceBatch.from_dict(item) for item in value["batches"]
            ),
        )


def plan_model_evidence_batches(
    intelligence: PartitionIntelligence,
    *,
    languages: tuple[str, ...],
    maximum_batch_bytes: int,
) -> ModelEvidenceBatchPlan:
    """Greedily create deterministic per-language calls with no silent omission."""

    if languages != tuple(sorted(set(languages))) or not languages:
        raise SourceToSpecificationError(
            "evidence_batch.languages_invalid", "batch languages must be canonical"
        )
    if (
        isinstance(maximum_batch_bytes, bool)
        or not isinstance(maximum_batch_bytes, int)
        or maximum_batch_bytes < 1
    ):
        raise SourceToSpecificationError(
            "evidence_batch.budget_invalid", "model evidence budget must be positive"
        )
    planned: list[ModelEvidenceBatch] = []
    seen_ids: set[str] = set()
    for language in languages:
        selected = sorted(
            (
                item
                for item in intelligence.evidence
                if getattr(item, "language", None) == language
            ),
            key=lambda item: (
                item.reference.path,
                item.reference.evidence_id,
            ),
        )
        if not selected:
            raise SourceToSpecificationError(
                "evidence_batch.language_empty",
                f"no admitted evidence exists for {language}",
            )
        groups: list[list[object]] = []
        current: list[object] = []
        current_bytes = 0
        for item in selected:
            evidence_id = item.reference.evidence_id
            content = getattr(item, "content", None)
            if (
                not isinstance(evidence_id, str)
                or not evidence_id
                or evidence_id in seen_ids
                or not isinstance(content, str)
                or not content
            ):
                raise SourceToSpecificationError(
                    "evidence_batch.evidence_invalid",
                    "model evidence must be unique, textual, and non-empty",
                )
            byte_count = len(content.encode("utf-8"))
            if byte_count > maximum_batch_bytes:
                raise SourceToSpecificationError(
                    "evidence_batch.item_over_budget",
                    f"evidence {evidence_id} exceeds the model evidence budget",
                )
            if current and current_bytes + byte_count > maximum_batch_bytes:
                groups.append(current)
                current = []
                current_bytes = 0
            current.append(item)
            current_bytes += byte_count
            seen_ids.add(evidence_id)
        groups.append(current)
        for ordinal, group in enumerate(groups):
            planned.append(
                ModelEvidenceBatch(
                    language,
                    ordinal,
                    len(groups),
                    tuple(sorted(item.reference.evidence_id for item in group)),
                    tuple(sorted({item.reference.path for item in group})),
                    sum(len(item.content.encode("utf-8")) for item in group),
                )
            )
    return ModelEvidenceBatchPlan(
        intelligence.authority_source_snapshot_id,
        maximum_batch_bytes,
        tuple(planned),
    )


@dataclass(frozen=True, slots=True)
class EvidencePartitionItem:
    path: str
    source_content_identity: str
    source_byte_count: int
    language: str | None
    classification: str
    disposition: EvidencePathDisposition
    evidence_ids: tuple[str, ...]
    admitted_byte_count: int
    partition_ordinal: int | None
    duplicate_of: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise SourceToSpecificationError(
                "evidence_partition.path_invalid", "partition path must be non-empty"
            )
        if not isinstance(
            self.source_content_identity, str
        ) or not self.source_content_identity.startswith("sha256:"):
            raise SourceToSpecificationError(
                "evidence_partition.identity_invalid",
                "partition source content identity must be sha256",
            )
        if (
            isinstance(self.source_byte_count, bool)
            or not isinstance(self.source_byte_count, int)
            or self.source_byte_count < 0
            or isinstance(self.admitted_byte_count, bool)
            or not isinstance(self.admitted_byte_count, int)
            or self.admitted_byte_count < 0
        ):
            raise SourceToSpecificationError(
                "evidence_partition.byte_count_invalid",
                "partition byte counts must be non-negative integers",
            )
        if self.language is not None and (
            not isinstance(self.language, str) or not self.language
        ):
            raise SourceToSpecificationError(
                "evidence_partition.language_invalid",
                "partition language must be null or non-empty",
            )
        if not isinstance(self.classification, str) or not self.classification:
            raise SourceToSpecificationError(
                "evidence_partition.classification_invalid",
                "partition classification must be non-empty",
            )
        if not isinstance(self.disposition, EvidencePathDisposition):
            raise SourceToSpecificationError(
                "evidence_partition.disposition_invalid",
                "partition disposition must be typed",
            )
        if not all(isinstance(item, str) and item for item in self.evidence_ids):
            raise SourceToSpecificationError(
                "evidence_partition.evidence_invalid",
                "partition evidence IDs must be non-empty strings",
            )
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise SourceToSpecificationError(
                "evidence_partition.evidence_noncanonical",
                "partition evidence IDs must be unique and canonical",
            )
        admitted = bool(self.evidence_ids)
        if admitted != (self.partition_ordinal is not None) or admitted != (
            self.admitted_byte_count > 0
        ):
            raise SourceToSpecificationError(
                "evidence_partition.admission_inconsistent",
                "admitted evidence, byte count, and ordinal must exist together",
            )
        admitted_dispositions = {
            EvidencePathDisposition.FULL,
            EvidencePathDisposition.SLICED,
            EvidencePathDisposition.DUPLICATE,
            EvidencePathDisposition.BUDGET_PARTITIONED,
        }
        if admitted != (self.disposition in admitted_dispositions):
            raise SourceToSpecificationError(
                "evidence_partition.disposition_inconsistent",
                "partition disposition must exactly describe evidence admission",
            )
        if self.partition_ordinal is not None and (
            isinstance(self.partition_ordinal, bool)
            or not isinstance(self.partition_ordinal, int)
            or self.partition_ordinal < 0
        ):
            raise SourceToSpecificationError(
                "evidence_partition.ordinal_invalid",
                "partition ordinal must be a non-negative integer",
            )
        duplicate = self.disposition is EvidencePathDisposition.DUPLICATE
        if self.duplicate_of is not None and (
            not isinstance(self.duplicate_of, str) or not self.duplicate_of
        ):
            raise SourceToSpecificationError(
                "evidence_partition.duplicate_invalid",
                "duplicate path must be a non-empty string",
            )
        if duplicate != (self.duplicate_of is not None):
            raise SourceToSpecificationError(
                "evidence_partition.duplicate_inconsistent",
                "duplicate disposition requires its canonical prior path",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "source_content_identity": self.source_content_identity,
            "source_byte_count": self.source_byte_count,
            "language": self.language,
            "classification": self.classification,
            "disposition": self.disposition.value,
            "evidence_ids": list(self.evidence_ids),
            "admitted_byte_count": self.admitted_byte_count,
            "partition_ordinal": self.partition_ordinal,
            "duplicate_of": self.duplicate_of,
        }

    @classmethod
    def from_dict(cls, value: object) -> EvidencePartitionItem:
        fields = {
            "path",
            "source_content_identity",
            "source_byte_count",
            "language",
            "classification",
            "disposition",
            "evidence_ids",
            "admitted_byte_count",
            "partition_ordinal",
            "duplicate_of",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise SourceToSpecificationError(
                "evidence_partition.record_invalid",
                "partition item has invalid fields",
            )
        evidence_ids = value["evidence_ids"]
        if not isinstance(evidence_ids, list) or not all(
            isinstance(item, str) and item for item in evidence_ids
        ):
            raise SourceToSpecificationError(
                "evidence_partition.record_invalid",
                "partition evidence IDs must be strings",
            )
        try:
            disposition = EvidencePathDisposition(value["disposition"])
        except (TypeError, ValueError) as exc:
            raise SourceToSpecificationError(
                "evidence_partition.record_invalid",
                "partition disposition is unsupported",
            ) from exc
        return cls(
            path=value["path"],  # type: ignore[arg-type]
            source_content_identity=value["source_content_identity"],  # type: ignore[arg-type]
            source_byte_count=value["source_byte_count"],  # type: ignore[arg-type]
            language=value["language"],  # type: ignore[arg-type]
            classification=value["classification"],  # type: ignore[arg-type]
            disposition=disposition,
            evidence_ids=tuple(evidence_ids),
            admitted_byte_count=value["admitted_byte_count"],  # type: ignore[arg-type]
            partition_ordinal=value["partition_ordinal"],  # type: ignore[arg-type]
            duplicate_of=value["duplicate_of"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class EvidencePartitionManifest:
    source_snapshot_identity: str
    collector_identity: str
    items: tuple[EvidencePartitionItem, ...]
    excluded_directories: tuple[str, ...]

    SCHEMA: ClassVar[str] = EVIDENCE_PARTITION_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_snapshot_identity, "source snapshot"),
            (self.collector_identity, "collector"),
        ):
            if not isinstance(value, str) or not value.startswith("sha256:"):
                raise SourceToSpecificationError(
                    "evidence_partition.identity_invalid",
                    f"{label} identity must be sha256",
                )
        paths = tuple(item.path for item in self.items)
        if not paths or paths != tuple(sorted(set(paths))):
            raise SourceToSpecificationError(
                "evidence_partition.paths_noncanonical",
                "every inventory path must occur exactly once in canonical order",
            )
        if self.excluded_directories != tuple(sorted(set(self.excluded_directories))):
            raise SourceToSpecificationError(
                "evidence_partition.directories_noncanonical",
                "excluded directories must be unique and canonical",
            )
        ordinals = tuple(
            item.partition_ordinal
            for item in self.items
            if item.partition_ordinal is not None
        )
        if ordinals != tuple(range(len(ordinals))):
            raise SourceToSpecificationError(
                "evidence_partition.ordinals_noncanonical",
                "admitted partitions must use contiguous canonical ordinals",
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    @property
    def blocking_paths(self) -> tuple[str, ...]:
        return tuple(
            item.path
            for item in self.items
            if item.disposition is EvidencePathDisposition.BLOCKING_OMITTED
        )

    def require_complete(self) -> None:
        if self.blocking_paths:
            raise SourceToSpecificationError(
                "evidence_partition.required_omitted",
                "required source evidence was omitted by the model budget: "
                + ", ".join(self.blocking_paths),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "source_snapshot_identity": self.source_snapshot_identity,
            "collector_identity": self.collector_identity,
            "items": [item.to_dict() for item in self.items],
            "excluded_directories": list(self.excluded_directories),
        }

    @classmethod
    def from_dict(cls, value: object) -> EvidencePartitionManifest:
        fields = {
            "schema",
            "source_snapshot_identity",
            "collector_identity",
            "items",
            "excluded_directories",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise SourceToSpecificationError(
                "evidence_partition.record_invalid",
                "partition manifest has invalid fields",
            )
        if value["schema"] != cls.SCHEMA:
            raise SourceToSpecificationError(
                "evidence_partition.schema_unsupported",
                "partition manifest schema is unsupported",
            )
        raw_items = value["items"]
        raw_directories = value["excluded_directories"]
        if not isinstance(raw_items, list) or not isinstance(raw_directories, list):
            raise SourceToSpecificationError(
                "evidence_partition.record_invalid",
                "partition manifest arrays are invalid",
            )
        if not all(isinstance(item, str) and item for item in raw_directories):
            raise SourceToSpecificationError(
                "evidence_partition.record_invalid",
                "partition excluded directories must be strings",
            )
        return cls(
            source_snapshot_identity=value["source_snapshot_identity"],  # type: ignore[arg-type]
            collector_identity=value["collector_identity"],  # type: ignore[arg-type]
            items=tuple(EvidencePartitionItem.from_dict(item) for item in raw_items),
            excluded_directories=tuple(raw_directories),
        )


def build_evidence_partition_manifest(
    inventory: SourceInventory,
    intelligence: PartitionIntelligence,
) -> EvidencePartitionManifest:
    if intelligence.authority_source_snapshot_id != inventory.identity:
        raise SourceToSpecificationError(
            "evidence_partition.snapshot_mismatch",
            "source intelligence describes another inventory",
        )
    collector_identity = canonical_digest(
        {
            "schema": "literate-ai/inverse-evidence-partitioner@1",
            "provider_id": intelligence.provider_id,
            "provider_version": intelligence.provider_version,
            "executable_identity": intelligence.executable_identity,
        }
    )
    evidence_by_path: dict[str, list[object]] = {}
    inventory_paths = {item.path for item in inventory.entries}
    for item in intelligence.evidence:
        reference = getattr(item, "reference", None)
        if isinstance(reference, EvidenceReference):
            if reference.path not in inventory_paths:
                raise SourceToSpecificationError(
                    "evidence_partition.path_unbound",
                    "source intelligence contains evidence outside the inventory",
                )
            evidence_by_path.setdefault(reference.path, []).append(item)
    first_digest_path: dict[str, str] = {}
    ordinal = 0
    items = []
    for entry in sorted(inventory.entries, key=lambda value: value.path):
        evidence = evidence_by_path.get(entry.path, [])
        evidence_ids = tuple(sorted({item.reference.evidence_id for item in evidence}))
        admitted_bytes = sum(
            len(item.content.encode("utf-8"))
            for item in evidence
            if isinstance(getattr(item, "content", None), str)
        )
        partition_ordinal = ordinal if evidence_ids else None
        if evidence_ids:
            ordinal += 1
        duplicate_of = first_digest_path.get(entry.content_digest)
        if entry.classification is SourceFileClassification.SENSITIVE:
            if evidence_ids:
                raise SourceToSpecificationError(
                    "evidence_partition.sensitive_admitted",
                    "sensitive source evidence cannot be admitted to a model partition",
                )
            disposition = EvidencePathDisposition.SENSITIVE_EXCLUDED
        elif entry.classification is SourceFileClassification.LARGE:
            disposition = (
                EvidencePathDisposition.BUDGET_PARTITIONED
                if evidence_ids
                else EvidencePathDisposition.BLOCKING_OMITTED
            )
        elif entry.classification not in _SAFE:
            if evidence_ids:
                raise SourceToSpecificationError(
                    "evidence_partition.unsupported_admitted",
                    "unsupported source evidence cannot be admitted to a model "
                    "partition",
                )
            disposition = EvidencePathDisposition.UNSUPPORTED
        elif entry.size == 0:
            disposition = EvidencePathDisposition.EMPTY
        elif not evidence_ids:
            disposition = EvidencePathDisposition.BLOCKING_OMITTED
        elif duplicate_of is not None:
            disposition = EvidencePathDisposition.DUPLICATE
        elif any(
            item.reference.content_digest == entry.content_digest
            and len(item.content.encode("utf-8")) == entry.size
            for item in evidence
        ):
            disposition = EvidencePathDisposition.FULL
        else:
            disposition = EvidencePathDisposition.SLICED
        items.append(
            EvidencePartitionItem(
                entry.path,
                entry.content_digest,
                entry.size,
                entry.language,
                entry.classification.value,
                disposition,
                evidence_ids,
                admitted_bytes,
                partition_ordinal,
                duplicate_of
                if disposition is EvidencePathDisposition.DUPLICATE
                else None,
            )
        )
        first_digest_path.setdefault(entry.content_digest, entry.path)
    return EvidencePartitionManifest(
        inventory.identity,
        collector_identity,
        tuple(items),
        inventory.excluded_directories,
    )


__all__ = [
    "EVIDENCE_PARTITION_MANIFEST_SCHEMA",
    "MODEL_EVIDENCE_BATCH_PLAN_SCHEMA",
    "EvidencePartitionItem",
    "EvidencePartitionManifest",
    "EvidencePathDisposition",
    "ModelEvidenceBatch",
    "ModelEvidenceBatchPlan",
    "PartitionIntelligence",
    "build_evidence_partition_manifest",
    "plan_model_evidence_batches",
]
