"""Operational clean-regeneration and independent-parity qualification."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .contracts import canonical_digest, canonical_value
from .errors import SourceToSpecificationError
from .promotion_materialization import SourceExclusionAssessment
from .qualification import (
    CleanRegenerationEvidence,
    RegenerativeQualificationDecision,
    RegenerativeQualificationPolicy,
    qualify_regenerative_specification,
)

OPERATIONAL_QUALIFICATION_ATTESTATION_SCHEMA = (
    "urn:literate-ai:schema:v2:operational-qualification-attestation"
)
QUALIFICATION_RUN_CHECKPOINT_SCHEMA = (
    "urn:literate-ai:schema:v1:qualification-run-checkpoint"
)
QUALIFICATION_RUN_CHECKPOINT_KEY_SCHEMA = (
    "urn:literate-ai:schema:v1:qualification-run-checkpoint-key"
)


def _identity(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
    ):
        raise SourceToSpecificationError(
            "qualification_runner.identity_invalid",
            f"{field} must be an exact sha256 content identity",
        )
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise SourceToSpecificationError(
            "qualification_runner.identity_invalid",
            f"{field} must be an exact sha256 content identity",
        ) from exc
    return value


def _identities(values: Sequence[str], field: str) -> tuple[str, ...]:
    normalized = tuple(_identity(value, field) for value in values)
    if len(normalized) != len(set(normalized)):
        raise SourceToSpecificationError(
            "qualification_runner.identity_duplicate", f"{field} must be unique"
        )
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class RegenerationRunPlan:
    """Exact spec-only inputs for one clean regeneration."""

    run_id: str
    specification_set_id: str
    target_profile_id: str
    flavor_lock_id: str
    generation_recipe_id: str
    covered_surface_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "run_id",
            "specification_set_id",
            "target_profile_id",
            "flavor_lock_id",
            "generation_recipe_id",
        ):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        surfaces = tuple(sorted(self.covered_surface_ids))
        if (
            not surfaces
            or len(surfaces) != len(set(surfaces))
            or any(not isinstance(item, str) or not item.strip() for item in surfaces)
        ):
            raise SourceToSpecificationError(
                "qualification_runner.surface_invalid",
                "covered_surface_ids must contain unique non-empty values",
            )
        object.__setattr__(self, "covered_surface_ids", surfaces)

    @property
    def declared_generation_inputs(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                (
                    self.specification_set_id,
                    self.flavor_lock_id,
                    self.generation_recipe_id,
                )
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "specification_set_id": self.specification_set_id,
            "target_profile_id": self.target_profile_id,
            "flavor_lock_id": self.flavor_lock_id,
            "generation_recipe_id": self.generation_recipe_id,
            "covered_surface_ids": list(self.covered_surface_ids),
        }


@dataclass(frozen=True, slots=True)
class RegenerationOutcome:
    """Measured build-and-test outcome returned by a spec-only regenerator."""

    generation_input_ids: tuple[str, ...]
    generated_tree_id: str
    build_result_id: str
    generated_test_result_id: str
    generated_source_cache_hit: bool
    build_passed: bool
    generated_tests_total: int
    generated_tests_succeeded: int
    generated_tests_failed: int
    generated_tests_skipped: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generation_input_ids",
            _identities(self.generation_input_ids, "generation_input_ids"),
        )
        for field in (
            "generated_tree_id",
            "build_result_id",
            "generated_test_result_id",
        ):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        for field in ("generated_source_cache_hit", "build_passed"):
            if not isinstance(getattr(self, field), bool):
                raise SourceToSpecificationError(
                    "qualification_runner.outcome_invalid",
                    f"{field} must be a boolean",
                )
        counts = (
            self.generated_tests_total,
            self.generated_tests_succeeded,
            self.generated_tests_failed,
            self.generated_tests_skipped,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise SourceToSpecificationError(
                "qualification_runner.outcome_invalid",
                "generated test counts must be non-negative integers",
            )
        if self.generated_tests_total < 1 or sum(counts[1:]) != counts[0]:
            raise SourceToSpecificationError(
                "qualification_runner.outcome_invalid",
                "generated test counts must account for a non-empty suite",
            )

    @property
    def generated_tests_passed(self) -> bool:
        return self.generated_tests_failed == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "generation_input_ids": list(self.generation_input_ids),
            "generated_tree_id": self.generated_tree_id,
            "build_result_id": self.build_result_id,
            "generated_test_result_id": self.generated_test_result_id,
            "generated_source_cache_hit": self.generated_source_cache_hit,
            "build_passed": self.build_passed,
            "generated_tests_total": self.generated_tests_total,
            "generated_tests_succeeded": self.generated_tests_succeeded,
            "generated_tests_failed": self.generated_tests_failed,
            "generated_tests_skipped": self.generated_tests_skipped,
        }


@dataclass(frozen=True, slots=True)
class ParityVerificationRequest:
    """Inputs exposed to the independent verifier after generation has ended."""

    run_id: str
    source_snapshot_id: str
    specification_set_id: str
    target_profile_id: str
    generated_tree_id: str
    covered_surface_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return canonical_value(self)


@dataclass(frozen=True, slots=True)
class ParityOutcome:
    """Measured observable parity for one generated tree."""

    result_id: str
    passed: bool
    covered_surface_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _identity(self.result_id, "result_id"))
        if not isinstance(self.passed, bool):
            raise SourceToSpecificationError(
                "qualification_runner.parity_invalid", "passed must be a boolean"
            )
        surfaces = tuple(sorted(self.covered_surface_ids))
        if (
            not surfaces
            or len(surfaces) != len(set(surfaces))
            or any(not isinstance(item, str) or not item.strip() for item in surfaces)
        ):
            raise SourceToSpecificationError(
                "qualification_runner.parity_invalid",
                "parity coverage must contain unique non-empty surfaces",
            )
        object.__setattr__(self, "covered_surface_ids", surfaces)

    def to_dict(self) -> dict[str, object]:
        return canonical_value(self)


class SpecOnlyRegenerator(Protocol):
    """Generate, build, and run tests using only the declared spec inputs."""

    @property
    def provider_identity(self) -> str: ...

    def regenerate(
        self, request: RegenerationRunPlan, workspace: Path
    ) -> RegenerationOutcome: ...


class IndependentParityVerifier(Protocol):
    """Compare original behavior with a generated output after generation."""

    @property
    def provider_identity(self) -> str: ...

    def verify(
        self, request: ParityVerificationRequest, generated_workspace: Path
    ) -> ParityOutcome: ...


class QualificationAttestor(Protocol):
    """Bind measured generation and parity results to an attestation identity."""

    @property
    def provider_identity(self) -> str: ...

    def attest(self, payload: Mapping[str, object]) -> str: ...

    def verify(self, payload: Mapping[str, object], attestation_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class QualificationRunCheckpoint:
    """Signed evidence for one completed clean run, never a generated-source cache."""

    checkpoint_key: str
    attestation_payload: Mapping[str, object]
    evidence: CleanRegenerationEvidence

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "checkpoint_key", _identity(self.checkpoint_key, "checkpoint_key")
        )
        if not isinstance(self.attestation_payload, Mapping) or any(
            not isinstance(key, str) for key in self.attestation_payload
        ):
            raise SourceToSpecificationError(
                "qualification_runner.checkpoint_invalid",
                "checkpoint attestation_payload must be an object",
            )
        object.__setattr__(
            self,
            "attestation_payload",
            canonical_value(dict(self.attestation_payload)),
        )
        if not isinstance(self.evidence, CleanRegenerationEvidence):
            raise SourceToSpecificationError(
                "qualification_runner.checkpoint_invalid",
                "checkpoint evidence must be typed clean-regeneration evidence",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": QUALIFICATION_RUN_CHECKPOINT_SCHEMA,
            "checkpoint_key": self.checkpoint_key,
            "attestation_payload": dict(self.attestation_payload),
            "evidence": self.evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> QualificationRunCheckpoint:
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str) for key in value
        ):
            raise SourceToSpecificationError(
                "qualification_runner.checkpoint_invalid",
                "qualification checkpoint must be an object",
            )
        expected = {
            "schema",
            "checkpoint_key",
            "attestation_payload",
            "evidence",
        }
        if (
            set(value) != expected
            or value.get("schema") != QUALIFICATION_RUN_CHECKPOINT_SCHEMA
        ):
            raise SourceToSpecificationError(
                "qualification_runner.checkpoint_invalid",
                "qualification checkpoint has an unsupported or malformed schema",
            )
        return cls(
            checkpoint_key=value["checkpoint_key"],
            attestation_payload=value["attestation_payload"],
            evidence=CleanRegenerationEvidence.from_dict(
                value["evidence"], path="QualificationRunCheckpoint.evidence"
            ),
        )


class QualificationCheckpointStore(Protocol):
    """Persist signed per-run evidence so an interrupted qualification can resume."""

    def load(self, checkpoint_key: str) -> QualificationRunCheckpoint | None: ...

    def save(self, checkpoint: QualificationRunCheckpoint) -> None: ...


@dataclass(frozen=True, slots=True)
class OperationalQualificationResult:
    """Evidence created by execution plus the resulting authority decision."""

    evidence: tuple[CleanRegenerationEvidence, ...]
    decision: RegenerativeQualificationDecision
    resumed_run_ids: tuple[str, ...] = ()


def _checkpoint_key(
    *,
    plan: RegenerationRunPlan,
    source_snapshot_id: str,
    provider_ids: tuple[str, str, str],
    source_exclusion_identity: str,
) -> str:
    return canonical_digest(
        {
            "schema": QUALIFICATION_RUN_CHECKPOINT_KEY_SCHEMA,
            "plan": plan.to_dict(),
            "source_snapshot_id": source_snapshot_id,
            "regenerator_identity": provider_ids[0],
            "parity_verifier_identity": provider_ids[1],
            "attestor_identity": provider_ids[2],
            "source_exclusion_identity": source_exclusion_identity,
        }
    )


def _validated_checkpoint_evidence(
    checkpoint: QualificationRunCheckpoint,
    *,
    checkpoint_key: str,
    plan: RegenerationRunPlan,
    source_snapshot_id: str,
    provider_ids: tuple[str, str, str],
    source_exclusion_identity: str,
    attestor: QualificationAttestor,
) -> CleanRegenerationEvidence:
    if checkpoint.checkpoint_key != checkpoint_key:
        raise SourceToSpecificationError(
            "qualification_runner.checkpoint_key_mismatch",
            "qualification checkpoint does not match the requested run",
        )
    payload = checkpoint.attestation_payload
    required_payload_keys = {
        "schema",
        "plan",
        "source_snapshot_id",
        "source_exclusion_identity",
        "regenerator_identity",
        "regeneration",
        "parity_verifier_identity",
        "parity_request",
        "parity",
        "attestor_identity",
        "empty_workspace",
        "exact_generation_inputs",
    }
    if (
        set(payload) != required_payload_keys
        or payload.get("schema") != OPERATIONAL_QUALIFICATION_ATTESTATION_SCHEMA
    ):
        raise SourceToSpecificationError(
            "qualification_runner.checkpoint_payload_invalid",
            "qualification checkpoint contains a malformed attestation payload",
        )
    expected_static = {
        "plan": plan.to_dict(),
        "source_snapshot_id": source_snapshot_id,
        "source_exclusion_identity": source_exclusion_identity,
        "regenerator_identity": provider_ids[0],
        "parity_verifier_identity": provider_ids[1],
        "attestor_identity": provider_ids[2],
        "empty_workspace": True,
        "exact_generation_inputs": True,
    }
    if any(payload.get(key) != value for key, value in expected_static.items()):
        raise SourceToSpecificationError(
            "qualification_runner.checkpoint_binding_mismatch",
            "qualification checkpoint is not bound to the exact requested inputs",
        )
    evidence = checkpoint.evidence
    regeneration = payload.get("regeneration")
    parity_request = payload.get("parity_request")
    parity = payload.get("parity")
    if not all(
        isinstance(item, Mapping) for item in (regeneration, parity_request, parity)
    ):
        raise SourceToSpecificationError(
            "qualification_runner.checkpoint_payload_invalid",
            "qualification checkpoint outcomes must be objects",
        )
    expected_evidence = {
        "run_id": plan.run_id,
        "source_snapshot_id": source_snapshot_id,
        "specification_set_id": plan.specification_set_id,
        "target_profile_id": plan.target_profile_id,
        "flavor_lock_id": plan.flavor_lock_id,
        "generation_recipe_id": plan.generation_recipe_id,
        "generated_tree_id": regeneration.get("generated_tree_id"),
        "build_result_id": regeneration.get("build_result_id"),
        "generated_test_result_id": regeneration.get("generated_test_result_id"),
        "independent_parity_result_id": parity.get("result_id"),
        "covered_surface_ids": tuple(plan.covered_surface_ids),
        "source_excluded_from_generation": True,
        "empty_workspace": True,
        "generated_source_cache_hit": False,
        "build_passed": True,
        "generated_tests_passed": True,
        "independent_parity_passed": True,
        "generated_tests_total": regeneration.get("generated_tests_total"),
        "generated_tests_succeeded": regeneration.get("generated_tests_succeeded"),
        "generated_tests_failed": regeneration.get("generated_tests_failed"),
        "generated_tests_skipped": regeneration.get("generated_tests_skipped"),
    }
    if any(getattr(evidence, key) != value for key, value in expected_evidence.items()):
        raise SourceToSpecificationError(
            "qualification_runner.checkpoint_evidence_mismatch",
            "qualification checkpoint evidence does not match its signed payload",
        )
    if (
        parity_request.get("generated_tree_id") != evidence.generated_tree_id
        or parity_request.get("run_id") != plan.run_id
        or parity.get("passed") is not True
        or tuple(parity.get("covered_surface_ids", ())) != plan.covered_surface_ids
        or tuple(regeneration.get("generation_input_ids", ()))
        != plan.declared_generation_inputs
    ):
        raise SourceToSpecificationError(
            "qualification_runner.checkpoint_outcome_mismatch",
            "qualification checkpoint outcomes do not prove the requested clean run",
        )
    if not attestor.verify(payload, evidence.run_attestation_id):
        raise SourceToSpecificationError(
            "qualification_runner.checkpoint_signature_invalid",
            "qualification checkpoint signature could not be verified",
        )
    return evidence


def run_regenerative_qualification(
    *,
    source_snapshot_id: str,
    specification_set_id: str,
    policy: RegenerativeQualificationPolicy,
    plans: Sequence[RegenerationRunPlan],
    regenerator: SpecOnlyRegenerator,
    parity_verifier: IndependentParityVerifier,
    attestor: QualificationAttestor,
    source_exclusion: SourceExclusionAssessment | None = None,
    scratch_root: Path | None = None,
    checkpoint_store: QualificationCheckpointStore | None = None,
) -> OperationalQualificationResult:
    """Orchestrate trusted providers from clean workspaces, not free-form evidence."""

    source_snapshot_id = _identity(source_snapshot_id, "source_snapshot_id")
    specification_set_id = _identity(specification_set_id, "specification_set_id")
    if source_exclusion is not None and not isinstance(
        source_exclusion, SourceExclusionAssessment
    ):
        raise SourceToSpecificationError(
            "qualification_runner.source_exclusion_invalid",
            "source exclusion must be derived from a typed promotion closure "
            "assessment",
        )
    if (
        source_exclusion is not None
        and source_exclusion.source_inventory_identity != source_snapshot_id
    ):
        raise SourceToSpecificationError(
            "qualification_runner.source_exclusion_mismatch",
            "source exclusion assessment names another source inventory",
        )
    provider_ids = tuple(
        _identity(value, "provider_identity")
        for value in (
            regenerator.provider_identity,
            parity_verifier.provider_identity,
            attestor.provider_identity,
        )
    )
    if len(set(provider_ids)) != len(provider_ids):
        raise SourceToSpecificationError(
            "qualification_runner.provider_not_independent",
            "regenerator, parity verifier, and attestor must have distinct identities",
        )
    normalized_plans = tuple(plans)
    if not normalized_plans:
        raise SourceToSpecificationError(
            "qualification_runner.plan_empty", "at least one run plan is required"
        )
    if any(
        item.specification_set_id != specification_set_id for item in normalized_plans
    ):
        raise SourceToSpecificationError(
            "qualification_runner.specification_mismatch",
            "every run plan must bind the exact specification set",
        )
    scratch = scratch_root.resolve() if scratch_root is not None else None
    if scratch is not None and (scratch.is_symlink() or not scratch.is_dir()):
        raise SourceToSpecificationError(
            "qualification_runner.scratch_invalid",
            "scratch_root must be an existing non-symbolic-link directory",
        )
    source_exclusion_identity = canonical_digest(
        canonical_value(source_exclusion) if source_exclusion is not None else None
    )
    evidence: list[CleanRegenerationEvidence] = []
    resumed_run_ids: list[str] = []
    for plan in normalized_plans:
        checkpoint_key = _checkpoint_key(
            plan=plan,
            source_snapshot_id=source_snapshot_id,
            provider_ids=provider_ids,
            source_exclusion_identity=source_exclusion_identity,
        )
        checkpoint = checkpoint_store.load(checkpoint_key) if checkpoint_store else None
        if checkpoint is not None:
            evidence.append(
                _validated_checkpoint_evidence(
                    checkpoint,
                    checkpoint_key=checkpoint_key,
                    plan=plan,
                    source_snapshot_id=source_snapshot_id,
                    provider_ids=provider_ids,
                    source_exclusion_identity=source_exclusion_identity,
                    attestor=attestor,
                )
            )
            resumed_run_ids.append(plan.run_id)
            continue
        with tempfile.TemporaryDirectory(
            prefix="literate-ai-clean-regeneration-", dir=scratch
        ) as temporary:
            workspace = Path(temporary).resolve()
            empty_workspace = not any(workspace.iterdir())
            outcome = regenerator.regenerate(plan, workspace)
            exact_inputs = (
                outcome.generation_input_ids == plan.declared_generation_inputs
            )
            if not exact_inputs:
                raise SourceToSpecificationError(
                    "qualification_runner.generation_inputs_undeclared",
                    "regenerator used inputs outside the exact specification, "
                    "Flavor lock, and generation recipe",
                )
            parity_request = ParityVerificationRequest(
                run_id=plan.run_id,
                source_snapshot_id=source_snapshot_id,
                specification_set_id=specification_set_id,
                target_profile_id=plan.target_profile_id,
                generated_tree_id=outcome.generated_tree_id,
                covered_surface_ids=plan.covered_surface_ids,
            )
            parity = parity_verifier.verify(parity_request, workspace)
            parity_covers_plan = set(plan.covered_surface_ids).issubset(
                parity.covered_surface_ids
            )
            attestation_payload = {
                "schema": OPERATIONAL_QUALIFICATION_ATTESTATION_SCHEMA,
                "plan": plan.to_dict(),
                "source_snapshot_id": source_snapshot_id,
                "source_exclusion_identity": source_exclusion_identity,
                "regenerator_identity": provider_ids[0],
                "regeneration": outcome.to_dict(),
                "parity_verifier_identity": provider_ids[1],
                "parity_request": parity_request.to_dict(),
                "parity": parity.to_dict(),
                "attestor_identity": provider_ids[2],
                "empty_workspace": empty_workspace,
                "exact_generation_inputs": exact_inputs,
            }
            run_attestation_id = _identity(
                attestor.attest(attestation_payload), "run_attestation_id"
            )
            run_evidence = CleanRegenerationEvidence(
                run_id=plan.run_id,
                run_attestation_id=run_attestation_id,
                source_snapshot_id=source_snapshot_id,
                specification_set_id=specification_set_id,
                target_profile_id=plan.target_profile_id,
                flavor_lock_id=plan.flavor_lock_id,
                generation_recipe_id=plan.generation_recipe_id,
                generated_tree_id=outcome.generated_tree_id,
                build_result_id=outcome.build_result_id,
                generated_test_result_id=outcome.generated_test_result_id,
                independent_parity_result_id=parity.result_id,
                covered_surface_ids=(
                    plan.covered_surface_ids if parity_covers_plan else ()
                ),
                source_excluded_from_generation=(
                    source_exclusion is not None and source_exclusion.source_excluded
                ),
                empty_workspace=empty_workspace,
                generated_source_cache_hit=outcome.generated_source_cache_hit,
                build_passed=outcome.build_passed,
                generated_tests_passed=outcome.generated_tests_passed,
                independent_parity_passed=parity.passed and parity_covers_plan,
                generated_tests_total=outcome.generated_tests_total,
                generated_tests_succeeded=outcome.generated_tests_succeeded,
                generated_tests_failed=outcome.generated_tests_failed,
                generated_tests_skipped=outcome.generated_tests_skipped,
            )
            evidence.append(run_evidence)
            if checkpoint_store is not None and all(
                (
                    run_evidence.source_excluded_from_generation,
                    run_evidence.empty_workspace,
                    not run_evidence.generated_source_cache_hit,
                    run_evidence.build_passed,
                    run_evidence.generated_tests_passed,
                    run_evidence.independent_parity_passed,
                )
            ):
                checkpoint_store.save(
                    QualificationRunCheckpoint(
                        checkpoint_key=checkpoint_key,
                        attestation_payload=attestation_payload,
                        evidence=run_evidence,
                    )
                )
    decision = qualify_regenerative_specification(
        source_snapshot_id=source_snapshot_id,
        specification_set_id=specification_set_id,
        policy=policy,
        evidence=tuple(evidence),
    )
    return OperationalQualificationResult(
        tuple(evidence), decision, tuple(resumed_run_ids)
    )


__all__ = [
    "IndependentParityVerifier",
    "OperationalQualificationResult",
    "OPERATIONAL_QUALIFICATION_ATTESTATION_SCHEMA",
    "ParityOutcome",
    "ParityVerificationRequest",
    "QualificationAttestor",
    "QualificationCheckpointStore",
    "QualificationRunCheckpoint",
    "QUALIFICATION_RUN_CHECKPOINT_KEY_SCHEMA",
    "QUALIFICATION_RUN_CHECKPOINT_SCHEMA",
    "RegenerationOutcome",
    "RegenerationRunPlan",
    "SpecOnlyRegenerator",
    "run_regenerative_qualification",
]
