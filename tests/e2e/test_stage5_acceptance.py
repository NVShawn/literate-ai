from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.evidence_ledger import load_run, open_run
from tests.conformance.support.sample_runner import main

REPO_ROOT = Path(__file__).resolve().parents[2]


def _environment(run_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("LITAI_EVIDENCE_PARENT", None)
    environment.update(
        {
            "LITAI_EVIDENCE_RUN": str(run_root),
            "PYTHONPATH": os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT))),
        }
    )
    return environment


class Stage5AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = patch.dict(
            os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
                # Generation is mocked here; skip the live-model preflight so it
                # does not intercept the sample-acceptance paths under test.
                "LITAI_SKIP_MODEL_PREFLIGHT": "1",
                # A worker release gate marks itself remote, which demands
                # remote model credentials before the mocked generator runs.
                "LITAI_REMOTE_LIVE_GATE": "",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    def test_pre_discovery_sample_failure_retains_owned_runtime_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="sample-acceptance")
            assert run is not None
            runtime = project / "runtime"
            runtime.mkdir()
            environment = _environment(run.root)
            with (
                patch.dict(os.environ, environment, clear=False),
                patch(
                    "tests.conformance.support.sample_runner.tempfile.mkdtemp",
                    return_value=str(runtime),
                ),
                patch(
                    "tests.conformance.support.sample_runner.CodingCliSourceGenerator",
                    side_effect=RuntimeError("coding CLI unavailable"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "coding CLI unavailable"):
                    main(["--allow-host-execution"])
            report = load_run(project, run.run_id)
            assert report is not None
            node = next(
                item
                for item in report.reduced()["nodes"]
                if item["path"] == "samples/runtime"
            )
            self.assertEqual(node["state"], "failed")
            pointer = next(
                output
                for output in node["outputs"]
                if output["role"] == "sample-runtime-root"
            )
            self.assertEqual(pointer["path"], str(runtime.resolve()))
            self.assertEqual(pointer["retention"], "retained")
            self.assertTrue(runtime.is_dir())

    def test_successful_sample_run_prunes_owned_runtime_with_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="sample-acceptance")
            assert run is not None
            runtime = project / "runtime"
            runtime.mkdir()
            environment = _environment(run.root)
            with (
                patch.dict(os.environ, environment, clear=False),
                patch(
                    "tests.conformance.support.sample_runner.tempfile.mkdtemp",
                    return_value=str(runtime),
                ),
                patch(
                    "tests.conformance.support.sample_runner.run_all",
                    return_value={"passed": True},
                ),
            ):
                self.assertEqual(main(["--allow-host-execution"]), 0)
            report = load_run(project, run.run_id)
            assert report is not None
            node = next(
                item
                for item in report.reduced()["nodes"]
                if item["path"] == "samples/runtime"
            )
            pointer = next(
                output
                for output in node["outputs"]
                if output["role"] == "sample-runtime-root"
            )
            self.assertEqual(pointer["path"], str(runtime.resolve()))
            self.assertEqual(pointer["retention"], "pruned")
            self.assertFalse(runtime.exists())

    @unittest.skipUnless(os.name == "posix", "signals are POSIX-specific")
    def test_explain_after_killed_gate_keeps_partial_transcripts_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="killed-acceptance")
            assert run is not None
            gate = textwrap.dedent(
                """
                import sys
                import time
                from literate_ai.evidence_ledger import attach_run, record_subprocess

                record_subprocess(
                    [
                        sys.executable,
                        "-c",
                        "import sys, time; sys.stdout.write('partial gate output ' + "
                        "'x' * 5000); sys.stdout.flush(); "
                        "time.sleep(5)",
                    ],
                    cwd=".",
                    run=attach_run(),
                    parent=None,
                    path="release/killed-gate",
                    operation="release.gate",
                )
                """
            )
            child = subprocess.Popen(
                [sys.executable, "-c", gate],
                cwd=project,
                env=_environment(run.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            transcript = run.root / "n0001" / "stdout.log"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (
                not transcript.is_file()
                or "partial gate output" not in transcript.read_text(encoding="utf-8")
            ):
                time.sleep(0.02)
            self.assertTrue(transcript.is_file())
            self.assertIn("partial gate output", transcript.read_text(encoding="utf-8"))
            self.assertIsNone(child.poll())
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
            self.assertEqual(child.returncode, -signal.SIGKILL)

            explained = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "literate_ai.cli",
                    "release",
                    "evidence",
                    "explain",
                    "--project",
                    str(project),
                    "--run",
                    run.run_id,
                ],
                cwd=REPO_ROOT,
                env=_environment(run.root),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(explained.returncode, 0, explained.stderr)
            result = json.loads(explained.stdout)["result"]
            self.assertEqual(result["path"], [])
            self.assertIsNone(result["failure"])
            shown = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "literate_ai.cli",
                    "release",
                    "evidence",
                    "show",
                    "n0001",
                    "--project",
                    str(project),
                    "--run",
                    run.run_id,
                ],
                cwd=REPO_ROOT,
                env=_environment(run.root),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            node = json.loads(shown.stdout)["result"]
            self.assertIn(node["state"], {"running", "unavailable"})
            self.assertIn("partial gate output", transcript.read_text(encoding="utf-8"))
