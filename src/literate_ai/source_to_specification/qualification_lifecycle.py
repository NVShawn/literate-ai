"""Lifecycle-backed regenerative qualification contracts and application runner.

This module deliberately depends on the public Standard lifecycle result contracts,
not on CLI envelopes or filesystem adapters.  Host composition supplies the narrow
ports; qualification owns evidence admission and all derived counts and coverage.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Protocol

from literate_ai.application.standard_project_lifecycle import (
    StandardProjectLifecycleResult,
)
from literate_ai.contracts.executable_components import SourceGenerationDisposition
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
)
from literate_ai.contracts.standard_lifecycle_membership import (
    StandardNodeCacheOutcome,
    StandardProjectLifecycleMembership,
)
from literate_ai.contracts.standard_lifecycle_policy import (
    STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
)
from literate_ai.contracts.standard_post_source_evidence import (
    StandardBuildEvidence,
    StandardComponentAcceptanceEvidence,
    StandardGeneratedTestExecutionEvidence,
)
from literate_ai.contracts.testing import ProjectTestReceipt

from .contracts import canonical_digest
from .errors import SourceToSpecificationError

_PREFIX = "urn:literate-ai:schema:v2:"


def parse_qualification_json_result(content: bytes) -> object:
    """Decode one unambiguous, finite JSON result for live or retained parity."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate qualification result field")
            result[key] = value
        return result

    if not isinstance(content, bytes):
        raise TypeError("qualification result must be bytes")
    value = json.loads(content.decode("utf-8"), object_pairs_hook=unique)
    # JSON's non-finite Python extensions cannot become qualification results.
    canonical_digest(value)
    return value


def _error(code: str, message: str) -> None:
    raise SourceToSpecificationError(code, message)


