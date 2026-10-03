"""Public CLI regression coverage for durable private worker CRUD and SSH tests."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.worker_connectivity import test_worker_connection
from literate_ai.adapters.worker_registry import change_registry, read_registry
from literate_ai.cli.dispatch import main
from literate_ai.contracts import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)


class WorkerRegistryCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "workers.json"

    def cli(self, *args):
        output = io.StringIO()
        status = main(
            ["worker", *args, "--worker-config", str(self.path), "--json"],
            stdout=output,
            stderr=output,
        )
        return status, json.loads(output.getvalue())

    def add(self, name):
        return self.cli(
            "add",
            name,
            "--endpoint",
            f"user@{name}.invalid",
            "--workspace",
            "~/litai",
            "--os",
            "linux",
        )

    def test_crud_roundtrip_preserves_peers_and_supports_empty_catalog(self):
        self.assertEqual(self.cli("list")[1]["result"]["workers"], [])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.add("zulu")[0], 0)
        self.assertEqual(self.add("alpha")[0], 0)
        status, listed = self.cli("list")
        self.assertEqual(status, 0)
        self.assertEqual(
            [w["worker_id"] for w in listed["result"]["workers"]], ["alpha", "zulu"]
        )
        peer = listed["result"]["workers"][1]
        self.assertEqual(self.cli("update", "alpha", "--slots", "3")[0], 0)
        self.assertEqual(
            self.cli("show", "alpha")[1]["result"]["workers"][0]["slots"], 3
        )
        self.assertEqual(self.cli("show", "zulu")[1]["result"]["workers"][0], peer)
        self.assertEqual(self.cli("remove", "alpha")[0], 0)
        self.assertEqual(self.cli("remove", "zulu")[0], 0)
        self.assertEqual(read_registry(self.path).workers, ())
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_failed_mutations_preserve_exact_bytes_and_report_field(self):
        self.add("alpha")
        original = self.path.read_bytes()
        for argv, code in [
            (("add", "alpha"), "worker.already_registered"),
            (("update", "missing"), "worker.not_found"),
            (("remove", "missing"), "worker.not_found"),
            (
                ("update", "alpha", "--workspace", "C:/litai"),
                "worker.declaration_invalid",
            ),
            (("update", "alpha", "--slots", "0"), "worker.declaration_invalid"),
            (
                ("remove", "alpha", "--if-identity", "sha256:" + "0" * 64),
                "worker.catalog_changed",
            ),
        ]:
            with self.subTest(argv=argv):
                status, result = self.cli(*argv)
                self.assertNotEqual(status, 0)
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(self.path.read_bytes(), original)
        self.assertIn(
            "workspace",
            self.cli("update", "alpha", "--workspace", "C:/litai")[1]["error"][
                "message"
            ],
        )

    def test_compare_and_swap_and_descriptor_import(self):
        self.add("alpha")
        identity = self.cli("list")[1]["result"]["catalog_identity"]
        descriptor = self.path.parent / "one.json"
        value = self.cli("show", "alpha")[1]["result"]["workers"][0]
        value["slots"] = 7
        descriptor.write_text(json.dumps(value))
        self.assertEqual(
            self.cli(
                "update", "alpha", "--file", str(descriptor), "--if-identity", identity
            )[0],
            0,
        )
        self.assertNotEqual(
            self.cli("remove", "alpha", "--if-identity", identity)[0], 0
        )
        self.assertEqual(
            self.cli("show", "alpha")[1]["result"]["workers"][0]["slots"], 7
        )

    def test_parallel_writers_do_not_lose_registrations(self):
        def add(index):
            worker = ExecutionWorker(f"local-{index}", ExecutionWorkerKind.LOCAL)

            def change(catalog):
                return ExecutionWorkerCatalog(
                    tuple(sorted((*catalog.workers, worker), key=lambda w: w.worker_id))
                )

            change_registry(self.path, change)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(16)))
        self.assertEqual(len(read_registry(self.path).workers), 16)

    def test_atomic_publication_failure_preserves_existing_catalog(self):
        self.add("alpha")
        original = self.path.read_bytes()
        with patch(
            "literate_ai.adapters.worker_registry.os.replace", side_effect=OSError
        ):
            self.assertNotEqual(self.cli("remove", "alpha")[0], 0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".workers-*")), [])

    def test_unsafe_catalog_is_not_replaced(self):
        target = self.path.parent / "target.json"
        target.write_text('{"private":"unchanged"}')
        try:
            self.path.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable")
        self.assertNotEqual(self.add("alpha")[0], 0)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), '{"private":"unchanged"}')

    def test_connectivity_reports_all_peers_and_does_not_write_catalog(self):
        self.add("alpha")
        self.add("zulu")
        before = self.path.read_bytes()

        def run(argv, **kwargs):
            self.assertIn("BatchMode=yes", argv)
            self.assertFalse(any("StrictHostKeyChecking=no" in arg for arg in argv))
            if "user@alpha.invalid" in argv:
                return BoundedProcessResult(255, b"", b"Permission denied (publickey).")
            marker = argv[-1].split()[-1].encode()
            return BoundedProcessResult(0, marker + b"\r\n", b"")

        with patch(
            "literate_ai.adapters.worker_connectivity.run_bounded_process",
            side_effect=run,
        ):
            status, result = self.cli("test", "--all")
        self.assertEqual(status, 1)
        rows = result["result"]["workers"]
        self.assertEqual([r["worker_id"] for r in rows], ["alpha", "zulu"])
        self.assertEqual(rows[0]["cause"], "authentication")
        self.assertEqual(rows[0]["exit_status"], 255)
        self.assertEqual(rows[1]["status"], "passed")
        self.assertEqual(self.path.read_bytes(), before)

    def test_distinct_transport_causes_and_redaction(self):
        self.add("alpha")
        worker = read_registry(self.path).worker("alpha")
        for diagnostic, cause in [
            ("REMOTE HOST IDENTIFICATION HAS CHANGED!", "changed-host-key"),
            ("Host key verification failed.", "host-key-verification"),
            ("Could not resolve hostname host", "name-resolution"),
            ("Connection refused", "connection-refused"),
            ("Connection timed out", "timeout"),
        ]:
            with (
                self.subTest(cause=cause),
                patch(
                    "literate_ai.adapters.worker_connectivity.run_bounded_process",
                    return_value=BoundedProcessResult(
                        255, b"", (diagnostic + " token=private-value").encode()
                    ),
                ),
            ):
                result = test_worker_connection(worker)
                self.assertEqual(result["cause"], cause)
                self.assertNotIn("private-value", result["diagnostic"])
                self.assertTrue(result["remedy"])

    def test_real_probe_process_is_bounded_and_reports_timeout(self):
        from literate_ai.adapters.worker_capabilities import (
            WorkerCapabilityProbeError,
            _run,
        )

        with self.assertRaises(WorkerCapabilityProbeError) as overflow:
            _run(
                (
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 1048576)",
                ),
                10,
            )
        self.assertEqual(overflow.exception.code, "worker.probe_output_oversized")
        with self.assertRaises(WorkerCapabilityProbeError) as timeout:
            _run((sys.executable, "-c", "import time; time.sleep(60)"), 1)
        self.assertIn("timed out", timeout.exception.message)

    def test_interactive_rendering_preserves_each_worker_outcome(self):
        from literate_ai.cli.worker_registry import human_registry_result

        rendered = human_registry_result(
            {
                "schema": "literate-ai/worker-connectivity-result@1",
                "workers": [
                    {
                        "worker_id": "bad",
                        "status": "failed",
                        "cause": "authentication",
                        "diagnostic": "Permission denied",
                        "remedy": "Check credentials",
                    },
                    {
                        "worker_id": "good",
                        "status": "passed",
                        "cause": None,
                        "diagnostic": "",
                        "remedy": "",
                    },
                ],
            }
        )
        self.assertIn("bad: failed", rendered)
        self.assertIn("Permission denied", rendered)
        self.assertIn("good: passed", rendered)

    def test_handshake_required_and_non_ssh_does_not_launch(self):
        self.add("alpha")
        with patch(
            "literate_ai.adapters.worker_connectivity.run_bounded_process",
            return_value=BoundedProcessResult(0, b"banner only", b""),
        ) as run:
            self.assertEqual(
                test_worker_connection(read_registry(self.path).worker("alpha"))[
                    "cause"
                ],
                "handshake",
            )
            run.reset_mock()
            local = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
            self.assertEqual(test_worker_connection(local)["status"], "unsupported")
            run.assert_not_called()
