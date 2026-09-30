"""``litai perf show``/``litai perf chart`` render recorded telemetry."""

from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from literate_ai.cache_directories import CacheDirectories
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.perf import perf_from_args
from literate_ai.perf import PerformanceRecorder


class PerfCliTests(unittest.TestCase):
    def _seed(self, project_root: Path) -> None:
        recorder = PerformanceRecorder(build_root=project_root / "_build", run_id="r1")
        with recorder.span(
            "cli.build", target_kind="command", target_id="samples/hello-component"
        ) as outcome:
            outcome["coding_cli"] = "claude"
        with recorder.span(
            "cli.test", target_kind="command", target_id="samples/hello-component"
        ):
            pass

    def _directories(self, project_root: Path) -> CacheDirectories:
        return CacheDirectories(
            project_root, project_root / "generated", project_root / "_build"
        )

    def test_show_summarizes_recorded_spans_by_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._seed(root)
            with patch(
                "literate_ai.cli.perf.resolve_cache_directories",
                return_value=self._directories(root),
            ):
                result, status = perf_from_args(
                    Namespace(
                        perf_command="show",
                        project=str(root),
                        group_by="stage",
                        stage=None,
                        run_id=None,
                    )
                )
            self.assertEqual(status, 0)
            self.assertEqual(result["span_count"], 2)
            groups = {item["group"]: item for item in result["groups"]}
            self.assertEqual(set(groups), {"cli.build", "cli.test"})
            self.assertEqual(groups["cli.build"]["count"], 1)
            self.assertIn("cli.build", result["table"])

    def test_show_filters_by_stage_and_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._seed(root)
            with patch(
                "literate_ai.cli.perf.resolve_cache_directories",
                return_value=self._directories(root),
            ):
                result, _status = perf_from_args(
                    Namespace(
                        perf_command="show",
                        project=str(root),
                        group_by="target_id",
                        stage="cli.build",
                        run_id="r1",
                    )
                )
            self.assertEqual(result["span_count"], 1)
            self.assertEqual(result["groups"][0]["group"], "samples/hello-component")

    def test_show_with_no_recorded_spans_reports_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "literate_ai.cli.perf.resolve_cache_directories",
                return_value=self._directories(root),
            ):
                result, status = perf_from_args(
                    Namespace(
                        perf_command="show",
                        project=str(root),
                        group_by="stage",
                        stage=None,
                        run_id=None,
                    )
                )
            self.assertEqual(status, 0)
            self.assertEqual(result["span_count"], 0)
            self.assertEqual(result["groups"], [])

    def test_chart_writes_a_valid_svg_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._seed(root)
            output = root / "chart.svg"
            with patch(
                "literate_ai.cli.perf.resolve_cache_directories",
                return_value=self._directories(root),
            ):
                result, status = perf_from_args(
                    Namespace(
                        perf_command="chart",
                        project=str(root),
                        group_by="stage",
                        stage=None,
                        run_id=None,
                        output=str(output),
                    )
                )
            self.assertEqual(status, 0)
            self.assertEqual(result["span_count"], 2)
            content = output.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("<svg"))
            self.assertIn("cli.build", content)
            self.assertIn("cli.test", content)

    def test_show_reads_an_archived_directory_when_dir_is_given(self) -> None:
        """An archived copy of OBJ_DIR/.litai/perf survives its build root being
        cleaned; --dir reports on it directly without any --project resolution."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._seed(root)
            archive = Path(temporary) / "archive"
            (archive).mkdir()
            for log in (root / "_build" / ".litai" / "perf").glob("*.jsonl"):
                (archive / log.name).write_bytes(log.read_bytes())
            with patch(
                "literate_ai.cli.perf.resolve_cache_directories",
                side_effect=AssertionError("must not resolve a project with --dir"),
            ):
                result, status = perf_from_args(
                    Namespace(
                        perf_command="show",
                        project=str(root),
                        dir=str(archive),
                        group_by="stage",
                        stage=None,
                        run_id=None,
                    )
                )
            self.assertEqual(status, 0)
            self.assertEqual(result["span_count"], 2)
            self.assertEqual(result["build_root"], str(archive.resolve(strict=True)))

    def test_show_rejects_a_missing_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CliFailure) as raised:
                perf_from_args(
                    Namespace(
                        perf_command="show",
                        project=temporary,
                        dir=str(Path(temporary) / "nonexistent"),
                        group_by="stage",
                        stage=None,
                        run_id=None,
                    )
                )
            self.assertEqual(raised.exception.code, "perf.directory_unavailable")

    def test_unknown_perf_subcommand_is_a_typed_failure(self) -> None:
        with self.assertRaises(CliFailure) as raised:
            perf_from_args(Namespace(perf_command="nonsense"))
        self.assertEqual(raised.exception.code, "perf.command_unknown")


if __name__ == "__main__":
    unittest.main()
