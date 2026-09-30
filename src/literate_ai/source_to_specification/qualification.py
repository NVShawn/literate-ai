"""Regenerative qualification of a reviewed source-derived specification."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .contracts import canonical_digest
from .errors import SourceToSpecificationError

_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCHEMA_PREFIX = "urn:literate-ai:schema:v2:"
REGENERATIVE_QUALIFICATION_POLICY_SCHEMA = (
    f"{_SCHEMA_PREFIX}regenerative-qualification-policy"
)
CLEAN_REGENERATION_EVIDENCE_SCHEMA = f"{_SCHEMA_PREFIX}clean-regeneration-evidence"
REGENERATIVE_QUALIFICATION_DECISION_SCHEMA = (
    f"{_SCHEMA_PREFIX}regenerative-qualification-decision"
)
LEGACY_QUALIFICATION_BLOCKER = "qualification-v2-required"


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise SourceToSpecificationError(
            "qualification.identity_invalid",
            f"{field} must be an exact sha256 content identity",
        )
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SourceToSpecificationError(
            "qualification.text_invalid", f"{field} must be a string"
        )
    normalized = value.strip()
    if not normalized:
        raise SourceToSpecificationError(
            "qualification.text_invalid", f"{field} must not be empty"
        )
    return normalized


def _unique(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    normalized = tuple(_text(value, field) for value in values)
    if len(normalized) != len(set(normalized)):
        raise SourceToSpecificationError(
            "qualification.value_duplicate", f"{field} must be unique"
        )
    return tuple(sorted(normalized))


def _wire_fields(
    value: Any,
    *,
    path: str,
    schema: str,
    required: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SourceToSpecificationError(
            "qualification.wire_invalid", f"{path} must be an object"
        )
    keys = frozenset(value)
    expected = required | {"schema"}
    missing = expected - keys
    unknown = keys - expected
    if missing:
        raise SourceToSpecificationError(
            "qualification.wire_invalid",
            f"{path} is missing: {', '.join(sorted(missing))}",
        )
    if unknown:
        raise SourceToSpecificationError(
            "qualification.wire_invalid",
            f"{path} has unknown fields: {', '.join(sorted(unknown))}",
        )
    if value["schema"] != schema:
        raise SourceToSpecificationError(
            "qualification.schema_unsupported",
            f"{path}.schema must be {schema!r}",
        )
    return value


def _wire_text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise SourceToSpecificationError(
            "qualification.wire_invalid", f"{path} must be a string"
        )
    return _text(value, path)


def _wire_identity(value: Any, path: str) -> str:
    return _identity(_wire_text(value, path), path)


def _wire_string_tuple(value: Any, path: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise SourceToSpecificationError(
            "qualification.wire_invalid", f"{path} must be an array"
        )
    return tuple(
        _wire_text(item, f"{path}[{index}]") for index, item in enumerate(value)
    )


def _wire_identity_tuple(value: Any, path: str) -> tuple[str, ...]:
    return tuple(_identity(item, path) for item in _wire_string_tuple(value, path))


def _wire_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise SourceToSpecificationError(
            "qualification.wire_invalid", f"{path} must be a boolean"
        )
    return value


def _wire_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceToSpecificationError(
            "qualification.wire_invalid", f"{path} must be an integer"
        )
    return value


class RegenerativeAuthority(StrEnum):
    """Which artifact remains the implementation authority after evaluation."""

    SOURCE_BASELINE = "source-baseline"
    SPECIFICATION = "specification"


@dataclass(frozen=True, slots=True)
class RegenerativeQualificationPolicy:
    """Explicit sufficiency rule; no model confidence score can replace it."""

    policy_id: str
    minimum_clean_runs: int
    required_target_profile_ids: tuple[str, ...]
    required_surface_ids: tuple[str, ...]
    allow_skipped_tests: bool = False

    SCHEMA: ClassVar[str] = REGENERATIVE_QUALIFICATION_POLICY_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _text(self.policy_id, "policy_id"))
        if (
            isinstance(self.minimum_clean_runs, bool)
            or not isinstance(self.minimum_clean_runs, int)
            or self.minimum_clean_runs < 2
        ):
            raise SourceToSpecificationError(
                "qualification.policy_invalid",
                "minimum_clean_runs must be an integer of at least two",
            )
        required_target_profile_ids = tuple(
            _identity(value, "required_target_profile_ids")
            for value in _unique(
                self.required_target_profile_ids,
                "required_target_profile_ids",
            )
        )
        if not required_target_profile_ids:
            raise SourceToSpecificationError(
                "qualification.policy_invalid",
                "required_target_profile_ids must contain at least one target",
            )
        required_surface_ids = _unique(
            self.required_surface_ids, "required_surface_ids"
        )
        if not required_surface_ids:
            raise SourceToSpecificationError(
                "qualification.policy_invalid",
                "required_surface_ids must contain at least one surface",
            )
        object.__setattr__(
            self, "required_target_profile_ids", required_target_profile_ids
        )
        object.__setattr__(self, "required_surface_ids", required_surface_ids)
        if not isinstance(self.allow_skipped_tests, bool):
            raise SourceToSpecificationError(
                "qualification.policy_invalid",
                "allow_skipped_tests must be a boolean",
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "policy_id": self.policy_id,
            "minimum_clean_runs": self.minimum_clean_runs,
            "required_target_profile_ids": list(self.required_target_profile_ids),
            "required_surface_ids": list(self.required_surface_ids),
            "allow_skipped_tests": self.allow_skipped_tests,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RegenerativeQualificationPolicy"
    ) -> RegenerativeQualificationPolicy:
        data = _wire_fields(
            value,
            path=path,
            schema=cls.SCHEMA,
            required=frozenset(
                {
                    "policy_id",
                    "minimum_clean_runs",
                    "required_target_profile_ids",
                    "required_surface_ids",
                    "allow_skipped_tests",
                }
            ),
        )
        return cls(
            policy_id=_wire_text(data["policy_id"], f"{path}.policy_id"),
            minimum_clean_runs=_wire_int(
                data["minimum_clean_runs"], f"{path}.minimum_clean_runs"
            ),
            required_target_profile_ids=_wire_identity_tuple(
                data["required_target_profile_ids"],
                f"{path}.required_target_profile_ids",
            ),
            required_surface_ids=_wire_string_tuple(
                data["required_surface_ids"], f"{path}.required_surface_ids"
            ),
            allow_skipped_tests=_wire_bool(
                data["allow_skipped_tests"], f"{path}.allow_skipped_tests"
            ),
        )


@dataclass(frozen=True, slots=True)
class CleanRegenerationEvidence:
    """One measured legacy run retained as non-authorizing historical evidence."""

    run_id: str
    run_attestation_id: str
    source_snapshot_id: str
    specification_set_id: str
    target_profile_id: str
    flavor_lock_id: str
    generation_recipe_id: str
    generated_tree_id: str
    build_result_id: str
    generated_test_result_id: str
    independent_parity_result_id: str
    covered_surface_ids: tuple[str, ...]
    source_excluded_from_generation: bool
    empty_workspace: bool
    generated_source_cache_hit: bool
    build_passed: bool
    generated_tests_passed: bool
    independent_parity_passed: bool
    generated_tests_total: int
    generated_tests_succeeded: int
    generated_tests_failed: int
    generated_tests_skipped: int

    SCHEMA: ClassVar[str] = CLEAN_REGENERATION_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for field in (
            "run_id",
            "run_attestation_id",
            "source_snapshot_id",
            "specification_set_id",
            "target_profile_id",
            "flavor_lock_id",
            "generation_recipe_id",
            "generated_tree_id",
            "build_result_id",
            "generated_test_result_id",
            "independent_parity_result_id",
        ):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        object.__setattr__(
            self,
            "covered_surface_ids",
            _unique(self.covered_surface_ids, "covered_surface_ids"),
        )
        for field in (
            "source_excluded_from_generation",
            "empty_workspace",
            "generated_source_cache_hit",
            "build_passed",
            "generated_tests_passed",
            "independent_parity_passed",
        ):
            if not isinstance(getattr(self, field), bool):
                raise SourceToSpecificationError(
                    "qualification.evidence_invalid", f"{field} must be a boolean"
                )
        for field in (
            "generated_tests_total",
            "generated_tests_succeeded",
            "generated_tests_failed",
            "generated_tests_skipped",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SourceToSpecificationError(
                    "qualification.evidence_invalid",
                    f"{field} must be a non-negative integer",
                )
        if self.generated_tests_total < 1 or (
            self.generated_tests_succeeded
            + self.generated_tests_failed
            + self.generated_tests_skipped
            != self.generated_tests_total
        ):
            raise SourceToSpecificationError(
                "qualification.test_accounting_invalid",
                "test counts must completely account for a non-empty suite",
            )
        if self.generated_tests_passed != (self.generated_tests_failed == 0):
            raise SourceToSpecificationError(
                "qualification.test_outcome_invalid",
                "generated_tests_passed must agree with the generated-test counts",
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "run_attestation_id": self.run_attestation_id,
            "source_snapshot_id": self.source_snapshot_id,
            "specification_set_id": self.specification_set_id,
            "target_profile_id": self.target_profile_id,
            "flavor_lock_id": self.flavor_lock_id,
            "generation_recipe_id": self.generation_recipe_id,
            "generated_tree_id": self.generated_tree_id,
            "build_result_id": self.build_result_id,
            "generated_test_result_id": self.generated_test_result_id,
            "independent_parity_result_id": self.independent_parity_result_id,
            "covered_surface_ids": list(self.covered_surface_ids),
            "source_excluded_from_generation": self.source_excluded_from_generation,
            "empty_workspace": self.empty_workspace,
            "generated_source_cache_hit": self.generated_source_cache_hit,
            "build_passed": self.build_passed,
            "generated_tests_passed": self.generated_tests_passed,
            "independent_parity_passed": self.independent_parity_passed,
            "generated_tests_total": self.generated_tests_total,
            "generated_tests_succeeded": self.generated_tests_succeeded,
            "generated_tests_failed": self.generated_tests_failed,
            "generated_tests_skipped": self.generated_tests_skipped,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CleanRegenerationEvidence"
    ) -> CleanRegenerationEvidence:
        identity_fields = (
            "run_id",
            "run_attestation_id",
            "source_snapshot_id",
            "specification_set_id",
            "target_profile_id",
            "flavor_lock_id",
            "generation_recipe_id",
            "generated_tree_id",
            "build_result_id",
            "generated_test_result_id",
            "independent_parity_result_id",
        )
        boolean_fields = (
            "source_excluded_from_generation",
            "empty_workspace",
            "generated_source_cache_hit",
            "build_passed",
            "generated_tests_passed",
            "independent_parity_passed",
        )
        integer_fields = (
            "generated_tests_total",
            "generated_tests_succeeded",
            "generated_tests_failed",
            "generated_tests_skipped",
        )
        data = _wire_fields(
            value,
            path=path,
            schema=cls.SCHEMA,
            required=frozenset(
                (
                    *identity_fields,
                    *boolean_fields,
                    *integer_fields,
                    "covered_surface_ids",
                )
            ),
        )
        return cls(
            **{
                field: _wire_identity(data[field], f"{path}.{field}")
                for field in identity_fields
            },
            covered_surface_ids=_wire_string_tuple(
                data["covered_surface_ids"], f"{path}.covered_surface_ids"
            ),
            **{
                field: _wire_bool(data[field], f"{path}.{field}")
                for field in boolean_fields
            },
            **{
                field: _wire_int(data[field], f"{path}.{field}")
                for field in integer_fields
            },
        )


@dataclass(frozen=True, slots=True)
class RegenerativeQualificationDecision:
    """A persisted qualification claim, never present authority by itself.

    Versioned wire records remain useful as historical evidence, including records
    whose producer claimed specification authority.  Parsing or constructing this
    value does not verify that claim against the current lifecycle.  A future
    verified-current wrapper must perform that verification before authority can
    transfer.
    """

    source_snapshot_id: str
    specification_set_id: str
    policy_id: str
    policy_identity: str
    evidence_ids: tuple[str, ...]
    claimed_authority: RegenerativeAuthority
    blockers: tuple[str, ...]

    SCHEMA: ClassVar[str] = REGENERATIVE_QUALIFICATION_DECISION_SCHEMA

    def __post_init__(self) -> None:
        for field in (
            "source_snapshot_id",
            "specification_set_id",
            "policy_identity",
        ):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        object.__setattr__(self, "policy_id", _text(self.policy_id, "policy_id"))
        object.__setattr__(
            self,
            "evidence_ids",
            tuple(
                _identity(value, "evidence_ids")
                for value in _unique(self.evidence_ids, "evidence_ids")
            ),
        )
        object.__setattr__(self, "blockers", _unique(self.blockers, "blockers"))
        if not isinstance(self.claimed_authority, RegenerativeAuthority):
            raise SourceToSpecificationError(
                "qualification.decision_invalid", "claimed_authority is invalid"
            )
        claimed_qualified = (
            self.claimed_authority is RegenerativeAuthority.SPECIFICATION
        )
        if claimed_qualified and len(self.evidence_ids) < 2:
            raise SourceToSpecificationError(
                "qualification.decision_invalid",
                "specification authority requires at least two evidence records",
            )
        if claimed_qualified == bool(self.blockers):
            raise SourceToSpecificationError(
                "qualification.decision_invalid",
                "specification authority requires no blockers and source authority "
                "requires at least one blocker",
            )

    @property
    def qualified(self) -> bool:
        """Return effective qualification; persisted claims are never sufficient."""

        return False

    @property
    def effective_authority(self) -> RegenerativeAuthority:
        """Return fail-closed authority until a current verifier wraps the claim."""

        return RegenerativeAuthority.SOURCE_BASELINE

    @property
    def claimed_qualified(self) -> bool:
        """Expose the persisted producer claim for inspection, never authorization."""

        return self.claimed_authority is RegenerativeAuthority.SPECIFICATION

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "source_snapshot_id": self.source_snapshot_id,
            "specification_set_id": self.specification_set_id,
            "policy_id": self.policy_id,
            "policy_identity": self.policy_identity,
            "evidence_ids": list(self.evidence_ids),
            "claimed_authority": self.claimed_authority.value,
            "effective_authority": self.effective_authority.value,
            "blockers": list(self.blockers),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RegenerativeQualificationDecision"
    ) -> RegenerativeQualificationDecision:
        data = _wire_fields(
            value,
            path=path,
            schema=cls.SCHEMA,
            required=frozenset(
                {
                    "source_snapshot_id",
                    "specification_set_id",
                    "policy_id",
                    "policy_identity",
                    "evidence_ids",
                    "claimed_authority",
                    "effective_authority",
                    "blockers",
                }
            ),
        )
        try:
            authority = RegenerativeAuthority(
                _wire_text(data["claimed_authority"], f"{path}.claimed_authority")
            )
        except ValueError as exc:
            raise SourceToSpecificationError(
                "qualification.wire_invalid",
                f"{path}.claimed_authority is unsupported",
            ) from exc
        effective_authority = _wire_text(
            data["effective_authority"], f"{path}.effective_authority"
        )
        if effective_authority != RegenerativeAuthority.SOURCE_BASELINE.value:
            raise SourceToSpecificationError(
                "qualification.wire_invalid",
                f"{path}.effective_authority must remain source-baseline until a "
                "verified-current authority projection exists",
            )
        return cls(
            source_snapshot_id=_wire_identity(
                data["source_snapshot_id"], f"{path}.source_snapshot_id"
            ),
            specification_set_id=_wire_identity(
                data["specification_set_id"], f"{path}.specification_set_id"
            ),
            policy_id=_wire_text(data["policy_id"], f"{path}.policy_id"),
            policy_identity=_wire_identity(
                data["policy_identity"], f"{path}.policy_identity"
            ),
            evidence_ids=_wire_identity_tuple(
                data["evidence_ids"], f"{path}.evidence_ids"
            ),
            claimed_authority=authority,
            blockers=_wire_string_tuple(data["blockers"], f"{path}.blockers"),
        )


def qualify_regenerative_specification(
    *,
    source_snapshot_id: str,
    specification_set_id: str,
    policy: RegenerativeQualificationPolicy,
    evidence: tuple[CleanRegenerationEvidence, ...],
) -> RegenerativeQualificationDecision:
    """Evaluate legacy evidence without transferring implementation authority.

    Version-1 evidence remains useful historical measurement, but its command-level
    test accounting and claimed source-exclusion fields are not sufficient to prove
    source fungibility.  Keep the source baseline authoritative until a version-2
    lifecycle-backed qualification contract is available.
    """

    if not isinstance(policy, RegenerativeQualificationPolicy):
        raise SourceToSpecificationError(
            "qualification.policy_invalid",
            "policy must be a RegenerativeQualificationPolicy",
        )
    if not isinstance(evidence, tuple) or any(
        not isinstance(item, CleanRegenerationEvidence) for item in evidence
    ):
        raise SourceToSpecificationError(
            "qualification.evidence_invalid",
            "evidence must be a tuple of verified CleanRegenerationEvidence records",
        )
    source_snapshot_id = _identity(source_snapshot_id, "source_snapshot_id")
    specification_set_id = _identity(specification_set_id, "specification_set_id")
    run_ids = tuple(item.run_id for item in evidence)
    attestation_ids = tuple(item.run_attestation_id for item in evidence)
    if len(run_ids) != len(set(run_ids)) or len(attestation_ids) != len(
        set(attestation_ids)
    ):
        raise SourceToSpecificationError(
            "qualification.evidence_duplicate",
            "qualification runs and attestations must be independently unique",
        )
    if any(item.source_snapshot_id != source_snapshot_id for item in evidence):
        raise SourceToSpecificationError(
            "qualification.source_mismatch",
            "qualification evidence compares another source snapshot",
        )
    if any(item.specification_set_id != specification_set_id for item in evidence):
        raise SourceToSpecificationError(
            "qualification.specification_mismatch",
            "qualification evidence was generated from another specification set",
        )

    blockers: list[str] = [LEGACY_QUALIFICATION_BLOCKER]
    clean = tuple(
        item
        for item in evidence
        if item.empty_workspace
        and not item.generated_source_cache_hit
        and item.source_excluded_from_generation
    )
    if any(not item.source_excluded_from_generation for item in evidence):
        blockers.append("original-source-entered-generation")
    if any(not item.empty_workspace for item in evidence):
        blockers.append("nonempty-generation-workspace")
    if any(item.generated_source_cache_hit for item in evidence):
        blockers.append("source-cache-used-for-qualification")
    if any(not item.build_passed for item in evidence):
        blockers.append("build-failure")
    if any(not item.generated_tests_passed for item in evidence):
        blockers.append("generated-test-failure")
    if any(not item.independent_parity_passed for item in evidence):
        blockers.append("source-parity-failure")
    if any(item.generated_tests_failed for item in evidence):
        blockers.append("test-failure")
    if not policy.allow_skipped_tests and any(
        item.generated_tests_skipped for item in evidence
    ):
        blockers.append("skipped-tests")
    if any(
        item.generated_tests_succeeded
        + (item.generated_tests_skipped if policy.allow_skipped_tests else 0)
        != item.generated_tests_total
        for item in evidence
    ):
        blockers.append("incomplete-passing-suite")

    for target in policy.required_target_profile_ids:
        target_runs = tuple(item for item in clean if item.target_profile_id == target)
        if not target_runs:
            blockers.append(f"missing-target:{target}")
            continue
        if len(target_runs) < policy.minimum_clean_runs:
            blockers.append(
                f"clean-runs:{target}:{len(target_runs)}/{policy.minimum_clean_runs}"
            )
        observed_surfaces = {
            surface for item in target_runs for surface in item.covered_surface_ids
        }
        for surface in policy.required_surface_ids:
            if surface not in observed_surfaces:
                blockers.append(f"missing-surface:{target}:{surface}")
    normalized_blockers = tuple(sorted(set(blockers)))
    return RegenerativeQualificationDecision(
        source_snapshot_id=source_snapshot_id,
        specification_set_id=specification_set_id,
        policy_id=policy.policy_id,
        policy_identity=policy.identity,
        evidence_ids=tuple(sorted(item.identity for item in evidence)),
        claimed_authority=RegenerativeAuthority.SOURCE_BASELINE,
        blockers=normalized_blockers,
    )


__all__ = [
    "CLEAN_REGENERATION_EVIDENCE_SCHEMA",
    "CleanRegenerationEvidence",
    "LEGACY_QUALIFICATION_BLOCKER",
    "REGENERATIVE_QUALIFICATION_DECISION_SCHEMA",
    "REGENERATIVE_QUALIFICATION_POLICY_SCHEMA",
    "RegenerativeAuthority",
    "RegenerativeQualificationDecision",
    "RegenerativeQualificationPolicy",
    "qualify_regenerative_specification",
]
