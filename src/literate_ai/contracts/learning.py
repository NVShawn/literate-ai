"""Evidence-only contracts for the reviewed authority-learning boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    parse_tuple,
    string_tuple,
)
from .executable_components._common import identity, portable_name
from .identity import ContentIdentity, contract_identity

LEARNING_EVIDENCE_BINDING_SCHEMA = "urn:literate-ai:schema:v2:learning-evidence-binding"
LEARNING_SIGNAL_EVIDENCE_SCHEMA = "urn:literate-ai:schema:v2:learning-signal-evidence"
LEARNING_OBSERVATION_SCHEMA = "urn:literate-ai:schema:v2:learning-observation"
LEARNING_CLASSIFICATION_SCHEMA = "urn:literate-ai:schema:v2:learning-classification"
LEARNING_PLAN_INPUT_SCHEMA = "urn:literate-ai:schema:v2:learning-plan-input"
LEARNING_PROPOSAL_SCHEMA = "urn:literate-ai:schema:v2:learning-proposal"


class LearningEvidenceKind(StrEnum):
    DERIVATION = "derivation"
    BUILD = "build"
    TEST = "test"
    ACCEPTANCE = "acceptance"
    REJECTION = "rejection"


class LearningObservationOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class LearningSignalKind(StrEnum):
    MISSING_BEHAVIOR = "missing-behavior"
    MISSING_PUBLIC_INTERFACE = "missing-public-interface"
    TARGET_VARIANCE = "target-variance"
    REUSABLE_CONVERSION_TECHNIQUE = "reusable-conversion-technique"
    STAGE_OR_HANDOFF = "stage-or-handoff"
    ROUTING_ELIGIBILITY = "routing-eligibility"
    FRAMEWORK_DEFECT = "framework-defect"
    AUTHORITY_ALREADY_FORBIDS = "authority-already-forbids"


class LearningDisposition(StrEnum):
    COMPONENT_BEHAVIOR = "component-behavior"
    COMPONENT_INTERFACE = "component-interface"
    FLAVOR_VARIANCE = "flavor-variance"
    SKILL_TECHNIQUE = "skill-technique"
    WORKFLOW_HANDOFF = "workflow-handoff"
    ROUTING_ELIGIBILITY = "routing-eligibility"
    FRAMEWORK_DEFECT = "framework-defect"
    CANDIDATE_SPECIFIC_NO_CHANGE = "candidate-specific-no-change"


class LearningProposalScope(StrEnum):
    COMPONENT = "component"
    FLAVOR = "flavor"
    SKILL = "skill"
    WORKFLOW = "workflow"
    ROUTING_POLICY = "routing-policy"
    FRAMEWORK = "framework"
    NO_CHANGE = "no-change"


def _identity_tuple(
    values: tuple[ContentIdentity, ...], path: str, *, required: bool = False
) -> None:
    if required and not values:
        fail(path, "must not be empty")
    for index, value in enumerate(values):
        identity(value, f"{path}[{index}]")
    uris = tuple(item.uri for item in values)
    if uris != tuple(sorted(set(uris))):
        fail(path, "must be unique and canonical")


@dataclass(frozen=True, slots=True)
class LearningEvidenceBinding:
    kind: LearningEvidenceKind
    identity: ContentIdentity

    SCHEMA: ClassVar[str] = LEARNING_EVIDENCE_BINDING_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LearningEvidenceKind):
            fail("LearningEvidenceBinding.kind", "must be typed")
        identity(self.identity, "LearningEvidenceBinding.identity")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind.value,
            "identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LearningEvidenceBinding"
    ) -> LearningEvidenceBinding:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"kind", "identity"}),
        )
        return cls(
            enum_value(LearningEvidenceKind, data["kind"], f"{path}.kind"),
            ContentIdentity.from_dict(data["identity"], path=f"{path}.identity"),
        )


@dataclass(frozen=True, slots=True)
class LearningSignalEvidence:
    signal: LearningSignalKind
    evidence_identity: ContentIdentity

    SCHEMA: ClassVar[str] = LEARNING_SIGNAL_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.signal, LearningSignalKind):
            fail("LearningSignalEvidence.signal", "must be typed")
        identity(self.evidence_identity, "LearningSignalEvidence.evidence_identity")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "signal": self.signal.value,
            "evidence_identity": self.evidence_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LearningSignalEvidence"
    ) -> LearningSignalEvidence:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"signal", "evidence_identity"}),
        )
        return cls(
            enum_value(LearningSignalKind, data["signal"], f"{path}.signal"),
            ContentIdentity.from_dict(
                data["evidence_identity"], path=f"{path}.evidence_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class LearningObservation:
    """Redacted semantic facts over exact lifecycle evidence; never prompt content."""

    component_revision: ContentIdentity
    project_lifecycle_result_identity: ContentIdentity
    observer_identity: ContentIdentity
    outcome: LearningObservationOutcome
    evidence: tuple[LearningEvidenceBinding, ...]
    signal_evidence: tuple[LearningSignalEvidence, ...]
    fact_codes: tuple[str, ...]

    SCHEMA: ClassVar[str] = LEARNING_OBSERVATION_SCHEMA

    def __post_init__(self) -> None:
        identity(self.component_revision, "LearningObservation.component_revision")
        identity(
            self.project_lifecycle_result_identity,
            "LearningObservation.project_lifecycle_result_identity",
        )
        identity(self.observer_identity, "LearningObservation.observer_identity")
        if not isinstance(self.outcome, LearningObservationOutcome):
            fail("LearningObservation.outcome", "must be typed")
        if not self.evidence or any(
            not isinstance(item, LearningEvidenceBinding) for item in self.evidence
        ):
            fail("LearningObservation.evidence", "must contain typed exact evidence")
        evidence_keys = tuple(
            (item.kind.value, item.identity.uri) for item in self.evidence
        )
        if evidence_keys != tuple(sorted(set(evidence_keys))):
            fail("LearningObservation.evidence", "must be unique and canonical")
        kinds = tuple(item.kind for item in self.evidence)
        if kinds.count(LearningEvidenceKind.DERIVATION) != 1:
            fail("LearningObservation.evidence", "requires one exact derivation")
        accepted = self.outcome is LearningObservationOutcome.ACCEPTED
        required_success = {
            LearningEvidenceKind.BUILD,
            LearningEvidenceKind.TEST,
            LearningEvidenceKind.ACCEPTANCE,
        }
        if accepted and (
            not required_success <= set(kinds)
            or LearningEvidenceKind.REJECTION in kinds
        ):
            fail(
                "LearningObservation.evidence",
                "accepted observations require build, test, and acceptance "
                "without rejection",
            )
        if not accepted and (
            LearningEvidenceKind.REJECTION not in kinds
            or LearningEvidenceKind.ACCEPTANCE in kinds
        ):
            fail(
                "LearningObservation.evidence",
                "rejected observations require rejection and cannot claim acceptance",
            )
        if any(
            not isinstance(item, LearningSignalEvidence)
            for item in self.signal_evidence
        ):
            fail("LearningObservation.signal_evidence", "must contain typed signals")
        signal_keys = tuple(
            (item.signal.value, item.evidence_identity.uri)
            for item in self.signal_evidence
        )
        if signal_keys != tuple(sorted(set(signal_keys))):
            fail("LearningObservation.signal_evidence", "must be unique and canonical")
        evidence_identities = {item.identity for item in self.evidence}
        if any(
            item.evidence_identity not in evidence_identities
            for item in self.signal_evidence
        ):
            fail(
                "LearningObservation.signal_evidence",
                "every signal must bind retained exact evidence",
            )
        for index, code in enumerate(self.fact_codes):
            portable_name(code, f"LearningObservation.fact_codes[{index}]")
        if self.fact_codes != tuple(sorted(set(self.fact_codes))):
            fail("LearningObservation.fact_codes", "must be unique and canonical")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "project_lifecycle_result_identity": (
                self.project_lifecycle_result_identity.to_dict()
            ),
            "observer_identity": self.observer_identity.to_dict(),
            "outcome": self.outcome.value,
            "evidence": [item.to_dict() for item in self.evidence],
            "signal_evidence": [item.to_dict() for item in self.signal_evidence],
            "fact_codes": list(self.fact_codes),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LearningObservation"
    ) -> LearningObservation:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "project_lifecycle_result_identity",
                    "observer_identity",
                    "outcome",
                    "evidence",
                    "signal_evidence",
                    "fact_codes",
                }
            ),
        )
        return cls(
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            ContentIdentity.from_dict(
                data["project_lifecycle_result_identity"],
                path=f"{path}.project_lifecycle_result_identity",
            ),
            ContentIdentity.from_dict(
                data["observer_identity"], path=f"{path}.observer_identity"
            ),
            enum_value(
                LearningObservationOutcome,
                data["outcome"],
                f"{path}.outcome",
            ),
            parse_tuple(
                data["evidence"],
                f"{path}.evidence",
                LearningEvidenceBinding.from_dict,
            ),
            parse_tuple(
                data["signal_evidence"],
                f"{path}.signal_evidence",
                LearningSignalEvidence.from_dict,
            ),
            string_tuple(data["fact_codes"], f"{path}.fact_codes"),
        )


@dataclass(frozen=True, slots=True)
class LearningClassification:
    observation_identity: ContentIdentity
    classifier_identity: ContentIdentity
    disposition: LearningDisposition
    matched_signals: tuple[LearningSignalKind, ...]

    SCHEMA: ClassVar[str] = LEARNING_CLASSIFICATION_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.observation_identity, "LearningClassification.observation_identity"
        )
        identity(self.classifier_identity, "LearningClassification.classifier_identity")
        if not isinstance(self.disposition, LearningDisposition):
            fail("LearningClassification.disposition", "must be typed")
        if any(
            not isinstance(item, LearningSignalKind) for item in self.matched_signals
        ):
            fail("LearningClassification.matched_signals", "must be typed")
        values = tuple(item.value for item in self.matched_signals)
        if values != tuple(sorted(set(values))):
            fail(
                "LearningClassification.matched_signals", "must be unique and canonical"
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "observation_identity": self.observation_identity.to_dict(),
            "classifier_identity": self.classifier_identity.to_dict(),
            "disposition": self.disposition.value,
            "matched_signals": [item.value for item in self.matched_signals],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LearningClassification"
    ) -> LearningClassification:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "observation_identity",
                    "classifier_identity",
                    "disposition",
                    "matched_signals",
                }
            ),
        )
        raw_signals = data["matched_signals"]
        if not isinstance(raw_signals, list):
            fail(f"{path}.matched_signals", "must be an array")
        return cls(
            ContentIdentity.from_dict(
                data["observation_identity"], path=f"{path}.observation_identity"
            ),
            ContentIdentity.from_dict(
                data["classifier_identity"], path=f"{path}.classifier_identity"
            ),
            enum_value(
                LearningDisposition,
                data["disposition"],
                f"{path}.disposition",
            ),
            tuple(
                enum_value(
                    LearningSignalKind,
                    item,
                    f"{path}.matched_signals[{index}]",
                )
                for index, item in enumerate(raw_signals)
            ),
        )


@dataclass(frozen=True, slots=True)
class LearningPlanInput:
    observation: LearningObservation
    component_authority_identities: tuple[ContentIdentity, ...]
    flavor_authority_identities: tuple[ContentIdentity, ...]
    skill_authority_identities: tuple[ContentIdentity, ...]
    workflow_authority_identities: tuple[ContentIdentity, ...]
    routing_authority_identities: tuple[ContentIdentity, ...]
    framework_authority_identities: tuple[ContentIdentity, ...]
    test_suite_identities: tuple[ContentIdentity, ...]
    rebuild_component_identities: tuple[ContentIdentity, ...]

    SCHEMA: ClassVar[str] = LEARNING_PLAN_INPUT_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.observation, LearningObservation):
            fail("LearningPlanInput.observation", "must be typed")
        for name in (
            "component_authority_identities",
            "flavor_authority_identities",
            "skill_authority_identities",
            "workflow_authority_identities",
            "routing_authority_identities",
            "framework_authority_identities",
            "test_suite_identities",
            "rebuild_component_identities",
        ):
            _identity_tuple(getattr(self, name), f"LearningPlanInput.{name}")
        if self.observation.component_revision not in self.rebuild_component_identities:
            fail(
                "LearningPlanInput.rebuild_component_identities",
                "must include the observed Component",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "observation": self.observation.to_dict(),
            **{
                name: [item.to_dict() for item in getattr(self, name)]
                for name in (
                    "component_authority_identities",
                    "flavor_authority_identities",
                    "skill_authority_identities",
                    "workflow_authority_identities",
                    "routing_authority_identities",
                    "framework_authority_identities",
                    "test_suite_identities",
                    "rebuild_component_identities",
                )
            },
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LearningPlanInput"
    ) -> LearningPlanInput:
        names = frozenset(
            {
                "observation",
                "component_authority_identities",
                "flavor_authority_identities",
                "skill_authority_identities",
                "workflow_authority_identities",
                "routing_authority_identities",
                "framework_authority_identities",
                "test_suite_identities",
                "rebuild_component_identities",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            observation=LearningObservation.from_dict(
                data["observation"], path=f"{path}.observation"
            ),
            **{
                name: parse_tuple(
                    data[name], f"{path}.{name}", ContentIdentity.from_dict
                )
                for name in names - {"observation"}
            },
        )


@dataclass(frozen=True, slots=True)
class LearningProposal:
    plan_input_identity: ContentIdentity
    observation_identity: ContentIdentity
    classification: LearningClassification
    scope: LearningProposalScope
    authority_identities: tuple[ContentIdentity, ...]
    affected_test_suite_identities: tuple[ContentIdentity, ...]
    rebuild_component_identities: tuple[ContentIdentity, ...]
    semantic_delta_codes: tuple[str, ...]

    SCHEMA: ClassVar[str] = LEARNING_PROPOSAL_SCHEMA

    def __post_init__(self) -> None:
        identity(self.plan_input_identity, "LearningProposal.plan_input_identity")
        identity(self.observation_identity, "LearningProposal.observation_identity")
        if (
            not isinstance(self.classification, LearningClassification)
            or self.classification.observation_identity != self.observation_identity
        ):
            fail(
                "LearningProposal.classification",
                "must bind the exact observation",
            )
        if not isinstance(self.scope, LearningProposalScope):
            fail("LearningProposal.scope", "must be typed")
        for name in (
            "authority_identities",
            "affected_test_suite_identities",
            "rebuild_component_identities",
        ):
            _identity_tuple(getattr(self, name), f"LearningProposal.{name}")
        no_change = self.scope is LearningProposalScope.NO_CHANGE
        if no_change != (not self.authority_identities):
            fail(
                "LearningProposal.authority_identities",
                "must be empty exactly for a no-change proposal",
            )
        if not no_change and len(self.authority_identities) != 1:
            fail(
                "LearningProposal.authority_identities",
                "a narrow proposal must name exactly one authority owner",
            )
        if not self.rebuild_component_identities:
            fail("LearningProposal.rebuild_component_identities", "must not be empty")
        for index, code in enumerate(self.semantic_delta_codes):
            portable_name(code, f"LearningProposal.semantic_delta_codes[{index}]")
        if self.semantic_delta_codes != tuple(sorted(set(self.semantic_delta_codes))):
            fail(
                "LearningProposal.semantic_delta_codes", "must be unique and canonical"
            )
        if no_change and self.semantic_delta_codes:
            fail(
                "LearningProposal.semantic_delta_codes",
                "no-change proposal cannot suggest an authority delta",
            )
        if not no_change and not self.semantic_delta_codes:
            fail(
                "LearningProposal.semantic_delta_codes",
                "authority-change proposal requires a minimal semantic delta",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "plan_input_identity": self.plan_input_identity.to_dict(),
            "observation_identity": self.observation_identity.to_dict(),
            "classification": self.classification.to_dict(),
            "scope": self.scope.value,
            "authority_identities": [
                item.to_dict() for item in self.authority_identities
            ],
            "affected_test_suite_identities": [
                item.to_dict() for item in self.affected_test_suite_identities
            ],
            "rebuild_component_identities": [
                item.to_dict() for item in self.rebuild_component_identities
            ],
            "semantic_delta_codes": list(self.semantic_delta_codes),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LearningProposal"
    ) -> LearningProposal:
        names = frozenset(
            {
                "plan_input_identity",
                "observation_identity",
                "classification",
                "scope",
                "authority_identities",
                "affected_test_suite_identities",
                "rebuild_component_identities",
                "semantic_delta_codes",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            ContentIdentity.from_dict(
                data["plan_input_identity"], path=f"{path}.plan_input_identity"
            ),
            ContentIdentity.from_dict(
                data["observation_identity"], path=f"{path}.observation_identity"
            ),
            LearningClassification.from_dict(
                data["classification"], path=f"{path}.classification"
            ),
            enum_value(LearningProposalScope, data["scope"], f"{path}.scope"),
            parse_tuple(
                data["authority_identities"],
                f"{path}.authority_identities",
                ContentIdentity.from_dict,
            ),
            parse_tuple(
                data["affected_test_suite_identities"],
                f"{path}.affected_test_suite_identities",
                ContentIdentity.from_dict,
            ),
            parse_tuple(
                data["rebuild_component_identities"],
                f"{path}.rebuild_component_identities",
                ContentIdentity.from_dict,
            ),
            string_tuple(data["semantic_delta_codes"], f"{path}.semantic_delta_codes"),
        )


__all__ = [
    "LearningClassification",
    "LearningDisposition",
    "LearningEvidenceBinding",
    "LearningEvidenceKind",
    "LearningObservation",
    "LearningObservationOutcome",
    "LearningPlanInput",
    "LearningProposal",
    "LearningProposalScope",
    "LearningSignalEvidence",
    "LearningSignalKind",
]
