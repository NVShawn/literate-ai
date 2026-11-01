"""Versioned exhaustive coverage requires external, exact public-interface authority."""

from __future__ import annotations

import copy
import json
import unittest

from literate_ai.generated_test_coverage import (
    PUBLIC_CALLABLE_SIGNATURE_SCHEMA,
    GeneratedTestCoverageAuthority,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_V2_SCHEMA,
    GeneratedTestSuiteError,
    validate_generated_test_suite,
)
from tests.support.fixtures_test_generated_tests import RECIPE_IDENTITY, valid_suite


class ZeroArgumentGeneratedTests(unittest.TestCase):
    def setUp(self):
        self.signature = {
            "schema": PUBLIC_CALLABLE_SIGNATURE_SCHEMA,
            "arity": 0,
            "optional": False,
            "variadic": False,
        }
        self.authority = GeneratedTestCoverageAuthority.from_public_signature(
            self.signature, public_interface_identity="sha256:" + "2" * 64
        )
        self.suite = {
            "schema": GENERATED_TEST_SUITE_V2_SCHEMA,
            "recipe_identity": RECIPE_IDENTITY,
            "generation_mode": "major-rebuild",
            "coverage": self.authority.to_dict(),
            "cases": [
                {
                    "case_id": "complete-zero-argument-domain",
                    "category": "example",
                    "specification_refs": ["openspec/spec.md"],
                    "arguments": [],
                    "expected_result": 7,
                }
            ],
        }

    def validate(self, suite=None, **kwargs):
        return validate_generated_test_suite(
            json.dumps(self.suite if suite is None else suite),
            recipe_identity=RECIPE_IDENTITY,
            specification_references=("openspec/spec.md", "cpp/openspec/spec.md"),
            **kwargs,
        )

    def assert_refused(self, code, suite=None, **kwargs):
        with self.assertRaises(GeneratedTestSuiteError) as caught:
            self.validate(suite, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_exact_closed_domain_has_one_real_example_and_explicit_exhaustive_mode(
        self,
    ):
        result = self.validate(
            coverage_authority=self.authority,
            acceptance_arguments=([],),
            result_shape="integer",
        )
        self.assertEqual(result.case_ids, ("complete-zero-argument-domain",))
        self.assertEqual(result.categories, frozenset({"example"}))
        self.assertEqual(result.cases[0].arguments, ())
        self.assertEqual(result.cases[0].expected_result, 7)
        self.assertEqual(result.coverage_authority, self.authority)

    def test_generated_manifest_cannot_grant_itself_an_exemption(self):
        self.assert_refused("generated_tests.coverage_not_authorized")
        value = valid_suite()
        value["coverage"] = self.authority.to_dict()
        self.assert_refused("generated_tests.invalid_fields", value)

    def test_nonzero_optional_variadic_or_unproven_signature_cannot_grant_authority(
        self,
    ):
        signatures = [
            {},
            {**self.signature, "arity": 1},
            {**self.signature, "arity": 2},
            {**self.signature, "arity": False},
            {**self.signature, "optional": True},
            {**self.signature, "variadic": True},
            {**self.signature, "unknown": True},
        ]
        for signature in signatures:
            with self.subTest(signature=signature), self.assertRaises(ValueError):
                GeneratedTestCoverageAuthority.from_public_signature(
                    signature, public_interface_identity="sha256:" + "2" * 64
                )

    def test_stale_interface_binding_is_not_current_authority(self):
        changed = GeneratedTestCoverageAuthority.from_public_signature(
            self.signature, public_interface_identity="sha256:" + "3" * 64
        )
        self.assertNotEqual(changed.identity, self.authority.identity)
        self.assert_refused(
            "generated_tests.coverage_authority_mismatch", coverage_authority=changed
        )

    def test_missing_duplicate_and_invented_inputs_are_refused(self):
        for cases in ([], self.suite["cases"] * 2):
            with self.subTest(case_count=len(cases)):
                self.assert_refused(
                    "generated_tests.case_count",
                    {**self.suite, "cases": cases},
                    coverage_authority=self.authority,
                )
        invented = copy.deepcopy(self.suite)
        invented["cases"][0]["arguments"] = [None]
        self.assert_refused(
            "generated_tests.arguments_outside_public_domain",
            invented,
            coverage_authority=self.authority,
        )

    def test_conflicting_acceptance_signature_reports_no_private_argument_value(self):
        private_value = "verifier-only-argument-value"
        error = self.assert_refused(
            "generated_tests.coverage_interface_conflict",
            coverage_authority=self.authority,
            acceptance_arguments=([private_value],),
        )
        self.assertNotIn(private_value, error.message)
        self.assertNotIn(private_value, json.dumps(self.authority.to_dict()))

    def test_expected_result_and_non_acceptance_specification_authority_remain_required(
        self,
    ):
        invalid = copy.deepcopy(self.suite)
        invalid["cases"][0]["expected_result"] = None
        self.assert_refused(
            "generated_tests.expected_result_shape_mismatch",
            invalid,
            coverage_authority=self.authority,
            result_shape="integer",
        )
        invalid = copy.deepcopy(self.suite)
        invalid["cases"][0]["specification_refs"] = ["acceptance/private.json"]
        self.assert_refused(
            "generated_tests.invalid_specification_refs",
            invalid,
            coverage_authority=self.authority,
        )
        invalid = copy.deepcopy(self.suite)
        invalid["cases"][0]["category"] = "invariant"
        self.assert_refused(
            "generated_tests.invalid_category",
            invalid,
            coverage_authority=self.authority,
        )

    def test_frozen_v1_diversity_and_default_identity_remain_unchanged(self):
        baseline = self.validate(valid_suite())
        self.assertEqual(len(baseline.cases), 3)
        self.assertEqual(
            baseline.categories, frozenset({"example", "boundary", "invariant"})
        )
        self.assertIsNone(baseline.coverage_authority)
        single = valid_suite()
        single["cases"] = [self.suite["cases"][0]]
        self.assert_refused("generated_tests.case_count", single)
        self.assert_refused(
            "generated_tests.schema_mismatch",
            valid_suite(),
            coverage_authority=self.authority,
        )
