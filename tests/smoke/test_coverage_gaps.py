from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.coverage_gaps import (
    GATE_FAIL_CLOSED,
    find_coverage_gaps,
    unimplemented_surface_message,
)
from literate_ai.contracts import Entrypoint

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINTS = (Entrypoint("api", "portable-application", "run"),)


class CoverageGapTests(unittest.TestCase):
    def test_not_implemented_in_product_file_is_fail_closed(self) -> None:
        cases = (
            (
                "worker.py",
                "def run():\n    raise NotImplementedError\n",
                "not_implemented",
            ),
            (
                "server.py",
                "ROUTES = {\n"
                "    '/v1/chat/completions': None,\n"
                "    '/nodes/{id}': None,\n"
                "}\n",
                "none_bound_handler",
            ),
        )
        for name, source, reason in cases:
            with (
                self.subTest(reason=reason),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / name).write_text(source, encoding="utf-8")
                report = find_coverage_gaps(_ENTRYPOINTS, root)
                self.assertEqual(len(report.gaps), 1)
                self.assertEqual(report.gaps[0].reason, reason)
                self.assertEqual(report.gaps[0].gate, GATE_FAIL_CLOSED)
                self.assertEqual(report.gaps[0].file_path, name)
                self.assertIsNotNone(unimplemented_surface_message(report))

    def test_fixture_and_sample_trees_have_no_fail_closed_hits(self) -> None:
        """Characterize false-positive rate against checked-in source."""

        trees = (
            _REPO_ROOT / "tests" / "fixtures" / "source_to_specification",
            _REPO_ROOT / "tests" / "fixtures" / "spec_map_hello",
            _REPO_ROOT / "samples",
        )
        files_scanned = 0
        fail_closed = 0
        for tree in trees:
            if not tree.is_dir():
                continue
            report = find_coverage_gaps(_ENTRYPOINTS, tree)
            files_scanned += report.files_scanned
            fail_closed += len(report.blocking_gaps)
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
