"""Verifier-owned evidence for admitting generated source before artifact builds."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    int_value,
    parse_tuple,
    string_value,
    unique,
)
from .blobs import BlobRef
from .executable_components import SourceGenerationResumeCandidate
from .generation_cache import CachedSourceFile, SourceDerivationCacheKey
from .identity import (
    SCHEMA_PREFIX,
    ContentIdentity,
    canonical_identity,
    contract_identity,
)

STANDARD_SOURCE_SELECTOR_SCHEMA = f"{SCHEMA_PREFIX}standard-source-selector"
STANDARD_SOURCE_SELECTOR_SET_SCHEMA = f"{SCHEMA_PREFIX}standard-source-selector-set"
STANDARD_SOURCE_TEST_RESULT_SCHEMA = f"{SCHEMA_PREFIX}standard-source-test-result"
STANDARD_SOURCE_ADMISSION_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-source-admission-evidence"
)
STANDARD_SOURCE_ADMISSION_MEMBERSHIP_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-source-admission-membership"
)
STANDARD_SOURCE_ADMISSION_CACHE_ENTRY_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-source-admission-cache-entry"
)
STANDARD_SOURCE_ADMISSION_RECEIPT_COMPOSITION_SCHEMA = (
    f"{SCHEMA_PREFIX}standard-source-admission-receipt-composition"
)


def _identity(value: object, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


def _identity_fields(
    data: object, names: frozenset[str], path: str
) -> dict[str, ContentIdentity]:
    assert isinstance(data, dict) or hasattr(data, "__getitem__")
    return {
        name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")  # type: ignore[index]
        for name in names
    }


class StandardSourceSelectorScope(StrEnum):
    TARGET_INDEPENDENT = "target-independent"
    TARGET_SPECIFIC = "target-specific"


@dataclass(frozen=True, slots=True)
class StandardSourceSelector:
    axis: str
    value: str

    SCHEMA: ClassVar[str] = STANDARD_SOURCE_SELECTOR_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.axis, "StandardSourceSelector.axis", max_length=128)
        string_value(self.value, "StandardSourceSelector.value", max_length=512)

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {"schema": self.SCHEMA, "axis": self.axis, "value": self.value}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardSourceSelector"
    ) -> StandardSourceSelector:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"axis", "value"}),
        )
        return cls(
            string_value(data["axis"], f"{path}.axis", max_length=128),
            string_value(data["value"], f"{path}.value", max_length=512),
        )


@dataclass(frozen=True, slots=True)
class StandardSourceSelectorSet:
    scope: StandardSourceSelectorScope
    selectors: tuple[StandardSourceSelector, ...]

    SCHEMA: ClassVar[str] = STANDARD_SOURCE_SELECTOR_SET_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.scope, StandardSourceSelectorScope):
            fail("StandardSourceSelectorSet.scope", "must be a typed selector scope")
        if any(not isinstance(item, StandardSourceSelector) for item in self.selectors):
            fail(
                "StandardSourceSelectorSet.selectors",
                "must contain typed source selectors",
            )
        pairs = tuple((item.axis, item.value) for item in self.selectors)
        unique(pairs, "StandardSourceSelectorSet.selectors")
        if pairs != tuple(sorted(pairs)):
            fail(
                "StandardSourceSelectorSet.selectors",
                "must use canonical axis/value order",
            )
        if (
            self.scope is StandardSourceSelectorScope.TARGET_INDEPENDENT
            and self.selectors
        ):
            fail(
                "StandardSourceSelectorSet.selectors",
                "target-independent source cannot carry target selectors",
            )
        if (
            self.scope is StandardSourceSelectorScope.TARGET_SPECIFIC
            and not self.selectors
        ):
            fail(
                "StandardSourceSelectorSet.selectors",
                "target-specific source requires at least one selector",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def compatible_with(self, worker: StandardSourceSelectorSet) -> bool:
        if not isinstance(worker, StandardSourceSelectorSet):
            return False
        return (
            self.scope is StandardSourceSelectorScope.TARGET_INDEPENDENT
            or self == worker
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "scope": self.scope.value,
            "selectors": [item.to_dict() for item in self.selectors],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardSourceSelectorSet"
    ) -> StandardSourceSelectorSet:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"scope", "selectors"}),
        )
        return cls(
            enum_value(StandardSourceSelectorScope, data["scope"], f"{path}.scope"),
            parse_tuple(
                data["selectors"],
                f"{path}.selectors",
                StandardSourceSelector.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardSourceTestResult:
    oracle_identity: ContentIdentity
    result_identity: ContentIdentity
    case_count: int
    passed_count: int
    failed_count: int
    skipped_count: int

    SCHEMA: ClassVar[str] = STANDARD_SOURCE_TEST_RESULT_SCHEMA

    def __post_init__(self) -> None:
        _identity(self.oracle_identity, "StandardSourceTestResult.oracle_identity")
        _identity(self.result_identity, "StandardSourceTestResult.result_identity")
        for name in ("case_count", "passed_count", "failed_count", "skipped_count"):
            int_value(getattr(self, name), f"StandardSourceTestResult.{name}")
        if (
            self.case_count <= 0
            or self.passed_count != self.case_count
            or self.failed_count
            or self.skipped_count
        ):
            fail(
                "StandardSourceTestResult",
                "source admission requires every non-empty oracle case to pass",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "oracle_identity": self.oracle_identity.to_dict(),
            "result_identity": self.result_identity.to_dict(),
            "case_count": self.case_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardSourceTestResult"
    ) -> StandardSourceTestResult:
        names = frozenset(
            {
                "oracle_identity",
                "result_identity",
                "case_count",
                "passed_count",
                "failed_count",
                "skipped_count",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            ContentIdentity.from_dict(
                data["oracle_identity"], path=f"{path}.oracle_identity"
            ),
            ContentIdentity.from_dict(
                data["result_identity"], path=f"{path}.result_identity"
            ),
            int_value(data["case_count"], f"{path}.case_count"),
            int_value(data["passed_count"], f"{path}.passed_count"),
            int_value(data["failed_count"], f"{path}.failed_count"),
            int_value(data["skipped_count"], f"{path}.skipped_count"),
        )


@dataclass(frozen=True, slots=True)
class StandardSourceAdmissionEvidence:
    component_revision_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    recipe_identity: ContentIdentity
    orchestration_request_identity: ContentIdentity
    planned_coding_cli_request_identity: ContentIdentity
    flavor_set_identity: ContentIdentity
    skill_closure_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    source_bundle_identity: ContentIdentity
    source_manifest_identity: ContentIdentity
    source_bom_identity: ContentIdentity
    generated_test_suite_identity: ContentIdentity
    coding_cli_tool_binding_identity: ContentIdentity
    coding_cli_transcript_identity: ContentIdentity
    generation_provenance_identity: ContentIdentity
    test_plan_identity: ContentIdentity
    test_results: tuple[StandardSourceTestResult, ...]
    source_selectors: StandardSourceSelectorSet
    framework_distribution_identity: ContentIdentity
    verifier_identity: ContentIdentity
    admitted_at: str

    SCHEMA: ClassVar[str] = STANDARD_SOURCE_ADMISSION_EVIDENCE_SCHEMA
    IDENTITY_FIELDS: ClassVar[tuple[str, ...]] = (
        "component_revision_identity",
        "component_lock_identity",
        "generation_plan_identity",
        "generation_key_identity",
        "recipe_identity",
        "orchestration_request_identity",
        "planned_coding_cli_request_identity",
        "flavor_set_identity",
        "skill_closure_identity",
        "source_tree_identity",
        "source_bundle_identity",
        "source_manifest_identity",
        "source_bom_identity",
        "generated_test_suite_identity",
        "coding_cli_tool_binding_identity",
        "coding_cli_transcript_identity",
        "generation_provenance_identity",
        "test_plan_identity",
        "framework_distribution_identity",
        "verifier_identity",
    )

    def __post_init__(self) -> None:
        for name in self.IDENTITY_FIELDS:
            _identity(getattr(self, name), f"StandardSourceAdmissionEvidence.{name}")
        if not self.test_results or any(
            not isinstance(item, StandardSourceTestResult) for item in self.test_results
        ):
            fail(
                "StandardSourceAdmissionEvidence.test_results",
                "must contain at least one typed passing verifier result",
            )
        identities = tuple(item.identity.uri for item in self.test_results)
        if identities != tuple(sorted(set(identities))):
            fail(
                "StandardSourceAdmissionEvidence.test_results",
                "must use unique canonical result order",
            )
        if not isinstance(self.source_selectors, StandardSourceSelectorSet):
            fail(
                "StandardSourceAdmissionEvidence.source_selectors",
                "must be a StandardSourceSelectorSet",
            )
        string_value(
            self.admitted_at,
            "StandardSourceAdmissionEvidence.admitted_at",
            max_length=64,
        )

    @property
    def identity(self) -> ContentIdentity:
        """Semantic identity; admission time is deliberately non-authoritative."""

        return canonical_identity(self.semantic_dict())

    def semantic_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            **{name: getattr(self, name).to_dict() for name in self.IDENTITY_FIELDS},
            "test_results": [item.to_dict() for item in self.test_results],
            "source_selectors": self.source_selectors.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.semantic_dict(),
            "admitted_at": self.admitted_at,
            "evidence_identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardSourceAdmissionEvidence"
    ) -> StandardSourceAdmissionEvidence:
        identity_names = frozenset(cls.IDENTITY_FIELDS)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=identity_names
            | frozenset(
                {
                    "test_results",
                    "source_selectors",
                    "admitted_at",
                    "evidence_identity",
                }
            ),
        )
        evidence = cls(
            **_identity_fields(data, identity_names, path),
            test_results=parse_tuple(
                data["test_results"],
                f"{path}.test_results",
                StandardSourceTestResult.from_dict,
            ),
            source_selectors=StandardSourceSelectorSet.from_dict(
                data["source_selectors"], path=f"{path}.source_selectors"
            ),
            admitted_at=string_value(data["admitted_at"], f"{path}.admitted_at"),
        )
        recorded = ContentIdentity.from_dict(
            data["evidence_identity"], path=f"{path}.evidence_identity"
        )
        if recorded != evidence.identity:
            fail(f"{path}.evidence_identity", "does not match semantic evidence")
        return evidence


@dataclass(frozen=True, slots=True)
class StandardSourceAdmissionMembership:
    generation: SourceGenerationResumeCandidate
    evidence: StandardSourceAdmissionEvidence

    SCHEMA: ClassVar[str] = STANDARD_SOURCE_ADMISSION_MEMBERSHIP_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.generation, SourceGenerationResumeCandidate):
            fail(
                "StandardSourceAdmissionMembership.generation",
                "must be a SourceGenerationResumeCandidate",
            )
        if not isinstance(self.evidence, StandardSourceAdmissionEvidence):
            fail(
                "StandardSourceAdmissionMembership.evidence",
                "must be verifier-owned source admission evidence",
            )
        candidate = self.generation.output.candidate
        provenance = self.generation.output.provenance
        bindings = {
            "component_revision_identity": candidate.component_revision,
            "component_lock_identity": provenance.component_lock_identity,
            "generation_plan_identity": candidate.component_generation_plan_identity,
            "generation_key_identity": candidate.generation_key_identity,
            "recipe_identity": candidate.recipe_identity,
            "orchestration_request_identity": (
                candidate.source_generation_request_identity
            ),
            "planned_coding_cli_request_identity": (
                candidate.planned_coding_cli_request_identity
            ),
            "source_tree_identity": candidate.tree_identity,
            "source_bundle_identity": candidate.source_bundle_identity,
            "source_manifest_identity": candidate.source_manifest_identity,
            "source_bom_identity": candidate.source_bom_identity,
            "generated_test_suite_identity": (candidate.generated_test_suite_identity),
            "generation_provenance_identity": (
                self.generation.output.provenance_identity
            ),
        }
        for name, actual in bindings.items():
            if getattr(self.evidence, name) != actual:
                fail(
                    f"StandardSourceAdmissionMembership.evidence.{name}",
                    "does not match the exact generated source custody",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def component_revision(self) -> ContentIdentity:
        return self.evidence.component_revision_identity

    @property
    def generation_key_identity(self) -> ContentIdentity:
        return self.evidence.generation_key_identity

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "generation": self.generation.to_dict(),
            "evidence": self.evidence.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardSourceAdmissionMembership"
    ) -> StandardSourceAdmissionMembership:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"generation", "evidence"}),
        )
        return cls(
            SourceGenerationResumeCandidate.from_dict(
                data["generation"], path=f"{path}.generation"
            ),
            StandardSourceAdmissionEvidence.from_dict(
                data["evidence"], path=f"{path}.evidence"
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardSourceAdmissionCacheEntry:
    """Immutable source bytes plus verifier evidence, with no artifact claims."""

    cache_key: SourceDerivationCacheKey
    membership: StandardSourceAdmissionMembership
    source_files: tuple[CachedSourceFile, ...]
    source_manifest: BlobRef
    source_bom: BlobRef
    generated_test_suite: BlobRef
    provenance_evidence: BlobRef
    admission_evidence: BlobRef

    SCHEMA: ClassVar[str] = STANDARD_SOURCE_ADMISSION_CACHE_ENTRY_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.cache_key, SourceDerivationCacheKey):
            fail(
                "StandardSourceAdmissionCacheEntry.cache_key",
                "must be a SourceDerivationCacheKey",
            )
        if self.cache_key.source_semantics_identity is None:
            fail(
                "StandardSourceAdmissionCacheEntry.cache_key",
                "must bind a stable accepted-source semantics identity",
            )
        if not isinstance(self.membership, StandardSourceAdmissionMembership):
            fail(
                "StandardSourceAdmissionCacheEntry.membership",
                "must be a StandardSourceAdmissionMembership",
            )
        if not self.source_files or any(
            not isinstance(item, CachedSourceFile) for item in self.source_files
        ):
            fail(
                "StandardSourceAdmissionCacheEntry.source_files",
                "must contain the complete generated source tree",
            )
        paths = tuple(item.path for item in self.source_files)
        if paths != tuple(sorted(set(paths))):
            fail(
                "StandardSourceAdmissionCacheEntry.source_files",
                "must use unique canonical path order",
            )
        for name in (
            "source_manifest",
            "source_bom",
            "generated_test_suite",
            "provenance_evidence",
            "admission_evidence",
        ):
            if not isinstance(getattr(self, name), BlobRef):
                fail(
                    f"StandardSourceAdmissionCacheEntry.{name}",
                    "must be a BlobRef",
                )
        candidate = self.membership.generation.output.candidate
        evidence = self.membership.evidence
        if (
            self.cache_key.recipe_identity != candidate.recipe_identity
            or self.cache_key.coding_cli_tool_binding_identity
            != evidence.coding_cli_tool_binding_identity
            or self.cache_key.request_identity
            != evidence.planned_coding_cli_request_identity
        ):
            fail(
                "StandardSourceAdmissionCacheEntry.cache_key",
                "does not bind the admitted planned coding-CLI request and tool",
            )
        for actual, expected, label in (
            (
                self.source_manifest.identity,
                candidate.source_manifest_identity.uri,
                "source manifest",
            ),
            (self.source_bom.identity, candidate.source_bom_identity.uri, "source BOM"),
            (
                self.generated_test_suite.identity,
                candidate.generated_test_suite_identity.uri,
                "generated test suite",
            ),
            (
                self.provenance_evidence.identity,
                self.membership.generation.output.provenance_identity.uri,
                "generation provenance",
            ),
            (
                self.admission_evidence.identity,
                evidence.identity.uri,
                "source admission evidence",
            ),
        ):
            if actual != expected:
                fail(
                    "StandardSourceAdmissionCacheEntry",
                    f"{label} blob does not match its exact admitted identity",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def source_tree_identity(self) -> ContentIdentity:
        return self.membership.evidence.source_tree_identity

    @property
    def component_lock_identity(self) -> ContentIdentity:
        return self.membership.evidence.component_lock_identity

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "cache_key": self.cache_key.to_dict(),
            "membership": self.membership.to_dict(),
            "source_files": [item.to_dict() for item in self.source_files],
            "source_manifest": self.source_manifest.to_dict(),
            "source_bom": self.source_bom.to_dict(),
            "generated_test_suite": self.generated_test_suite.to_dict(),
            "provenance_evidence": self.provenance_evidence.to_dict(),
            "admission_evidence": self.admission_evidence.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardSourceAdmissionCacheEntry"
    ) -> StandardSourceAdmissionCacheEntry:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "cache_key",
                    "membership",
                    "source_files",
                    "source_manifest",
                    "source_bom",
                    "generated_test_suite",
                    "provenance_evidence",
                    "admission_evidence",
                }
            ),
        )
        return cls(
            SourceDerivationCacheKey.from_dict(
                data["cache_key"], path=f"{path}.cache_key"
            ),
            StandardSourceAdmissionMembership.from_dict(
                data["membership"], path=f"{path}.membership"
            ),
            parse_tuple(
                data["source_files"],
                f"{path}.source_files",
                CachedSourceFile.from_dict,
            ),
            BlobRef.from_dict(data["source_manifest"], path=f"{path}.source_manifest"),
            BlobRef.from_dict(data["source_bom"], path=f"{path}.source_bom"),
            BlobRef.from_dict(
                data["generated_test_suite"],
                path=f"{path}.generated_test_suite",
            ),
            BlobRef.from_dict(
                data["provenance_evidence"],
                path=f"{path}.provenance_evidence",
            ),
            BlobRef.from_dict(
                data["admission_evidence"], path=f"{path}.admission_evidence"
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardSourceAdmissionReceiptComposition:
    execution_plan_identity: ContentIdentity
    source_admission_identities: tuple[ContentIdentity, ...]
    downstream_lifecycle_result_identities: tuple[ContentIdentity, ...]
    project_admission_identity: ContentIdentity

    SCHEMA: ClassVar[str] = STANDARD_SOURCE_ADMISSION_RECEIPT_COMPOSITION_SCHEMA

    def __post_init__(self) -> None:
        _identity(
            self.execution_plan_identity,
            "StandardSourceAdmissionReceiptComposition.execution_plan_identity",
        )
        _identity(
            self.project_admission_identity,
            "StandardSourceAdmissionReceiptComposition.project_admission_identity",
        )
        if not self.source_admission_identities or len(
            self.source_admission_identities
        ) != len(self.downstream_lifecycle_result_identities):
            fail(
                "StandardSourceAdmissionReceiptComposition",
                "source admissions and successful downstream results must cover the "
                "same non-empty node set",
            )
        for name in (
            "source_admission_identities",
            "downstream_lifecycle_result_identities",
        ):
            values = getattr(self, name)
            for index, value in enumerate(values):
                _identity(
                    value, f"StandardSourceAdmissionReceiptComposition.{name}[{index}]"
                )
            unique(
                tuple(item.uri for item in values),
                f"StandardSourceAdmissionReceiptComposition.{name}",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "source_admission_identities": [
                item.to_dict() for item in self.source_admission_identities
            ],
            "downstream_lifecycle_result_identities": [
                item.to_dict() for item in self.downstream_lifecycle_result_identities
            ],
            "project_admission_identity": self.project_admission_identity.to_dict(),
        }


__all__ = [
    "STANDARD_SOURCE_ADMISSION_EVIDENCE_SCHEMA",
    "STANDARD_SOURCE_ADMISSION_CACHE_ENTRY_SCHEMA",
    "STANDARD_SOURCE_ADMISSION_MEMBERSHIP_SCHEMA",
    "STANDARD_SOURCE_ADMISSION_RECEIPT_COMPOSITION_SCHEMA",
    "STANDARD_SOURCE_SELECTOR_SCHEMA",
    "STANDARD_SOURCE_SELECTOR_SET_SCHEMA",
    "STANDARD_SOURCE_TEST_RESULT_SCHEMA",
    "StandardSourceAdmissionEvidence",
    "StandardSourceAdmissionCacheEntry",
    "StandardSourceAdmissionMembership",
    "StandardSourceAdmissionReceiptComposition",
    "StandardSourceSelector",
    "StandardSourceSelectorScope",
    "StandardSourceSelectorSet",
    "StandardSourceTestResult",
]
