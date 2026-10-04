"""Public CLI regression coverage for durable private worker CRUD and SSH tests."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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
