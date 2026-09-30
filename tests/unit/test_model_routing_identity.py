"""Regression tests for canonical model-routing identities."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.contracts import ContractValidationError, canonical_identity
from literate_ai.models import (
    DataEgress,
    Locality,
    ModelEndpoint,
    ModelGroup,
    ModelRouter,
    StageModelPolicy,
)


def endpoint(**changes: object) -> ModelEndpoint:
    values: dict[str, object] = {
        "endpoint_id": "local-generator",
        "provider": "local",
        "model": "generator",
        "base_url": "http://127.0.0.1:8000",
        "locality": Locality.LOCAL,
        "capabilities": ("structured",),
        "context_tokens": 4096,
        "input_cost_per_million": 0.125,
        "output_cost_per_million": 2.5,
    }
    values.update(changes)
    return ModelEndpoint(**values)  # type: ignore[arg-type]


def canonical_cost(value: str) -> dict[str, str]:
    return {"encoding": "decimal-v1", "value": value}


class ModelRoutingIdentityTests(unittest.TestCase):
    def test_endpoint_uses_canonical_cost_identity_but_preserves_wire_numbers(
        self,
    ) -> None:
        model = endpoint()
        wire = model.to_dict()
        self.assertEqual(wire["input_cost_per_million"], 0.125)
        self.assertEqual(wire["output_cost_per_million"], 2.5)

        identity_material = dict(wire)
        identity_material["input_cost_per_million"] = canonical_cost("0.125")
        identity_material["output_cost_per_million"] = canonical_cost("2.5")
        self.assertEqual(model.digest, canonical_identity(identity_material).uri)
        self.assertEqual(
            model.digest,
            "sha256:15640e29027dba59dc62540504983e8e369044f6f6a08aeb0efefe1ac991f053",
        )

        with self.assertRaisesRegex(ContractValidationError, "floating-point"):
            canonical_identity(wire)

    def test_equivalent_numeric_costs_have_stable_endpoint_and_policy_identities(
        self,
    ) -> None:
        integer_costs = endpoint(
            input_cost_per_million=1,
            output_cost_per_million=-0.0,
        )
        float_costs = endpoint(
            input_cost_per_million=1.0,
            output_cost_per_million=0.0,
        )
        self.assertEqual(integer_costs.digest, float_costs.digest)
        self.assertEqual(integer_costs.ref, float_costs.ref)

        integer_policy = StageModelPolicy(
            "generate",
            "generate",
            "generation",
            maximum_input_cost_per_million=1,  # type: ignore[arg-type]
        )
        float_policy = replace(integer_policy, maximum_input_cost_per_million=1.0)
        self.assertEqual(integer_policy.digest, float_policy.digest)

        material = integer_policy.to_dict()
        material["maximum_input_cost_per_million"] = canonical_cost("1")
        self.assertEqual(integer_policy.digest, canonical_identity(material).uri)
        self.assertEqual(
            integer_policy.digest,
            "sha256:883c80eb5a7342a5480f992822012e2a6d1d7d362b5be910743eeda1daf7d3ee",
        )

    def test_route_identity_is_the_canonical_identity_of_its_public_wire_shape(
        self,
    ) -> None:
        model = endpoint(input_cost_per_million=1.0)
        group = ModelGroup(
            "generation",
            "1.0.0",
            (model.endpoint_id,),
            (model.ref,),
        )
        policy = StageModelPolicy(
            "generate",
            "generate",
            group.group_id,
            maximum_input_cost_per_million=1.0,
            data_egress=DataEgress.NONE,
            group_ref=group.ref,
        )
        decision = ModelRouter(endpoints=(model,), groups=(group,)).select(policy)

        self.assertEqual(group.digest, canonical_identity(group.to_dict()).uri)
        self.assertEqual(decision.digest, canonical_identity(decision.to_dict()).uri)
        self.assertEqual(
            decision.digest,
            "sha256:177b5fa6443a855e61394ebe2382dba1aaaf5777945a4f2fa5361c660b90c281",
        )
        equivalent_policy = replace(policy, maximum_input_cost_per_million=1)
        equivalent_decision = ModelRouter(
            endpoints=(replace(model, input_cost_per_million=1),),
            groups=(group,),
        ).select(equivalent_policy)
        self.assertEqual(decision.digest, equivalent_decision.digest)

    def test_non_finite_boolean_and_negative_costs_are_rejected(self) -> None:
        invalid = (float("nan"), float("inf"), float("-inf"), True, False, -0.01)
        for value in invalid:
            with self.subTest(endpoint_input=value), self.assertRaises(ValueError):
                endpoint(input_cost_per_million=value)
            with self.subTest(endpoint_output=value), self.assertRaises(ValueError):
                endpoint(output_cost_per_million=value)
            with self.subTest(policy=value), self.assertRaises(ValueError):
                StageModelPolicy(
                    "generate",
                    "generate",
                    "generation",
                    maximum_input_cost_per_million=value,  # type: ignore[arg-type]
                )

    def test_canonical_integer_fields_reject_booleans_and_out_of_range_values(
        self,
    ) -> None:
        for value in (True, 0, 2**63):
            with (
                self.subTest(endpoint=value),
                self.assertRaisesRegex(ValueError, "signed 64-bit"),
            ):
                endpoint(context_tokens=value)
            with (
                self.subTest(policy=value),
                self.assertRaisesRegex(ValueError, "signed 64-bit"),
            ):
                StageModelPolicy(
                    "generate",
                    "generate",
                    "generation",
                    minimum_context_tokens=value,  # type: ignore[arg-type]
                )


if __name__ == "__main__":
    unittest.main()
