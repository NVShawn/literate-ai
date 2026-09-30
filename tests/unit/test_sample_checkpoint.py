"""Samples-gate checkpoint invalidation and resumption tests (ADR 0008)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.conformance.support import sample_runner


def _sample(directory: Path, name: str, body: str) -> Path:
    sample = directory / name
    (sample / "nested").mkdir(parents=True, exist_ok=True)
    (sample / "component.md").write_text(body, encoding="utf-8")
    (sample / "nested" / "extra.json").write_text("{}", encoding="utf-8")
    return sample


class SampleCheckpointKeyTests(unittest.TestCase):
    def _key(self, sample: Path, **overrides: object) -> str:
        arguments: dict[str, object] = {
            "flavor_selectors": (),
            "native_packages": False,
            "project_revision": "sha256:revision-a",
        }
        arguments.update(overrides)
        return sample_runner._sample_checkpoint_key(sample, **arguments)  # type: ignore[arg-type]

    def test_identical_inputs_produce_a_stable_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sample = _sample(Path(temporary), "demo", "spec")
            self.assertEqual(self._key(sample), self._key(sample))

    def test_any_authored_content_change_invalidates_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sample = _sample(Path(temporary), "demo", "spec")
            before = self._key(sample)
            (sample / "component.md").write_text("changed", encoding="utf-8")
            self.assertNotEqual(before, self._key(sample))

    def test_a_nested_file_change_invalidates_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sample = _sample(Path(temporary), "demo", "spec")
            before = self._key(sample)
            (sample / "nested" / "extra.json").write_text(
                '{"changed": true}', encoding="utf-8"
            )
            self.assertNotEqual(before, self._key(sample))

    def test_a_new_file_invalidates_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sample = _sample(Path(temporary), "demo", "spec")
            before = self._key(sample)
            (sample / "added.md").write_text("more", encoding="utf-8")
            self.assertNotEqual(before, self._key(sample))

    def test_a_framework_revision_change_invalidates_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sample = _sample(Path(temporary), "demo", "spec")
            self.assertNotEqual(
                self._key(sample, project_revision="sha256:revision-a"),
                self._key(sample, project_revision="sha256:revision-b"),
            )

    def test_flavor_selectors_and_packaging_scope_invalidate_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sample = _sample(Path(temporary), "demo", "spec")
            baseline = self._key(sample)
            self.assertNotEqual(
                baseline, self._key(sample, flavor_selectors=("+lang-rust",))
            )
            self.assertNotEqual(baseline, self._key(sample, native_packages=True))

    def test_pipeline_model_invalidates_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sample = _sample(Path(temporary), "demo", "spec")
            self.assertNotEqual(
                self._key(sample, pipeline_model="model-a"),
                self._key(sample, pipeline_model="model-b"),
            )

    def test_two_samples_with_identical_content_do_not_share_a_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = _sample(directory, "alpha", "spec")
            second = _sample(directory, "beta", "spec")
            self.assertNotEqual(self._key(first), self._key(second))


class SampleCheckpointStoreTests(unittest.TestCase):
    def test_absent_and_unreadable_state_resumes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "samples-checkpoint.json"
            self.assertEqual(sample_runner._load_sample_checkpoint(path), {})
            path.write_text("not json at all", encoding="utf-8")
            self.assertEqual(sample_runner._load_sample_checkpoint(path), {})

    def test_a_foreign_schema_resumes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "samples-checkpoint.json"
            path.write_text(
                json.dumps({"schema": "something/else@9", "entries": {"k": {}}}),
                encoding="utf-8",
            )
            self.assertEqual(sample_runner._load_sample_checkpoint(path), {})

    def test_a_recorded_case_round_trips_completely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "samples-checkpoint.json"
            case = {
                "sample_id": "demo",
                "passed": True,
                "executions": [{"nodes": [{"source_sbom_identity": "sha256:abc"}]}],
            }
            sample_runner._store_sample_checkpoint(path, {"key": case})
            self.assertEqual(sample_runner._load_sample_checkpoint(path), {"key": case})

    def test_storing_unserializable_state_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "samples-checkpoint.json"
            sample_runner._store_sample_checkpoint(path, {"key": {"bad": object()}})
            self.assertEqual(sample_runner._load_sample_checkpoint(path), {})


class DriverFailureDiagnosticTests(unittest.TestCase):
    def test_the_excerpt_keeps_the_line_that_names_the_cause(self) -> None:
        from literate_ai.adapters import project_lifecycle_driver

        diagnostic = "\n".join(
            [
                "sample: service-stack starting",
                "sample: service-stack failed: RuntimeError: generation errors="
                "{'money-calculation': 'claude exited 0 and last said: I need "
                "permission to write into `source/`'}",
                "Traceback (most recent call last):",
                *[f'  File "frame{index}.py", line {index}' for index in range(20)],
                "RuntimeError: live Standard service-stack failed",
            ]
        )
        excerpt = project_lifecycle_driver._driver_failure_excerpt(diagnostic)
        self.assertIn("I need permission to write into", excerpt)
        self.assertIn("RuntimeError: live Standard service-stack failed", excerpt)

    def test_an_empty_diagnostic_yields_no_excerpt_and_no_retained_file(self) -> None:
        from literate_ai.adapters import project_lifecycle_driver

        self.assertEqual(project_lifecycle_driver._driver_failure_excerpt("  \n "), "")
        self.assertIsNone(project_lifecycle_driver._retain_driver_diagnostic(""))

    def test_the_complete_driver_output_is_retained_for_inspection(self) -> None:
        from literate_ai.adapters import project_lifecycle_driver

        diagnostic = "\n".join(f"line {index}" for index in range(500))
        retained = project_lifecycle_driver._retain_driver_diagnostic(diagnostic)
        self.assertIsNotNone(retained)
        assert retained is not None
        try:
            self.assertEqual(Path(retained).read_text(encoding="utf-8"), diagnostic)
        finally:
            Path(retained).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
