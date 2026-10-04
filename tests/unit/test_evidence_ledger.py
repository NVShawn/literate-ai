from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.evidence_ledger import (
    explain_run,
    open_run,
    prune_runs,
    record_subprocess,
)


class EvidenceLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = patch.dict(
            os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    def test_subprocess_tees_redacts_and_records_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="subprocess")
            assert run is not None
            stdout = io.StringIO()
            argv = [
                sys.executable,
                "-c",
                "import os; print(os.environ['TEST_TOKEN']); print('done')",
            ]
            with (
                patch("sys.stdout", stdout),
                patch.dict(os.environ, {"TEST_TOKEN": "do-not-persist"}, clear=False),
            ):
                completed = record_subprocess(
                    argv,
                    cwd=project,
                    run=run,
                    parent=None,
                    path="release/gate",
                    operation="gate",
                    environment={**os.environ, "TEST_TOKEN": "do-not-persist"},
                )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("do-not-persist", stdout.getvalue())
            node = run.reduced()["nodes"][0]
            transcript = run.root / "n0001" / "stdout.log"
            self.assertNotIn("do-not-persist", transcript.read_text(encoding="utf-8"))
            pointer = next(item for item in node["outputs"] if item["role"] == "stdout")
            self.assertEqual(pointer["bytes"], transcript.stat().st_size)
            self.assertTrue(pointer["digest"].startswith("sha256:"))
            self.assertEqual(node["state"], "passed")
            self.assertEqual(
                pointer["digest"],
                "sha256:" + hashlib.sha256(transcript.read_bytes()).hexdigest(),
            )

    def test_nonzero_subprocess_is_returned_and_recorded_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="subprocess")
            assert run is not None
            result = record_subprocess(
                [sys.executable, "-c", "raise SystemExit(7)"],
                cwd=project,
                run=run,
                parent=None,
                path="release/gate",
                operation="gate",
            )
            self.assertEqual(result.returncode, 7)
            self.assertEqual(run.reduced()["nodes"][0]["state"], "failed")

    def test_explain_renders_failure_and_independent_ancestor_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="release")
            assert run is not None
            try:
                with run.node("release", operation="release") as root:
                    try:
                        with run.node(
                            "release/failing-gate",
                            operation="gate",
                            parent=root.node_id,
                        ):
                            raise RuntimeError("leaf causal error")
                    except RuntimeError as error:
                        raise ValueError("ancestor causal error") from error
            except ValueError:
                pass

            explanation = explain_run(run)
            rendered = "\n".join(explanation["render"])
            self.assertIn("- release/failing-gate [failed]", rendered)
            self.assertIn("leaf causal error", rendered)
            self.assertIn("ancestor causal error", rendered)
            self.assertEqual(
                len(
                    [
                        line
                        for line in explanation["render"]
                        if line.startswith("  error: ")
                    ]
                ),
                2,
            )

    def test_prune_keeps_failed_runs_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            first = open_run(project, operation="first")
            assert first is not None
            with first.node("first", operation="first") as node:
                node.finish(1)
            second = open_run(project, operation="second")
            assert second is not None
            with second.node("second", operation="second"):
                pass
            result = prune_runs(project, keep=1)
            self.assertNotIn(first.run_id, result["removed_run_ids"])
            self.assertTrue(first.root.exists())
            self.assertTrue(second.root.exists())


if __name__ == "__main__":
    unittest.main()
