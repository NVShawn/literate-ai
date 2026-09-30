"""Staleness wire records must not turn absent or contradictory evidence into green."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import FrozenInstanceError, replace

from literate_ai.contracts.html_observability import HtmlStalenessReport
from literate_ai.contracts.identity import canonical_identity
from tests.unit import test_html_observability_schema as schema_tests

# Synthetic input identities qualify the record, not an installed HTML artifact.
EXPECTED = canonical_identity({"fixture": "current-render-inputs"})
OLDER = canonical_identity({"fixture": "older-render-inputs"})


def report(status: str = "current") -> HtmlStalenessReport:
    return HtmlStalenessReport(
        "observability/graph.html",
        status,
        EXPECTED,
        EXPECTED if status == "current" else OLDER if status == "stale" else None,
        ("authority-graph",) if status == "stale" else (),
    )


class HtmlStalenessContractTests(unittest.TestCase):
    def test_all_five_states_roundtrip_and_validate_against_published_schema(self):
        schemas = schema_tests.HtmlObservabilitySchemaTests()
        schemas.setUp()
        validator = schemas.validator(schema_tests.STALENESS)
        for status in ("current", "stale", "missing", "unreadable", "unpinned"):
            with self.subTest(status=status):
                record = report(status)
                wire = json.loads(json.dumps(record.to_dict()))
                validator.validate(wire)
                self.assertEqual(HtmlStalenessReport.from_dict(wire), record)
                self.assertEqual(wire["schema"], schema_tests.STALENESS)

    def test_current_requires_equal_inputs_and_no_changed_sources(self):
        for changes in (
            {"observed_render_inputs_identity": OLDER},
            {"observed_render_inputs_identity": None},
            {"stale_source_labels": ("authority-graph",)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(report(), **changes)

    def test_stale_requires_different_inputs_and_named_changed_sources(self):
        for changes in (
            {"observed_render_inputs_identity": EXPECTED},
            {"observed_render_inputs_identity": None},
            {"stale_source_labels": ()},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(report("stale"), **changes)

    def test_unavailable_states_cannot_claim_observed_inputs_or_source_changes(self):
        for status in ("missing", "unreadable", "unpinned"):
            for changes in (
                {"observed_render_inputs_identity": EXPECTED},
                {"stale_source_labels": ("authority-graph",)},
            ):
                with self.subTest(status=status, changes=changes):
                    with self.assertRaises(ValueError):
                        replace(report(status), **changes)

    def test_expected_identity_is_required_and_typed_for_every_state(self):
        for status in ("current", "stale", "missing", "unreadable", "unpinned"):
            for value in (None, EXPECTED.to_dict(), EXPECTED.uri):
                with self.subTest(status=status, value=value):
                    with self.assertRaises(ValueError):
                        replace(report(status), expected_render_inputs_identity=value)

    def test_labels_are_unique_portable_bounded_and_immutable(self):
        for labels in (
            ["authority-graph"],
            ("authority-graph", "authority-graph"),
            ("../source",),
            ("SOURCE",),
            ("",),
            ("x" * 65,),
            (1,),
            tuple(f"source-{index}" for index in range(257)),
        ):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                replace(report("stale"), stale_source_labels=labels)
        labels = tuple(f"source-{index}" for index in range(256))
        record = replace(report("stale"), stale_source_labels=labels)
        self.assertEqual(record.stale_source_labels, labels)
        self.assertEqual(HtmlStalenessReport.from_dict(record.to_dict()), record)

    def test_output_is_a_portable_html_path_and_status_is_closed(self):
        for path in ("../graph.html", "/graph.html", "graph.json", "CON.html", ""):
            with self.subTest(path=path), self.assertRaises(ValueError):
                replace(report(), artifact_path=path)
        for status in ("skip", "ok", "CURRENT", "", None):
            with self.subTest(status=status), self.assertRaises(ValueError):
                replace(report(), status=status)

    def test_wire_rejects_unknown_fields_missing_fields_and_wrong_schema(self):
        wire = report().to_dict()
        variants = [{**wire, "extra": True}, {**wire, "schema": "other"}]
        variants.extend(
            {key: value for key, value in wire.items() if key != omitted}
            for omitted in wire
        )
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                HtmlStalenessReport.from_dict(variant)

    def test_wire_rejects_malformed_nested_identities_and_labels(self):
        for field in (
            "expected_render_inputs_identity",
            "observed_render_inputs_identity",
        ):
            for value in ({}, EXPECTED.uri, {**EXPECTED.to_dict(), "digest": "bad"}):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        HtmlStalenessReport.from_dict(
                            {**report().to_dict(), field: value}
                        )
        for value in (None, "authority-graph", [None], ["a", "a"]):
            with self.subTest(labels=value), self.assertRaises(ValueError):
                HtmlStalenessReport.from_dict(
                    {**report("stale").to_dict(), "stale_source_labels": value}
                )

    def test_record_and_deserialized_labels_do_not_alias_mutable_wire(self):
        wire = report("stale").to_dict()
        original = copy.deepcopy(wire)
        record = HtmlStalenessReport.from_dict(wire)
        wire["stale_source_labels"].append("renderer")
        wire["expected_render_inputs_identity"]["digest"] = "0" * 64
        self.assertEqual(record.to_dict(), original)
        with self.assertRaises(FrozenInstanceError):
            record.status = "current"
