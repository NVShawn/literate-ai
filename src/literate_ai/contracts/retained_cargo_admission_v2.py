"""Versioned custody and honest target coverage in retained Cargo receipts.

These are serialized claims, not admission capabilities. Receivers must reopen
current authority and verified command records; parsing cannot restore them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, list_value
from .identity import ContentIdentity, canonical_identity
from .retained_cargo_admission import RetainedCargoAdmissionReceipt
from .retained_cargo_binding_v2 import RetainedCargoBindingV2
from .retained_cargo_policy import RetainedLibraryGatePolicyV2
from .retained_cargo_test_evidence import (
    RetainedCargoTargetEvidenceV2,
    require_retained_cargo_coverage,
)
from .retained_cargo_tests_v2 import RetainedCargoTestInventoryV2
from .retained_cargo_workspace_v2 import RetainedCargoWorkspacePlanV2


@dataclass(frozen=True, slots=True)
class RetainedCargoAdmissionReceiptV2:
    common: RetainedCargoAdmissionReceipt
    input_custody_policy_identity: ContentIdentity
    target_outcomes: tuple[RetainedCargoTargetEvidenceV2, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-admission-receipt@2"
    COMMON_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "binding_identity",
            "workspace_plan_identity",
            "gate_policy_identity",
            "test_inventory_identity",
            "source_retirement_identity",
            "consumer_inputs_identity",
            "qualification_identity",
            "command_observation_identities",
        }
    )

    def __post_init__(self):
        if type(self.common) is not RetainedCargoAdmissionReceipt or not isinstance(
            self.input_custody_policy_identity, ContentIdentity
        ):
            fail(
                self.__class__.__name__,
                "requires exact common receipt and custody declarations",
            )
        if (
            not isinstance(self.target_outcomes, tuple)
            or not 1 <= len(self.target_outcomes) <= 4096
            or any(
                type(x) is not RetainedCargoTargetEvidenceV2
                for x in self.target_outcomes
            )
        ):
            fail(self.__class__.__name__, "requires bounded typed target outcomes")
        keys = tuple(x.key for x in self.target_outcomes)
        if keys != tuple(sorted(set(keys))):
            fail(self.__class__.__name__, "requires unique targets in canonical order")
        count = sum(
            len(x.passed_cases) + len(x.ignored_cases) for x in self.target_outcomes
        )
        if count > 100000:
            fail(self.__class__.__name__, "aggregate case allowance exceeded")
        if not any(
            x.passed_cases or x.kind == "custom-harness" for x in self.target_outcomes
        ):
            fail(self.__class__.__name__, "requires positive execution coverage")
        phases = tuple(
            identity
            for x in self.target_outcomes
            for identity in x.command_observation_identities
        )
        if len(set(phases)) != len(phases) or not set(phases) <= set(
            self.common.command_observation_identities
        ):
            fail(
                self.__class__.__name__,
                "requires distinct bound target phase observations",
            )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @property
    def coverage_counts(self):
        return {
            "passed_cases": sum(len(x.passed_cases) for x in self.target_outcomes),
            "reviewed_ignored_cases": sum(
                len(x.ignored_cases) for x in self.target_outcomes
            ),
            "custom_commands": sum(
                x.kind == "custom-harness" for x in self.target_outcomes
            ),
            "excluded_targets": sum(x.kind == "excluded" for x in self.target_outcomes),
        }

    def require_declarations(
        self,
        binding: RetainedCargoBindingV2,
        plan: RetainedCargoWorkspacePlanV2,
        policy: RetainedLibraryGatePolicyV2,
        inventory: RetainedCargoTestInventoryV2,
    ):
        if type(binding) is not RetainedCargoBindingV2:
            fail(self.__class__.__name__, "requires the matching v2 binding")
        binding.require_declarations(plan, policy, inventory)
        if (
            self.common.binding_identity != binding.identity
            or self.common.workspace_plan_identity != plan.identity
            or self.common.gate_policy_identity != policy.identity
            or self.common.test_inventory_identity != inventory.identity
            or self.input_custody_policy_identity != policy.input_custody.identity
            or self.common.qualification_identity
            != binding.common.qualification_identity
        ):
            fail(self.__class__.__name__, "receipt declaration identities differ")
        require_retained_cargo_coverage(self.target_outcomes, inventory)

    def to_dict(self):
        result = self.common.to_dict()
        result.update(
            schema=self.SCHEMA,
            input_custody_policy_identity=self.input_custody_policy_identity.to_dict(),
            target_outcomes=[x.to_dict() for x in self.target_outcomes],
            coverage_counts=self.coverage_counts,
        )
        return result

    @classmethod
    def from_dict(cls, value: Any):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=cls.COMMON_FIELDS
            | {"input_custody_policy_identity", "target_outcomes", "coverage_counts"},
        )
        outcomes = list_value(data["target_outcomes"], cls.__name__)
        observations = list_value(data["command_observation_identities"], cls.__name__)
        if not 1 <= len(outcomes) <= 4096 or not 1 <= len(observations) <= 100000:
            fail(cls.__name__, "receipt record allowance exceeded")
        count = 0
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                fail(cls.__name__, "requires target outcome records")
            count += len(list_value(outcome.get("passed_cases"), cls.__name__))
            count += len(list_value(outcome.get("ignored_cases"), cls.__name__))
            if count > 100000:
                fail(cls.__name__, "aggregate case allowance exceeded")
        common = {key: data[key] for key in cls.COMMON_FIELDS}
        common["schema"] = RetainedCargoAdmissionReceipt.SCHEMA
        result = cls(
            RetainedCargoAdmissionReceipt.from_dict(common),
            ContentIdentity.from_dict(data["input_custody_policy_identity"]),
            tuple(RetainedCargoTargetEvidenceV2.from_dict(x) for x in outcomes),
        )
        counts = data["coverage_counts"]
        if (
            not isinstance(counts, dict)
            or counts.keys() != result.coverage_counts.keys()
            or any(type(x) is not int for x in counts.values())
            or counts != result.coverage_counts
        ):
            fail(cls.__name__, "coverage counts differ from explicit target outcomes")
        return result
