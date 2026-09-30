from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.coverage_gaps import (
    GATE_ADVISORY,
    GATE_FAIL_CLOSED,
    CoverageGapReport,
    find_coverage_gaps,
    is_generated_product_file,
    scan_runtime_coverage_gaps,
    unimplemented_surface_message,
)
from literate_ai.contracts import Entrypoint

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINTS = (Entrypoint("api", "portable-application", "run"),)


class CoverageGapTests(unittest.TestCase):
    def test_fully_implemented_source_has_no_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.py").write_text(
                "def handle_health(): return {'ok': True}\n"
                "ROUTES = {'/health': handle_health}\n",
                encoding="utf-8",
            )
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            self.assertEqual(report.gaps, ())
            self.assertEqual(report.entrypoint_count, 1)
            self.assertEqual(report.files_scanned, 1)

    def test_missing_source_tree_scans_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "does-not-exist"
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            self.assertEqual(report.gaps, ())
            self.assertEqual(report.files_scanned, 0)

    def test_none_bound_route_handler_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.py").write_text(
                "ROUTES = {\n"
                "    '/v1/chat/completions': None,\n"
                "    '/nodes/{id}': None,\n"
                "}\n",
                encoding="utf-8",
            )
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            self.assertEqual(len(report.gaps), 1)
            self.assertEqual(report.gaps[0].reason, "none_bound_handler")
            self.assertEqual(report.gaps[0].gate, GATE_FAIL_CLOSED)
            self.assertEqual(report.gaps[0].file_path, "server.py")
            self.assertIsNotNone(unimplemented_surface_message(report))

    def test_string_keyed_none_default_is_not_a_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.py").write_text(
                "SETTINGS = {'timeout': None, 'retries': None}\n",
                encoding="utf-8",
            )
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            self.assertEqual(report.gaps, ())

    def test_not_implemented_in_product_file_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worker.py").write_text(
                "def run():\n    raise NotImplementedError\n",
                encoding="utf-8",
            )
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            self.assertEqual(len(report.gaps), 1)
            self.assertEqual(report.gaps[0].reason, "not_implemented")
            self.assertEqual(report.gaps[0].gate, GATE_FAIL_CLOSED)

    def test_not_implemented_in_generated_tests_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests" / "test_worker.py").write_text(
                "def test_stub():\n    raise NotImplementedError\n",
                encoding="utf-8",
            )
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            self.assertEqual(len(report.gaps), 1)
            self.assertEqual(report.gaps[0].reason, "not_implemented")
            self.assertEqual(report.gaps[0].gate, GATE_ADVISORY)
            self.assertIsNone(unimplemented_surface_message(report))

    def test_todo_comment_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app" / "routes").mkdir(parents=True)
            (root / "app" / "routes" / "handlers.py").write_text(
                "# TODO: wire this up\n", encoding="utf-8"
            )
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            self.assertEqual(len(report.gaps), 1)
            self.assertEqual(report.gaps[0].reason, "todo_comment")
            self.assertEqual(report.gaps[0].gate, GATE_ADVISORY)
            self.assertEqual(report.gaps[0].file_path, "app/routes/handlers.py")
            self.assertIsNone(unimplemented_surface_message(report))

    def test_pass_bodied_abstract_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "base.py").write_text(
                "from abc import ABC, abstractmethod\n"
                "class Handler(ABC):\n"
                "    @abstractmethod\n"
                "    def run(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            self.assertEqual(len(report.gaps), 1)
            self.assertEqual(report.gaps[0].reason, "pass_bodied_abstract")
            self.assertEqual(report.gaps[0].gate, GATE_ADVISORY)
            self.assertIsNone(unimplemented_surface_message(report))

    def test_todo_does_not_hide_a_fail_closed_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.py").write_text(
                "# TODO: finish the table\nROUTES = {'/health': None}\n",
                encoding="utf-8",
            )
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            reasons = {gap.reason for gap in report.gaps}
            self.assertEqual(reasons, {"todo_comment", "none_bound_handler"})
            self.assertTrue(report.blocking_gaps)
            self.assertIsNotNone(unimplemented_surface_message(report.to_dict()))

    def test_report_round_trips_through_from_dict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.py").write_text(
                "ROUTES = {'/v1/x': None}\n", encoding="utf-8"
            )
            report = find_coverage_gaps(_ENTRYPOINTS, root)
            restored = CoverageGapReport.from_dict(report.to_dict())
            self.assertEqual(restored, report)

    def test_report_identity_is_stable_and_content_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = find_coverage_gaps(_ENTRYPOINTS, root)
            second = find_coverage_gaps(_ENTRYPOINTS, root)
            self.assertEqual(first.identity, second.identity)

    def test_scan_runtime_reads_sources_before_component_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            (runtime / "sources").mkdir(parents=True)
            (runtime / "sources" / "app.py").write_text(
                "ROUTES = {'/ready': None}\n", encoding="utf-8"
            )
            report = scan_runtime_coverage_gaps(root, "missing-component", runtime)
            self.assertIsNotNone(report)
            assert report is not None
            self.assertEqual(report.entrypoint_count, 0)
            self.assertEqual(report.gaps[0].reason, "none_bound_handler")

    def test_product_file_classifier(self) -> None:
        self.assertTrue(is_generated_product_file("app/server.py"))
        self.assertFalse(is_generated_product_file("tests/test_server.py"))
        self.assertFalse(is_generated_product_file("app/test_helpers.py"))
        self.assertFalse(is_generated_product_file("src/server_test.py"))

    def test_fixture_and_sample_trees_have_no_fail_closed_hits(self) -> None:
        """Characterize false-positive rate against checked-in source."""

        trees = (
            _REPO_ROOT / "tests" / "fixtures" / "source_to_specification",
            _REPO_ROOT / "tests" / "fixtures" / "spec_map_hello",
            _REPO_ROOT / "samples",
        )
        files_scanned = 0
        fail_closed = 0
        advisory = 0
        for tree in trees:
            if not tree.is_dir():
                continue
            report = find_coverage_gaps(_ENTRYPOINTS, tree)
            files_scanned += report.files_scanned
            fail_closed += len(report.blocking_gaps)
            advisory += len(report.gaps) - len(report.blocking_gaps)
        self.assertGreater(files_scanned, 0)
        self.assertEqual(
            fail_closed,
            0,
            "fail-closed markers fired on checked-in fixture/sample source",
        )
        self.assertIsNone(
            unimplemented_surface_message(
                find_coverage_gaps(
                    _ENTRYPOINTS,
                    _REPO_ROOT / "tests" / "fixtures" / "source_to_specification",
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
