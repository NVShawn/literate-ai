"""Structured performance telemetry: recorder and reader."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.perf import (
    PERFORMANCE_SPAN_SCHEMA,
    PerformanceRecorder,
    PerformanceSpan,
    perf_log_directory,
    read_performance_spans,
    read_performance_spans_from_directory,
)


class PerformanceRecorderTests(unittest.TestCase):
    def test_span_records_success_with_duration_and_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build_root = Path(temporary)
            recorder = PerformanceRecorder(build_root=build_root, run_id="run-a")
            with recorder.span(
                "cli.build", target_kind="command", target_id="samples/hello-component"
            ):
                pass
            spans = read_performance_spans(build_root)
            self.assertEqual(len(spans), 1)
            span = spans[0]
            self.assertEqual(span.schema, PERFORMANCE_SPAN_SCHEMA)
            self.assertEqual(span.run_id, "run-a")
            self.assertEqual(span.stage, "cli.build")
            self.assertEqual(span.target_kind, "command")
            self.assertEqual(span.target_id, "samples/hello-component")
            self.assertTrue(span.ok)
            self.assertIsNone(span.error_code)
            self.assertGreaterEqual(span.duration_ms, 0)
            self.assertIsNotNone(span.started_at)
            self.assertIsNotNone(span.ended_at)

    def test_span_records_failure_and_reraises(self) -> None:
        class _TypedError(RuntimeError):
            def __init__(self) -> None:
                self.code = "widget.exploded"
                super().__init__("boom")

        with tempfile.TemporaryDirectory() as temporary:
            build_root = Path(temporary)
            recorder = PerformanceRecorder(build_root=build_root, run_id="run-b")
            with self.assertRaises(_TypedError):
                with recorder.span(
                    "release.check.worker", target_kind="worker", target_id="ubuntu-a"
                ):
                    raise _TypedError()
            spans = read_performance_spans(build_root)
            self.assertEqual(len(spans), 1)
            self.assertFalse(spans[0].ok)
            self.assertEqual(spans[0].error_code, "widget.exploded")

    def test_span_lets_the_caller_record_coding_cli_and_model_after_the_fact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build_root = Path(temporary)
            recorder = PerformanceRecorder(build_root=build_root, run_id="run-c")
            with recorder.span(
                "cli.build", target_kind="command", target_id="samples/hello-component"
            ) as outcome:
                outcome["coding_cli"] = "claude"
                outcome["model"] = "pipeline-default-model"
            span = read_performance_spans(build_root)[0]
            self.assertEqual(span.coding_cli, "claude")
            self.assertEqual(span.model, "pipeline-default-model")

    def test_multiple_spans_from_one_recorder_append_to_the_same_run_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build_root = Path(temporary)
            recorder = PerformanceRecorder(build_root=build_root, run_id="run-d")
            with recorder.span("cli.build", target_kind="command", target_id="a"):
                pass
            with recorder.span("cli.test", target_kind="command", target_id="a"):
                pass
            log_lines = recorder.log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(log_lines), 2)
            stages = {json.loads(line)["stage"] for line in log_lines}
            self.assertEqual(stages, {"cli.build", "cli.test"})

    def test_a_missing_build_root_never_fails_the_observed_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build_root = Path(temporary) / "does" / "not" / "exist" / "yet"
            recorder = PerformanceRecorder(build_root=build_root, run_id="run-e")
            with recorder.span("cli.build", target_kind="command", target_id="a"):
                pass
            self.assertTrue(recorder.log_path.is_file())

    def test_read_performance_spans_returns_empty_for_an_absent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(read_performance_spans(Path(temporary)), [])

    def test_read_performance_spans_skips_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build_root = Path(temporary)
            directory = perf_log_directory(build_root)
            directory.mkdir(parents=True)
            (directory / "run-f.jsonl").write_text(
                "not json\n"
                + json.dumps(
                    PerformanceSpan(
                        run_id="run-f",
                        stage="cli.build",
                        target_kind="command",
                        target_id="a",
                        coding_cli=None,
                        model=None,
                        started_at="2026-08-17T00:00:00+00:00",
                        ended_at="2026-08-17T00:00:01+00:00",
                        duration_ms=1000,
                        ok=True,
                        error_code=None,
                        extra={},
                    ).to_dict()
                )
                + "\n"
                + json.dumps({"schema": PERFORMANCE_SPAN_SCHEMA})
                + "\n",
                encoding="utf-8",
            )
            spans = read_performance_spans(build_root)
            self.assertEqual(len(spans), 1)
            self.assertEqual(spans[0].stage, "cli.build")

    def test_reads_spans_from_multiple_run_logs_in_one_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build_root = Path(temporary)
            for run_id in ("run-g", "run-h"):
                recorder = PerformanceRecorder(build_root=build_root, run_id=run_id)
                with recorder.span("cli.build", target_kind="command", target_id="a"):
                    pass
            spans = read_performance_spans(build_root)
            self.assertEqual({span.run_id for span in spans}, {"run-g", "run-h"})

    def test_reads_spans_from_an_archived_directory_without_litai_perf_nesting(
        self,
    ) -> None:
        """An archived copy of OBJ_DIR/.litai/perf, moved anywhere, is still
        readable: the archive directory is the log directory itself, with no
        .litai/perf nesting required (unlike read_performance_spans)."""

        with tempfile.TemporaryDirectory() as temporary:
            build_root = Path(temporary) / "build"
            recorder = PerformanceRecorder(build_root=build_root, run_id="run-i")
            with recorder.span("cli.build", target_kind="command", target_id="a"):
                pass
            archive = Path(temporary) / "archive"
            archive.mkdir()
            for log in perf_log_directory(build_root).glob("*.jsonl"):
                (archive / log.name).write_bytes(log.read_bytes())
            spans = read_performance_spans_from_directory(archive)
            self.assertEqual(len(spans), 1)
            self.assertEqual(spans[0].run_id, "run-i")

    def test_reads_spans_from_directory_returns_empty_for_an_absent_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                read_performance_spans_from_directory(Path(temporary) / "gone"), []
            )


if __name__ == "__main__":
    unittest.main()
