from __future__ import annotations

import unittest

from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    ContributionKind,
    ContributionReference,
    MergeOperator,
    ToolchainConstraint,
    merge_toolchain_constraints,
)


class ToolchainConstraintContractTests(unittest.TestCase):
    def test_roundtrip_preserves_an_argument_vector_and_version_prefixes(self) -> None:
        constraint = ToolchainConstraint.from_dict(
            {
                "schema": "urn:literate-ai:schema:v1:toolchain-constraint",
                "toolchain": "python",
                "command": ["python3.12", "-I"],
                "minimum_version": [3, 11],
                "required_version": [3, 12],
                "remediation": "Install the selected Python toolchain.",
                "remediation_uri": "https://example.test/python",
            }
        )

        self.assertEqual(
            ToolchainConstraint.from_dict(constraint.to_dict()), constraint
        )
        self.assertEqual(constraint.command, ("python3.12", "-I"))
        self.assertEqual(constraint.remediation_uri, "https://example.test/python")
        self.assertTrue(constraint.identity.uri.startswith("sha256:"))

    def test_merge_intersects_pins_instead_of_using_precedence(self) -> None:
        merged = merge_toolchain_constraints(
            (
                ToolchainConstraint("python", minimum_version=(3, 11)),
                ToolchainConstraint(
                    "python",
                    command=("python3.12",),
                    minimum_version=(3, 12),
                    required_version=(3,),
                ),
                ToolchainConstraint("python", required_version=(3, 12)),
            )
        )

        self.assertEqual(merged.command, ("python3.12",))
        self.assertEqual(merged.minimum_version, (3, 12))
        self.assertEqual(merged.required_version, (3, 12))

    def test_merge_retains_the_tightest_exclusive_upper_bound(self) -> None:
        merged = merge_toolchain_constraints(
            (
                ToolchainConstraint(
                    "npm",
                    minimum_version=(9,),
                    maximum_exclusive_version=(13,),
                ),
                ToolchainConstraint(
                    "npm",
                    minimum_version=(9,),
                    maximum_exclusive_version=(12,),
                ),
            )
        )

        self.assertEqual(merged.minimum_version, (9,))
        self.assertEqual(merged.maximum_exclusive_version, (12,))
        self.assertEqual(merged.version_range(), ">=9,<12")

    def test_empty_exclusive_range_fails_closed(self) -> None:
        with self.assertRaises(ContractValidationError):
            ToolchainConstraint(
                "npm",
                minimum_version=(9,),
                maximum_exclusive_version=(9,),
            )

    def test_incompatible_command_and_version_pins_fail_closed(self) -> None:
        cases = (
            (
                ToolchainConstraint("node", command=("node",)),
                ToolchainConstraint("node", command=("nodejs",)),
            ),
            (
                ToolchainConstraint("node", required_version=(20,)),
                ToolchainConstraint("node", required_version=(22,)),
            ),
            (
                ToolchainConstraint("python", minimum_version=(3, 11)),
                ToolchainConstraint("python", required_version=(3, 10)),
            ),
        )
        for constraints in cases:
            with self.subTest(constraints=constraints):
                with self.assertRaises(ContractValidationError):
                    merge_toolchain_constraints(constraints)

    def test_toolchain_contribution_requires_typed_exact_singleton_content(
        self,
    ) -> None:
        identity = ContentIdentity.parse_uri("sha256:" + "a" * 64)
        with self.assertRaises(ContractValidationError):
            ContributionReference(
                "node",
                ContributionKind.TOOLCHAIN,
                MergeOperator.ADDITIVE_SET,
                "node",
                ContentReference("toolchain-constraint", "node.json", identity),
            )
        with self.assertRaises(ContractValidationError):
            ContributionReference(
                "node",
                ContributionKind.TOOLCHAIN,
                MergeOperator.EXACT_SINGLETON,
                "node",
                ContentReference("specification", "node.json", identity),
            )

    def test_unknown_fields_and_impossible_version_ranges_are_rejected(self) -> None:
        with self.assertRaises(ContractValidationError):
            ToolchainConstraint.from_dict(
                {
                    "schema": "urn:literate-ai:schema:v1:toolchain-constraint",
                    "toolchain": "python",
                    "minimum_version": [3, 11],
                    "required_version": [3, 10],
                }
            )
        with self.assertRaises(ContractValidationError):
            ToolchainConstraint.from_dict(
                {
                    "schema": "urn:literate-ai:schema:v1:toolchain-constraint",
                    "toolchain": "python",
                    "minimum_version": [3, 11],
                    "prose": "please find a recent Python",
                }
            )


if __name__ == "__main__":
    unittest.main()