def _identity(value: object, field: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        _error("qualification_lifecycle.identity_invalid", f"{field} must be typed")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _error(
            "qualification_lifecycle.text_invalid",
            f"{field} must be a non-empty canonical string",
        )
    return value


def _texts(values: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        _error("qualification_lifecycle.sequence_invalid", f"{field} must be a tuple")
    result = tuple(_text(item, field) for item in values)
    if (not result and not allow_empty) or result != tuple(sorted(set(result))):
        _error(
            "qualification_lifecycle.sequence_invalid",
            f"{field} must be non-empty, unique, and sorted",
        )
    return result


def _identities(values: object, field: str) -> tuple[ContentIdentity, ...]:
    if not isinstance(values, tuple):
        _error("qualification_lifecycle.sequence_invalid", f"{field} must be a tuple")
    result = tuple(_identity(item, field) for item in values)
    uris = tuple(item.uri for item in result)
    if not result or uris != tuple(sorted(set(uris))):
        _error(
            "qualification_lifecycle.sequence_invalid",
            f"{field} must be non-empty, unique, and sorted",
        )
    return result


def _fields(value: object, schema: str, names: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"schema", *names}:
        _error(
            "qualification_lifecycle.wire_invalid",
            "qualification lifecycle document has unknown or missing fields",
        )
    if value.get("schema") != schema:
        _error(
            "qualification_lifecycle.wire_invalid",
            "qualification lifecycle document has an unsupported schema",
        )
    return value


def _parse_identity(value: object, field: str) -> ContentIdentity:
    try:
        return ContentIdentity.from_dict(value, path=field)
    except (TypeError, ValueError) as exc:
        raise SourceToSpecificationError(
            "qualification_lifecycle.wire_invalid",
            f"{field} is not an exact content identity",
        ) from exc


@dataclass(frozen=True, slots=True)
class QualificationCaseSurfaceBinding:
    """Verifier-owned mapping from one exact case to covered behavioral surfaces."""

    case_id: str
    case_identity: ContentIdentity
    surface_ids: tuple[str, ...]

    SCHEMA: ClassVar[str] = _PREFIX + "qualification-case-surface-binding"

    def __post_init__(self) -> None:
        _text(self.case_id, "case_id")
        _identity(self.case_identity, "case_identity")
        _texts(self.surface_ids, "surface_ids")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "case_id": self.case_id,
            "case_identity": self.case_identity.to_dict(),
            "surface_ids": list(self.surface_ids),
        }

    @classmethod
    def from_dict(cls, value: object) -> QualificationCaseSurfaceBinding:
        data = _fields(
            value, cls.SCHEMA, frozenset({"case_id", "case_identity", "surface_ids"})
        )
        surfaces = data["surface_ids"]
        if not isinstance(surfaces, list):
            _error(
                "qualification_lifecycle.wire_invalid", "surface_ids must be an array"
            )
        return cls(
            _text(data["case_id"], "case_id"),
            _parse_identity(data["case_identity"], "case_identity"),
            tuple(surfaces),
        )


@dataclass(frozen=True, slots=True)
class QualificationVerifierCaseMap:
    """Pinned complete case-to-surface authority owned by one verifier."""

    verifier_identity: ContentIdentity
    cases: tuple[QualificationCaseSurfaceBinding, ...]

    SCHEMA: ClassVar[str] = _PREFIX + "qualification-verifier-case-map"

    def __post_init__(self) -> None:
        _identity(self.verifier_identity, "verifier_identity")
        if not self.cases or any(
            not isinstance(item, QualificationCaseSurfaceBinding) for item in self.cases
        ):
            _error(
                "qualification_lifecycle.case_map_invalid",
                "case map must contain typed case bindings",
            )
        keys = tuple((item.case_id, item.case_identity.uri) for item in self.cases)
        if keys != tuple(sorted(set(keys))) or len({item[0] for item in keys}) != len(
            keys
        ):
            _error(
                "qualification_lifecycle.case_map_invalid",
                "case map must be sorted with unique case IDs and identities",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    @property
    def surface_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted({surface for case in self.cases for surface in case.surface_ids})
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "verifier_identity": self.verifier_identity.to_dict(),
            "cases": [item.to_dict() for item in self.cases],
        }

    @classmethod
    def from_dict(cls, value: object) -> QualificationVerifierCaseMap:
        data = _fields(value, cls.SCHEMA, frozenset({"verifier_identity", "cases"}))
        cases = data["cases"]
        if not isinstance(cases, list):
            _error("qualification_lifecycle.wire_invalid", "cases must be an array")
        return cls(
            _parse_identity(data["verifier_identity"], "verifier_identity"),
            tuple(QualificationCaseSurfaceBinding.from_dict(item) for item in cases),
        )


@dataclass(frozen=True, slots=True)
class QualificationParityCaseEvidence:
    """Independent baseline/generated observation for one verifier-owned case."""

    case_id: str
    case_identity: ContentIdentity
    baseline_observation_identity: ContentIdentity
    generated_observation_identity: ContentIdentity
    passed: bool

    SCHEMA: ClassVar[str] = _PREFIX + "qualification-parity-case-evidence"

    def __post_init__(self) -> None:
        _text(self.case_id, "case_id")
        for name in (
            "case_identity",
            "baseline_observation_identity",
            "generated_observation_identity",
        ):
            _identity(getattr(self, name), name)
        if type(self.passed) is not bool:
            _error("qualification_lifecycle.parity_invalid", "passed must be a bool")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "case_id": self.case_id,
            "case_identity": self.case_identity.to_dict(),
            "baseline_observation_identity": (
                self.baseline_observation_identity.to_dict()
            ),
            "generated_observation_identity": (
                self.generated_observation_identity.to_dict()
            ),
            "passed": self.passed,
        }

    @classmethod
    def from_dict(cls, value: object) -> QualificationParityCaseEvidence:
        names = frozenset(
            {
                "case_id",
                "case_identity",
                "baseline_observation_identity",
                "generated_observation_identity",
                "passed",
            }
        )
        data = _fields(value, cls.SCHEMA, names)
        if type(data["passed"]) is not bool:
            _error("qualification_lifecycle.wire_invalid", "passed must be a bool")
        return cls(
            _text(data["case_id"], "case_id"),
            _parse_identity(data["case_identity"], "case_identity"),
            _parse_identity(
                data["baseline_observation_identity"], "baseline_observation_identity"
            ),
            _parse_identity(
                data["generated_observation_identity"], "generated_observation_identity"
            ),
            data["passed"],
        )


