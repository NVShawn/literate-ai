"""Cancellation stops owned gate descendants and closes their evidence."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_CHILD = """import json, os, subprocess, sys, time
from pathlib import Path
from literate_ai.evidence_ledger import attach_run

root = Path(sys.argv[1])
run = attach_run()
assert run is not None
with run.node("release/gate/completed", operation="completed"):
    pass
with run.node("release/gate/retry", operation="retry"):
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
    (root / "ready.tmp").write_text(
        json.dumps({"gate": os.getpid(), "grandchild": child.pid})
    )
    (root / "ready.tmp").replace(root / "ready.json")
    print("gate ready", flush=True)
    time.sleep(120)
"""
_PARENT = """import subprocess, sys
from pathlib import Path
from literate_ai.evidence_ledger import open_run, record_subprocess

root = Path(sys.argv[1])
run = open_run(root, operation="release.check")
assert run is not None
(root / "run.txt").write_text(str(run.root))
try:
    record_subprocess(
        [sys.executable, str(root / "child.py"), str(root)],
        cwd=root,
        run=run,
        parent=None,
        path="release/gate",
        operation="release.gate",
        tee=sys.argv[2] == "true",
        timeout=1.0 if sys.argv[3] == "timeout" else None,
    )
except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
    run.close("failed")
    raise SystemExit(130 if isinstance(exc, KeyboardInterrupt) else 124)
"""


@unittest.skipUnless(os.name == "posix", "requires targeted POSIX SIGINT")
class EvidenceCancellationTests(unittest.TestCase):
    def test_interruption_stops_gate_tree_and_finalizes_nested_evidence(self):
        for tee, reason in ((True, "signal"), (True, "timeout")):
            with (
                self.subTest(tee=tee, reason=reason),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                (root / "child.py").write_text(_CHILD)
                (root / "parent.py").write_text(_PARENT)
                pids = {}
                with (
                    (root / "stdout").open("wb") as out,
                    (root / "stderr").open("wb") as err,
                ):
                    owner = subprocess.Popen(
                        [
                            sys.executable,
                            str(root / "parent.py"),
                            str(root),
                            str(tee).lower(),
                            reason,
                        ],
                        stdout=out,
                        stderr=err,
                        start_new_session=True,
                    )
                    try:
                        deadline = time.monotonic() + 15
                        while not (root / "ready.json").exists():
                            self.assertIsNone(
                                owner.poll(), (root / "stderr").read_text()
                            )
                            self.assertLess(
                                time.monotonic(), deadline, "gate did not become ready"
                            )
                            time.sleep(0.02)
                        pids = json.loads((root / "ready.json").read_text())
                        if reason == "signal":
                            os.kill(owner.pid, signal.SIGINT)
                        self.assertEqual(
                            owner.wait(timeout=10), 130 if reason == "signal" else 124
                        )
                        for name, pid in pids.items():
                            observed = subprocess.run(
                                ["ps", "-o", "stat=", "-p", str(pid)],
                                capture_output=True,
                                text=True,
                                check=False,
                            ).stdout.strip()
                            self.assertTrue(
                                not observed or observed.startswith("Z"),
                                (name, observed),
                            )
                        ledger_root = Path((root / "run.txt").read_text())
                        ledger = json.loads((ledger_root / "index.json").read_text())
                        self.assertEqual(ledger["state"], "failed")
                        nodes = {node["path"]: node for node in ledger["nodes"]}
                        completed = nodes.pop("release/gate/completed")
                        self.assertEqual(completed["state"], "passed")
                        self.assertIsNone(completed["error"])
                        self.assertEqual(
                            set(nodes), {"release/gate", "release/gate/retry"}
                        )
                        for node in nodes.values():
                            self.assertEqual(node["state"], "failed")
                            self.assertEqual(
                                node["error"]["code"],
                                "exception.keyboardinterrupt"
                                if reason == "signal"
                                else "exception.timeoutexpired",
                            )
                            self.assertTrue(node["ended_at"])
                    finally:
                        if pids:
                            try:
                                os.killpg(pids["gate"], signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        if owner.poll() is None:
                            os.killpg(owner.pid, signal.SIGKILL)
                            owner.wait(timeout=5)
