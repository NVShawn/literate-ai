"""Versioned contract and package-resource tests for Standard lifecycle policy."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import FrozenInstanceError
from importlib.resources import files

from literate_ai.contracts import (
    CURRENT_STANDARD_LIFECYCLE_POLICY_RESOURCE,
    MINIMUM_PROJECT_REBUILD_PHASES,
    PROJECT_LIFECYCLE_EXTENSION_PHASES,
    STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
    StandardLifecyclePolicy,
    load_current_standard_lifecycle_policy,
)
from tests.unit.test_schema_catalog import SchemaCatalog


class StandardLifecyclePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_current_standard_lifecycle_policy()
        self.wire = self.policy.to_dict()

    def test_packaged_policy_is_exact_versioned_immutable_authority(self) -> None:
        resource = files("literate_ai.standard_policies").joinpath(
            CURRENT_STANDARD_LIFECYCLE_POLICY_RESOURCE
        )
        self.assertTrue(resource.is_file())
        self.assertEqual(
            json.loads(resource.read_text(encoding="utf-8")),
            self.wire,
        )
        self.assertEqual(self.policy.policy_id, "literate-ai-standard")
        self.assertEqual(self.policy.policy_version, "1.0.0")
        self.assertEqual(self.policy.phases, MINIMUM_PROJECT_REBUILD_PHASES)
        self.assertEqual(
            self.policy.required_evidence_kinds,
            STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
        )
        self.assertEqual(
            tuple(item.phase for item in self.policy.extension_evidence),
            PROJECT_LIFECYCLE_EXTENSION_PHASES,
        )
        self.assertEqual(
            self.policy.identity.uri,
            "sha256:a5ca0d6d13daff0199f218c5e4af723a2cec87b927e75dd3a731c511b5d85ac7",
        )
        with self.assertRaises(FrozenInstanceError):
            self.policy.policy_version = "2.0.0"  # type: ignore[misc]

    def test_contract_round_trips_and_validates_against_public_schema(self) -> None:
        SchemaCatalog().validate(self.policy.SCHEMA, self.wire)
        decoded = StandardLifecyclePolicy.from_dict(self.wire)
        self.assertEqual(decoded, self.policy)
        self.assertEqual(decoded.identity, self.policy.identity)

    def test_unknown_fields_and_schema_versions_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            StandardLifecyclePolicy.from_dict({**self.wire, "ambient": True})
        with self.assertRaisesRegex(ValueError, "schema"):
            StandardLifecyclePolicy.from_dict(
                {**self.wire, "schema": "literate-ai/standard-policy@99"}
            )

    def test_policy_rejects_incomplete_or_reordered_phase_authority(self) -> None:
        missing = copy.deepcopy(self.wire)
        missing["phases"].pop(4)
        with self.assertRaisesRegex(ValueError, "complete Standard lifecycle"):
            StandardLifecyclePolicy.from_dict(missing)

        reordered = copy.deepcopy(self.wire)
        reordered["phases"][-1:-1] = ["deploy", "package-artifacts"]
        with self.assertRaisesRegex(ValueError, "canonical phase order"):
            StandardLifecyclePolicy.from_dict(reordered)

        extended = copy.deepcopy(self.wire)
        extended["phases"][-1:-1] = ["package-artifacts", "publish-artifacts"]
        parsed = StandardLifecyclePolicy.from_dict(extended)
        self.assertEqual(parsed.phases, tuple(extended["phases"]))
        SchemaCatalog().validate(parsed.SCHEMA, extended)

        after_receipt = copy.deepcopy(self.wire)
        after_receipt["phases"].append("package-artifacts")
        with self.assertRaisesRegex(ValueError, "complete Standard lifecycle"):
            StandardLifecyclePolicy.from_dict(after_receipt)

    def test_policy_rejects_weak_or_noncanonical_receipt_evidence(self) -> None:
        for label, kinds, message in (
            (
                "missing-runner",
                tuple(
                    item
                    for item in STANDARD_FULL_REBUILD_EVIDENCE_KINDS
                    if item != "test-runner"
                ),
                "must include 'test-runner'",
            ),
            (
                "unknown",
                tuple(sorted((*STANDARD_FULL_REBUILD_EVIDENCE_KINDS, "asserted"))),
                "unsupported evidence kinds",
            ),
            (
                "unordered",
                tuple(reversed(STANDARD_FULL_REBUILD_EVIDENCE_KINDS)),
                "canonical evidence-kind order",
            ),
        ):
            with self.subTest(label=label):
                wire = copy.deepcopy(self.wire)
                wire["required_evidence_kinds"] = list(kinds)
                with self.assertRaisesRegex(ValueError, message):
                    StandardLifecyclePolicy.from_dict(wire)

    def test_policy_rejects_invalid_extension_bindings(self) -> None:
        for label, mutate, message in (
            (
                "missing",
                lambda values: values.pop(),
                "cover every supported extension",
            ),
            (
                "reordered",
                lambda values: values.reverse(),
                "canonical phase order",
            ),
            (
                "duplicate-evidence",
                lambda values: values[1].update(
                    evidence_kind=values[0]["evidence_kind"]
                ),
                "extension evidence kinds must be unique",
            ),
        ):
            with self.subTest(label=label):
                wire = copy.deepcopy(self.wire)
                mutate(wire["extension_evidence"])
                with self.assertRaisesRegex(ValueError, message):
                    StandardLifecyclePolicy.from_dict(wire)

    def test_extension_lookup_is_total_only_for_supported_extensions(self) -> None:
        self.assertEqual(
            self.policy.evidence_kind_for_extension("package-artifacts"),
            "package-result",
        )
        with self.assertRaisesRegex(ValueError, "has no evidence binding"):
            self.policy.evidence_kind_for_extension("unknown")


if __name__ == "__main__":
    unittest.main()