@dataclass(frozen=True, slots=True)
class QualificationParityEvidence:
    """Complete independent verifier evidence for one generated lifecycle run."""

    run_identity: ContentIdentity
    source_snapshot_identity: ContentIdentity
    generated_tree_identities: tuple[ContentIdentity, ...]
    verifier_identity: ContentIdentity
    case_map_identity: ContentIdentity
    cases: tuple[QualificationParityCaseEvidence, ...]

    SCHEMA: ClassVar[str] = _PREFIX + "qualification-parity-evidence"

    def __post_init__(self) -> None:
        for name in (
            "run_identity",
            "source_snapshot_identity",
            "verifier_identity",
            "case_map_identity",
        ):
            _identity(getattr(self, name), name)
        _identities(self.generated_tree_identities, "generated_tree_identities")
        if not self.cases or any(
            not isinstance(item, QualificationParityCaseEvidence) for item in self.cases
        ):
            _error(
                "qualification_lifecycle.parity_invalid", "parity cases are required"
            )
        keys = tuple((item.case_id, item.case_identity.uri) for item in self.cases)
        if keys != tuple(sorted(set(keys))) or len({item[0] for item in keys}) != len(
            keys
        ):
            _error(
                "qualification_lifecycle.parity_invalid",
                "parity cases must be sorted and unique",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "run_identity": self.run_identity.to_dict(),
            "source_snapshot_identity": self.source_snapshot_identity.to_dict(),
            "generated_tree_identities": [
                item.to_dict() for item in self.generated_tree_identities
            ],
            "verifier_identity": self.verifier_identity.to_dict(),
            "case_map_identity": self.case_map_identity.to_dict(),
            "cases": [item.to_dict() for item in self.cases],
        }

    @classmethod
    def from_dict(cls, value: object) -> QualificationParityEvidence:
        names = frozenset(
            {
                "run_identity",
                "source_snapshot_identity",
                "generated_tree_identities",
                "verifier_identity",
                "case_map_identity",
                "cases",
            }
        )
        data = _fields(value, cls.SCHEMA, names)
        trees, cases = data["generated_tree_identities"], data["cases"]
        if not isinstance(trees, list) or not isinstance(cases, list):
            _error(
                "qualification_lifecycle.wire_invalid", "trees and cases must be arrays"
            )
        return cls(
            _parse_identity(data["run_identity"], "run_identity"),
            _parse_identity(
                data["source_snapshot_identity"], "source_snapshot_identity"
            ),
            tuple(_parse_identity(item, "generated_tree_identities") for item in trees),
            _parse_identity(data["verifier_identity"], "verifier_identity"),
            _parse_identity(data["case_map_identity"], "case_map_identity"),
            tuple(QualificationParityCaseEvidence.from_dict(item) for item in cases),
        )


