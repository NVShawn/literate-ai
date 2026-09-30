"""Phase-1 compatibility coverage for current OVA lifecycle artifacts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.compatibility.ova import (
    DiagnosticSeverity,
    LifecycleState,
    OvaCompatibilityReader,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ova"


class OvaCompatibilityReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = OvaCompatibilityReader()

    def test_current_component_and_settings_shapes_are_read_immutably(self) -> None:
        component = self.reader.read_component(FIXTURES / "valid" / "ova.yaml")
        models = self.reader.read_settings(FIXTURES / "valid" / "models.yaml")
        runtime = self.reader.read_settings(
            FIXTURES / "valid" / "runtime-settings.json"
        )

        self.assertEqual(
            component.state,
            LifecycleState.VALID,
            tuple((item.code, item.value_path) for item in component.diagnostics),
        )
        self.assertEqual(component.kind, "component")
        self.assertEqual(component.data["component"]["id"], "sample.compatibility")
        self.assertEqual(
            component.data["future_component_metadata"]["labels"],
            ("alpha", "beta"),
        )
        self.assertEqual(models.kind, "settings:model-portfolio")
        self.assertEqual(models.data["future_settings"]["selection_trace"], "enabled")
        self.assertEqual(runtime.kind, "settings:runtime")
        self.assertNotIn("nvidia_api_key", runtime.data)
        with self.assertRaises(TypeError):
            component.data["component"]["id"] = "mutated"

    def test_versioned_ova_v3_component_is_ready_without_drift(self) -> None:
        document = (
            """\
schema_version: 3
component:
  id: sample.versioned
  name: Versioned Sample
  version: 2.1.0
openspec:
  root: openspec
  capabilities: [sample-versioned]
spec_files: [openspec/specs/sample-versioned/spec.md]
skills:
  - skill_id: authoring
    version: 1.0.0
    source: skills/authoring.md
    content_digest: sha256:"""
            + ("a" * 64)
            + """
model_selector:
  version: 1.0.0
  stages:
    - {stage: api_contracts}
    - {stage: file_plan}
    - {stage: generate}
    - {stage: repair}
requirements:
  - {id: stage, capability: scene.runtime, purpose: Run the scene.}
flavor_slots: {}
"""
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ova.yaml"
            path.write_text(document, encoding="utf-8")

            component = self.reader.read_component(path)

        self.assertEqual(
            component.state,
            LifecycleState.VALID,
            tuple((item.code, item.value_path) for item in component.diagnostics),
        )
        self.assertTrue(component.ready)
        self.assertEqual(component.data["schema_version"], 3)
        self.assertEqual(component.data["component"]["version"], "2.1.0")
        self.assertNotIn(
            "ova.compatibility.unknown_field",
            {item.code for item in component.diagnostics},
        )

    def test_cache_and_standalone_package_shapes_preserve_unknown_fields(self) -> None:
        valid = FIXTURES / "valid"
        cache = self.reader.read_cache(valid / "cache.json")
        source = self.reader.read_source_package(valid / "source-package.json")
        object_package = self.reader.read_object_package(valid / "object-package.json")

        for result in (cache, source, object_package):
            self.assertEqual(result.state, LifecycleState.VALID)
            self.assertTrue(result.ready)
            unknown = [
                item
                for item in result.diagnostics
                if item.code == "ova.compatibility.unknown_field"
            ]
            self.assertEqual(len(unknown), 1)
            self.assertEqual(unknown[0].severity, DiagnosticSeverity.INFO)
            self.assertEqual(unknown[0].source_path, str(Path(result.source_path)))

        self.assertTrue(cache.data["future_cache_field"]["retained"])
        self.assertTrue(source.data["future_source_package_field"]["retained"])
        self.assertEqual(
            object_package.data["future_object_package_field"], ("retained",)
        )

    def test_json_records_round_trip_without_losing_future_fields(self) -> None:
        valid = FIXTURES / "valid"
        cases = (
            (self.reader.read_cache, "cache.json"),
            (self.reader.read_source_package, "source-package.json"),
            (self.reader.read_object_package, "object-package.json"),
            (self.reader.read_provenance, "generation.json"),
            (self.reader.read_publication_settings, "publication-settings.json"),
            (self.reader.read_publications, "publication-records.json"),
        )
        for reader, filename in cases:
            with self.subTest(filename=filename):
                path = valid / filename
                original = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(reader(path).mutable_value(), original)

    def test_provenance_and_publication_shapes_are_source_diagnosed(self) -> None:
        valid = FIXTURES / "valid"
        provenance = self.reader.read_provenance(valid / "generation.json")
        publications = self.reader.read_publications(valid / "publication-records.json")
        settings = self.reader.read_publication_settings(
            valid / "publication-settings.json"
        )

        self.assertEqual(provenance.data["transaction_id"], "gen_demo")
        self.assertEqual(
            provenance.data["spec_artifacts"][0]["path"],
            ("openspec/specs/sample-compatibility/spec.md"),
        )
        self.assertEqual(publications.records[0]["state"], "published")
        self.assertEqual(
            settings.data["targets"]["team-cache"]["transport"], "filesystem"
        )
        self.assertTrue(
            all(
                diagnostic.source_path == result.source_path
                for result in (provenance, publications, settings)
                for diagnostic in result.diagnostics
            )
        )

    def test_invalid_component_retains_precise_source_and_value_paths(self) -> None:
        result = self.reader.read_component(FIXTURES / "invalid" / "ova.yaml")
        self.assertEqual(result.state, LifecycleState.INVALID)
        self.assertFalse(result.structurally_valid)
        self.assertEqual(
            Path(result.source_path),
            (FIXTURES / "invalid" / "ova.yaml").resolve(),
        )
        self.assertIn("$.component", {item.value_path for item in result.diagnostics})
        self.assertIn(
            "$.spec_files[0]", {item.value_path for item in result.diagnostics}
        )

    def test_transient_and_truncated_records_are_interrupted_not_drifted(self) -> None:
        transient = self.reader.read_publications(
            FIXTURES / "interrupted" / "publication-records.json"
        )
        truncated = self.reader.read_cache(
            FIXTURES / "interrupted" / "truncated-cache.json"
        )

        self.assertEqual(transient.state, LifecycleState.INTERRUPTED)
        self.assertEqual(truncated.state, LifecycleState.INTERRUPTED)
        self.assertEqual(truncated.root_shape, "unreadable")
        self.assertEqual(truncated.diagnostics[0].code, "ova.json.interrupted")
        self.assertIsNotNone(truncated.diagnostics[0].line)

    def test_cross_reference_drift_is_distinct_from_schema_invalidity(self) -> None:
        result = self.reader.read_cache(FIXTURES / "drifted" / "cache.json")
        self.assertEqual(result.state, LifecycleState.DRIFTED)
        self.assertTrue(result.structurally_valid)
        self.assertFalse(result.ready)
        self.assertIn("latest source package is absent", result.diagnostics[0].message)


if __name__ == "__main__":
    unittest.main()
