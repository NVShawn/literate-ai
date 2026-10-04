from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.evidence_ledger import latest_run, open_run

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "scripts" / "litai_step.py"


class StepHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = patch.dict(
            os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
                # A command-line OBJ_DIR reaches nested Make through these even
                # after the direct environment entry above is cleared.
                "MAKEFLAGS": "",
                "MAKEOVERRIDES": "",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    def test_standalone_step_opens_owned_run_and_preserves_io_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            environment = dict(os.environ)
            environment.pop("LITAI_EVIDENCE_RUN", None)
            environment.pop("LITAI_EVIDENCE_PARENT", None)
            result = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "--name",
                    "plain",
                    "--",
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'out\\x00'); "
                    "sys.stderr.buffer.write(b'err\\xff'); raise SystemExit(23)",
                ],
                cwd=project,
                env=environment,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 23)
            self.assertEqual(result.stdout, b"out\x00")
            self.assertEqual(result.stderr, "err\ufffd".encode("utf-8"))
            run = latest_run(project)
            self.assertIsNotNone(run)
            assert run is not None
            self.assertEqual(run.reduced()["state"], "failed")
            self.assertEqual(run.reduced()["nodes"][0]["state"], "failed")

    def test_attached_run_records_successful_step_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="test")
            assert run is not None
            environment = dict(os.environ)
            environment["LITAI_EVIDENCE_RUN"] = str(run.root)
            environment.pop("LITAI_EVIDENCE_PARENT", None)
            environment["PYTHONPATH"] = str(REPO_ROOT / "src")
            result = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "--name",
                    "attached",
                    "--",
                    sys.executable,
                    "-c",
                    "print('out'); import sys; print('err', file=sys.stderr)",
                ],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            node = run.reduced()["nodes"][0]
            self.assertEqual(node["state"], "passed")
            self.assertEqual(
                {output["role"] for output in node["outputs"]},
                {"stdout", "stderr"},
            )