@dataclass(frozen=True, slots=True)
class QualificationLifecycleRunEvidence:
    """Complete host-path-free qualification evidence for one Standard rebuild."""

    run_identity: ContentIdentity
    target_profile_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    specification_set_identity: ContentIdentity
    source_snapshot_identity: ContentIdentity
    generation_input_audit_identity: ContentIdentity
    promotion_tree_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity
    driver_identity: ContentIdentity
    lifecycle_policy_identity: ContentIdentity
    framework_distribution_identity: ContentIdentity
    lifecycle_request_identity: ContentIdentity
    lifecycle_invocation_identity: ContentIdentity
    lifecycle_result_identity: ContentIdentity
    project_receipt_identity: ContentIdentity
    source_tree_identities: tuple[ContentIdentity, ...]
    source_index_identities: tuple[ContentIdentity, ...]
    build_evidence_identities: tuple[ContentIdentity, ...]
    resolved_sbom_identities: tuple[ContentIdentity, ...]
    generated_test_suite_identities: tuple[ContentIdentity, ...]
    generated_test_evidence_identities: tuple[ContentIdentity, ...]
    generated_test_case_identities: tuple[ContentIdentity, ...]
    acceptance_evidence_identities: tuple[ContentIdentity, ...]
    cache_decision_identities: tuple[ContentIdentity, ...]
    node_workspace_identities: tuple[ContentIdentity, ...]
    parity_evidence_identity: ContentIdentity
    generated_test_total: int
    covered_surface_ids: tuple[str, ...]

    SCHEMA: ClassVar[str] = _PREFIX + "qualification-lifecycle-run-evidence"

    def __post_init__(self) -> None:
        scalar_identities = (
            "run_identity",
            "target_profile_identity",
            "component_lock_identity",
            "specification_set_identity",
            "source_snapshot_identity",
            "generation_input_audit_identity",
            "promotion_tree_identity",
            "workspace_allocation_identity",
            "driver_identity",
            "lifecycle_policy_identity",
            "framework_distribution_identity",
            "lifecycle_request_identity",
            "lifecycle_invocation_identity",
            "lifecycle_result_identity",
            "project_receipt_identity",
            "parity_evidence_identity",
        )
        for name in scalar_identities:
            _identity(getattr(self, name), name)
        sequence_names = (
            "source_tree_identities",
            "source_index_identities",
            "build_evidence_identities",
            "resolved_sbom_identities",
            "generated_test_suite_identities",
            "generated_test_evidence_identities",
            "generated_test_case_identities",
            "acceptance_evidence_identities",
            "cache_decision_identities",
            "node_workspace_identities",
        )
        for name in sequence_names:
            _identities(getattr(self, name), name)
        if type(self.generated_test_total) is not int or self.generated_test_total < 1:
            _error(
                "qualification_lifecycle.test_total_invalid",
                "generated_test_total must be a positive derived integer",
            )
        _texts(self.covered_surface_ids, "covered_surface_ids", allow_empty=True)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"schema": self.SCHEMA}
        for name in (
            "run_identity",
            "target_profile_identity",
            "component_lock_identity",
            "specification_set_identity",
            "source_snapshot_identity",
            "generation_input_audit_identity",
            "promotion_tree_identity",
            "workspace_allocation_identity",
            "driver_identity",
            "lifecycle_policy_identity",
            "framework_distribution_identity",
            "lifecycle_request_identity",
            "lifecycle_invocation_identity",
            "lifecycle_result_identity",
            "project_receipt_identity",
            "parity_evidence_identity",
        ):
            result[name] = getattr(self, name).to_dict()
        for name in (
            "source_tree_identities",
            "source_index_identities",
            "build_evidence_identities",
            "resolved_sbom_identities",
            "generated_test_suite_identities",
            "generated_test_evidence_identities",
            "generated_test_case_identities",
            "acceptance_evidence_identities",
            "cache_decision_identities",
            "node_workspace_identities",
        ):
            result[name] = [item.to_dict() for item in getattr(self, name)]
        result["generated_test_total"] = self.generated_test_total
        result["covered_surface_ids"] = list(self.covered_surface_ids)
        return result

    @classmethod
    def from_dict(cls, value: object) -> QualificationLifecycleRunEvidence:
        scalar_names = (
            "run_identity",
            "target_profile_identity",
            "component_lock_identity",
            "specification_set_identity",
            "source_snapshot_identity",
            "generation_input_audit_identity",
            "promotion_tree_identity",
            "workspace_allocation_identity",
            "driver_identity",
            "lifecycle_policy_identity",
            "framework_distribution_identity",
            "lifecycle_request_identity",
            "lifecycle_invocation_identity",
            "lifecycle_result_identity",
            "project_receipt_identity",
            "parity_evidence_identity",
        )
        sequence_names = (
            "source_tree_identities",
            "source_index_identities",
            "build_evidence_identities",
            "resolved_sbom_identities",
            "generated_test_suite_identities",
            "generated_test_evidence_identities",
            "generated_test_case_identities",
            "acceptance_evidence_identities",
            "cache_decision_identities",
            "node_workspace_identities",
        )
        data = _fields(
            value,
            cls.SCHEMA,
            frozenset(
                (
                    *scalar_names,
                    *sequence_names,
                    "generated_test_total",
                    "covered_surface_ids",
                )
            ),
        )
        parsed: dict[str, object] = {
            name: _parse_identity(data[name], name) for name in scalar_names
        }
        for name in sequence_names:
            raw = data[name]
            if not isinstance(raw, list):
                _error(
                    "qualification_lifecycle.wire_invalid", f"{name} must be an array"
                )
            parsed[name] = tuple(_parse_identity(item, name) for item in raw)
        surfaces = data["covered_surface_ids"]
        if not isinstance(surfaces, list):
            _error(
                "qualification_lifecycle.wire_invalid",
                "covered_surface_ids must be an array",
            )
        parsed["covered_surface_ids"] = tuple(surfaces)
        parsed["generated_test_total"] = data["generated_test_total"]
        return cls(**parsed)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class QualificationWorkspaceAllocation:
    allocation_identity: ContentIdentity
    empty_at_allocation: bool

    def __post_init__(self) -> None:
        _identity(self.allocation_identity, "workspace allocation")
        if type(self.empty_at_allocation) is not bool:
            _error(
                "qualification_lifecycle.workspace_invalid",
                "workspace allocation state must be a bool",
            )


@dataclass(frozen=True, slots=True)
class QualificationLifecycleRequest:
    run_identity: ContentIdentity
    target_profile_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    specification_set_identity: ContentIdentity
    source_snapshot_identity: ContentIdentity
    generation_input_audit_identity: ContentIdentity
    promotion_tree_identity: ContentIdentity
    workspace: QualificationWorkspaceAllocation
    force_regeneration: bool = True

    def __post_init__(self) -> None:
        for name in (
            "run_identity",
            "target_profile_identity",
            "component_lock_identity",
            "specification_set_identity",
            "source_snapshot_identity",
            "generation_input_audit_identity",
            "promotion_tree_identity",
        ):
            _identity(getattr(self, name), name)
        if not isinstance(self.workspace, QualificationWorkspaceAllocation):
            _error(
                "qualification_lifecycle.workspace_invalid", "workspace must be typed"
            )
        if self.force_regeneration is not True:
            _error(
                "qualification_lifecycle.cache_not_bypassed",
                "qualification lifecycle must force source regeneration",
            )


@dataclass(frozen=True, slots=True)
class QualificationLifecycleExecution:
    lifecycle: StandardProjectLifecycleResult
    receipt: ProjectTestReceipt
    lifecycle_request_identity: ContentIdentity
    lifecycle_invocation_identity: ContentIdentity
    driver_identity: ContentIdentity
    lifecycle_policy_identity: ContentIdentity
    framework_distribution_identity: ContentIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle, StandardProjectLifecycleResult):
            _error(
                "qualification_lifecycle.result_invalid",
                "lifecycle execution must return a typed Standard result",
            )
        if not isinstance(self.receipt, ProjectTestReceipt):
            _error(
                "qualification_lifecycle.receipt_invalid",
                "lifecycle execution must return a typed project receipt",
            )
        for name in (
            "lifecycle_request_identity",
            "lifecycle_invocation_identity",
            "driver_identity",
            "lifecycle_policy_identity",
            "framework_distribution_identity",
        ):
            _identity(getattr(self, name), name)


class QualificationWorkspaceAllocator(Protocol):
    def allocate(
        self, run_identity: ContentIdentity
    ) -> QualificationWorkspaceAllocation: ...


class QualificationLifecyclePort(Protocol):
    def execute(
        self, request: QualificationLifecycleRequest
    ) -> QualificationLifecycleExecution: ...


class QualificationParityVerifier(Protocol):
    @property
    def provider_identity(self) -> ContentIdentity: ...

    def verify(
        self,
        *,
        run_identity: ContentIdentity,
        source_snapshot_identity: ContentIdentity,
        generated_tree_identities: tuple[ContentIdentity, ...],
        case_map: QualificationVerifierCaseMap,
    ) -> QualificationParityEvidence: ...


@dataclass(frozen=True, slots=True)
class QualificationLifecyclePlan:
    target_profile_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    specification_set_identity: ContentIdentity
    source_snapshot_identity: ContentIdentity
    generation_input_audit_identity: ContentIdentity
    promotion_tree_identity: ContentIdentity
    run_identities: tuple[ContentIdentity, ...]
    case_map: QualificationVerifierCaseMap

    def __post_init__(self) -> None:
        for name in (
            "target_profile_identity",
            "component_lock_identity",
            "specification_set_identity",
            "source_snapshot_identity",
            "generation_input_audit_identity",
            "promotion_tree_identity",
        ):
            _identity(getattr(self, name), name)
        runs = _identities(self.run_identities, "run_identities")
        if len(runs) < 2:
            _error(
                "qualification_lifecycle.runs_insufficient",
                "lifecycle qualification requires at least two clean runs",
            )
        if not isinstance(self.case_map, QualificationVerifierCaseMap):
            _error("qualification_lifecycle.case_map_invalid", "case map must be typed")


@dataclass(frozen=True, slots=True)
class QualificationLifecycleResult:
    runs: tuple[QualificationLifecycleRunEvidence, ...]
    case_map: QualificationVerifierCaseMap
    parity_evidence: tuple[QualificationParityEvidence, ...]

    SCHEMA: ClassVar[str] = _PREFIX + "qualification-lifecycle-result"

    def __post_init__(self) -> None:
        if not isinstance(self.case_map, QualificationVerifierCaseMap):
            _error("qualification_lifecycle.case_map_invalid", "case map must be typed")
        if len(self.runs) < 2 or any(
            not isinstance(item, QualificationLifecycleRunEvidence)
            for item in self.runs
        ):
            _error(
                "qualification_lifecycle.runs_insufficient",
                "result requires at least two typed lifecycle runs",
            )
        if len(self.parity_evidence) != len(self.runs) or any(
            not isinstance(item, QualificationParityEvidence)
            for item in self.parity_evidence
        ):
            _error(
                "qualification_lifecycle.parity_invalid",
                "result requires one typed parity record per lifecycle run",
            )
        run_ids = tuple(item.run_identity.uri for item in self.runs)
        workspace_ids = tuple(
            item.workspace_allocation_identity.uri for item in self.runs
        )
        node_workspace_ids = tuple(
            workspace.uri
            for item in self.runs
            for workspace in item.node_workspace_identities
        )
        if len(set(run_ids)) != len(run_ids):
            _error(
                "qualification_lifecycle.run_reused", "run identities must be distinct"
            )
        if run_ids != tuple(sorted(run_ids)):
            _error(
                "qualification_lifecycle.runs_noncanonical",
                "qualification runs must be sorted by identity",
            )
        parity_run_ids = tuple(item.run_identity.uri for item in self.parity_evidence)
        if parity_run_ids != run_ids:
            _error(
                "qualification_lifecycle.parity_mismatch",
                "parity records must follow and bind the exact lifecycle runs",
            )
        for run, parity in zip(self.runs, self.parity_evidence, strict=True):
            expected_cases = {
                (item.case_id, item.case_identity) for item in self.case_map.cases
            }
            observed_cases = {
                (item.case_id, item.case_identity) for item in parity.cases
            }
            if (
                run.parity_evidence_identity != parity.identity
                or parity.case_map_identity != self.case_map.identity
                or parity.verifier_identity != self.case_map.verifier_identity
                or run.source_snapshot_identity != parity.source_snapshot_identity
                or run.source_tree_identities != parity.generated_tree_identities
                or observed_cases != expected_cases
                or any(not item.passed for item in parity.cases)
                or run.covered_surface_ids != self.case_map.surface_ids
            ):
                _error(
                    "qualification_lifecycle.parity_mismatch",
                    "result parity membership differs from its exact run and case map",
                )
        if len(set(workspace_ids)) != len(workspace_ids) or len(
            set(node_workspace_ids)
        ) != len(node_workspace_ids):
            _error(
                "qualification_lifecycle.workspace_reused",
                "qualification workspaces must be distinct across clean runs",
            )
        for name in (
            "target_profile_identity",
            "component_lock_identity",
            "specification_set_identity",
            "source_snapshot_identity",
            "generation_input_audit_identity",
            "promotion_tree_identity",
            "driver_identity",
            "lifecycle_policy_identity",
            "framework_distribution_identity",
        ):
            values = {getattr(item, name) for item in self.runs}
            if len(values) != 1:
                _error(
                    "qualification_lifecycle.run_binding_mismatch",
                    "clean runs must retain one exact target, authority, Standard "
                    "binding, and independently covered surface set",
                )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "runs": [item.to_dict() for item in self.runs],
            "case_map": self.case_map.to_dict(),
            "parity_evidence": [item.to_dict() for item in self.parity_evidence],
        }

    @classmethod
    def from_dict(cls, value: object) -> QualificationLifecycleResult:
        data = _fields(
            value,
            cls.SCHEMA,
            frozenset({"runs", "case_map", "parity_evidence"}),
        )
        runs, parity = data["runs"], data["parity_evidence"]
        if not isinstance(runs, list) or not isinstance(parity, list):
            _error(
                "qualification_lifecycle.wire_invalid",
                "runs and parity_evidence must be arrays",
            )
        return cls(
            tuple(QualificationLifecycleRunEvidence.from_dict(item) for item in runs),
            QualificationVerifierCaseMap.from_dict(data["case_map"]),
            tuple(QualificationParityEvidence.from_dict(item) for item in parity),
        )


class QualificationLifecycleRunner:
    """Execute clean Standard rebuilds and derive qualification evidence."""

    def __init__(
        self,
        *,
        workspaces: QualificationWorkspaceAllocator,
        lifecycle: QualificationLifecyclePort,
        parity_verifier: QualificationParityVerifier,
    ) -> None:
        self.workspaces = workspaces
        self.lifecycle = lifecycle
        self.parity_verifier = parity_verifier

    def run(self, plan: QualificationLifecyclePlan) -> QualificationLifecycleResult:
        if not isinstance(plan, QualificationLifecyclePlan):
            raise TypeError("plan must be a QualificationLifecyclePlan")
        verifier_identity = _identity(
            self.parity_verifier.provider_identity, "parity verifier identity"
        )
        if verifier_identity != plan.case_map.verifier_identity:
            _error(
                "qualification_lifecycle.verifier_mismatch",
                "parity verifier differs from the pinned case map",
            )
        allocations: set[str] = set()
        node_allocations: set[str] = set()
        evidence: list[QualificationLifecycleRunEvidence] = []
        parity_records: list[QualificationParityEvidence] = []
        for run_identity in plan.run_identities:
            workspace = self.workspaces.allocate(run_identity)
            if not isinstance(workspace, QualificationWorkspaceAllocation):
                _error(
                    "qualification_lifecycle.workspace_invalid",
                    "workspace allocator returned an invalid value",
                )
            if not workspace.empty_at_allocation:
                _error(
                    "qualification_lifecycle.workspace_not_empty",
                    "qualification workspace was not empty at allocation",
                )
            if workspace.allocation_identity.uri in allocations:
                _error(
                    "qualification_lifecycle.workspace_reused",
                    "workspace allocation was reused across qualification runs",
                )
            allocations.add(workspace.allocation_identity.uri)
            request = QualificationLifecycleRequest(
                run_identity,
                plan.target_profile_identity,
                plan.component_lock_identity,
                plan.specification_set_identity,
                plan.source_snapshot_identity,
                plan.generation_input_audit_identity,
                plan.promotion_tree_identity,
                workspace,
            )
            execution = self.lifecycle.execute(request)
            if not isinstance(execution, QualificationLifecycleExecution):
                _error(
                    "qualification_lifecycle.result_invalid",
                    "lifecycle port returned an invalid execution",
                )
            derived = self._derive_standard(execution)
            overlap = node_allocations.intersection(item.uri for item in derived[9])
            if overlap:
                _error(
                    "qualification_lifecycle.workspace_reused",
                    "a Component generation workspace was reused across clean runs",
                )
            node_allocations.update(item.uri for item in derived[9])
            generated_trees = derived[0]
            parity = self.parity_verifier.verify(
                run_identity=run_identity,
                source_snapshot_identity=plan.source_snapshot_identity,
                generated_tree_identities=generated_trees,
                case_map=plan.case_map,
            )
            covered = self._covered_surfaces(
                plan, parity, generated_trees, run_identity
            )
            parity_records.append(parity)
            evidence.append(
                QualificationLifecycleRunEvidence(
                    run_identity,
                    plan.target_profile_identity,
                    plan.component_lock_identity,
                    plan.specification_set_identity,
                    plan.source_snapshot_identity,
                    plan.generation_input_audit_identity,
                    plan.promotion_tree_identity,
                    workspace.allocation_identity,
                    execution.driver_identity,
                    execution.lifecycle_policy_identity,
                    execution.framework_distribution_identity,
                    execution.lifecycle_request_identity,
                    execution.lifecycle_invocation_identity,
                    execution.lifecycle.identity,
                    execution.receipt.identity,
                    *derived,
                    parity.identity,
                    sum(
                        len(node.generated_test_evidence.cases)  # type: ignore[union-attr]
                        for node in execution.lifecycle.node_results
                    ),
                    covered,
                )
            )
        return QualificationLifecycleResult(
            tuple(evidence), plan.case_map, tuple(parity_records)
        )

    @staticmethod
    def _derive_standard(
        execution: QualificationLifecycleExecution,
    ) -> tuple[
        tuple[ContentIdentity, ...],
        tuple[ContentIdentity, ...],
        tuple[ContentIdentity, ...],
        tuple[ContentIdentity, ...],
        tuple[ContentIdentity, ...],
        tuple[ContentIdentity, ...],
        tuple[ContentIdentity, ...],
        tuple[ContentIdentity, ...],
        tuple[ContentIdentity, ...],
        tuple[ContentIdentity, ...],
    ]:
        lifecycle, receipt = execution.lifecycle, execution.receipt
        if not lifecycle.successful or lifecycle.aggregate_receipt is None:
            _error(
                "qualification_lifecycle.lifecycle_failed",
                "qualification requires a successful accepted Standard lifecycle",
            )
        if (
            receipt.result_identity != lifecycle.identity
            or receipt.subject_identity != lifecycle.aggregate_receipt.identity
        ):
            _error(
                "qualification_lifecycle.receipt_mismatch",
                "project receipt does not finalize the exact lifecycle result",
            )
        evidence_kinds = {item.kind for item in receipt.evidence}
        if evidence_kinds != set(STANDARD_FULL_REBUILD_EVIDENCE_KINDS):
            _error(
                "qualification_lifecycle.receipt_incomplete",
                "project receipt omits complete Standard lifecycle evidence",
            )
        membership = lifecycle.lifecycle_membership
        if not isinstance(membership, StandardProjectLifecycleMembership):
            _error(
                "qualification_lifecycle.cache_invalid",
                "lifecycle cache membership must be typed",
            )
        if any(
            item.outcome is StandardNodeCacheOutcome.HIT
            for item in membership.cache_decisions
        ):
            _error(
                "qualification_lifecycle.source_cache_hit",
                "source-cache hits cannot establish regeneration",
            )
        nodes = lifecycle.node_results
        if not nodes:
            _error("qualification_lifecycle.nodes_empty", "lifecycle has no Components")
        for node in nodes:
            if node.disposition is not SourceGenerationDisposition.GENERATED:
                _error(
                    "qualification_lifecycle.source_reused",
                    "qualification requires freshly generated source for every "
                    "Component",
                )
            if (
                node.source_output is None
                or node.index_identity is None
                or not isinstance(node.build_evidence, StandardBuildEvidence)
                or not isinstance(
                    node.generated_test_evidence, StandardGeneratedTestExecutionEvidence
                )
                or not isinstance(
                    node.acceptance_evidence, StandardComponentAcceptanceEvidence
                )
            ):
                _error(
                    "qualification_lifecycle.evidence_incomplete",
                    "lifecycle node omits strict source, index, build, test, or "
                    "acceptance evidence",
                )
        generated_total = sum(
            len(node.generated_test_evidence.cases)  # type: ignore[union-attr]
            for node in nodes
        )
        if receipt.summary.total != generated_total:
            _error(
                "qualification_lifecycle.test_total_mismatch",
                "receipt total differs from exact generated case membership",
            )

        def ordered(values: object) -> tuple[ContentIdentity, ...]:
            return tuple(sorted(values, key=lambda item: item.uri))  # type: ignore[arg-type,union-attr]

        return (
            ordered(node.source_output.candidate.tree_identity for node in nodes),  # type: ignore[union-attr]
            ordered(node.index_identity for node in nodes),
            ordered(node.build_evidence.identity for node in nodes),  # type: ignore[union-attr]
            ordered(node.build_evidence.resolved_sbom.bom_identity for node in nodes),  # type: ignore[union-attr]
            ordered(
                node.generated_test_evidence.generated_test_suite_identity
                for node in nodes
            ),  # type: ignore[union-attr]
            ordered(node.generated_test_evidence.identity for node in nodes),  # type: ignore[union-attr]
            ordered(
                case.case_identity
                for node in nodes
                for case in node.generated_test_evidence.cases  # type: ignore[union-attr]
            ),
            ordered(node.acceptance_evidence.identity for node in nodes),  # type: ignore[union-attr]
            ordered(item.identity for item in membership.cache_decisions),
            ordered(
                node.source_output.candidate.workspace_allocation_identity  # type: ignore[union-attr]
                for node in nodes
            ),
        )

    @staticmethod
    def _covered_surfaces(
        plan: QualificationLifecyclePlan,
        parity: QualificationParityEvidence,
        generated_trees: tuple[ContentIdentity, ...],
        run_identity: ContentIdentity,
    ) -> tuple[str, ...]:
        if not isinstance(parity, QualificationParityEvidence):
            _error(
                "qualification_lifecycle.parity_invalid",
                "parity verifier returned invalid evidence",
            )
        if (
            parity.run_identity != run_identity
            or parity.source_snapshot_identity != plan.source_snapshot_identity
            or parity.generated_tree_identities != generated_trees
            or parity.verifier_identity != plan.case_map.verifier_identity
            or parity.case_map_identity != plan.case_map.identity
        ):
            _error(
                "qualification_lifecycle.parity_mismatch",
                "parity evidence does not bind the exact run, source, trees, "
                "verifier, and map",
            )
        expected = {
            (item.case_id, item.case_identity): item for item in plan.case_map.cases
        }
        observed = {(item.case_id, item.case_identity): item for item in parity.cases}
        if observed.keys() != expected.keys():
            _error(
                "qualification_lifecycle.parity_cases_mismatch",
                "parity evidence must execute every and only pinned verifier case",
            )
        if any(not case.passed for case in observed.values()):
            _error(
                "qualification_lifecycle.parity_failed",
                "every pinned independent parity case must pass",
            )
        return tuple(
            sorted(
                {
                    surface
                    for key, case in observed.items()
                    if case.passed
                    for surface in expected[key].surface_ids
                }
            )
        )


__all__ = [
    "QualificationCaseSurfaceBinding",
    "QualificationLifecycleExecution",
    "QualificationLifecyclePlan",
    "QualificationLifecyclePort",
    "QualificationLifecycleRequest",
    "QualificationLifecycleResult",
    "QualificationLifecycleRunEvidence",
    "QualificationLifecycleRunner",
    "QualificationParityCaseEvidence",
    "QualificationParityEvidence",
    "QualificationParityVerifier",
    "QualificationVerifierCaseMap",
    "QualificationWorkspaceAllocation",
    "QualificationWorkspaceAllocator",
]
