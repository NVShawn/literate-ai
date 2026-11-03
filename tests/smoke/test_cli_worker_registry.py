"""Public CLI regression coverage for durable private worker CRUD and SSH tests."""

from __future__ import annotations

import io
import json
import os
import platform
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from literate_ai.adapters.worker_registry import change_registry, read_registry
from literate_ai.cli.dispatch import main
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    WorkerReachability,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


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

    def test_freebsd_registration_roundtrips_without_linux_identity(self):
        status, _ = self.cli(
            "add",
            "bsd",
            "--endpoint",
            "user@bsd.invalid",
            "--workspace",
            "~/litai",
            "--os",
            "freebsd",
        )
        self.assertEqual(status, 0)
        catalog = read_registry(self.path)
        self.assertEqual(catalog.worker("bsd").requirements.os_family, "freebsd")
        status, _ = self.cli("update", "bsd", "--slots", "2")
        self.assertEqual(status, 0)
        self.assertEqual(
            read_registry(self.path).worker("bsd").requirements.os_family, "freebsd"
        )

    @unittest.skipIf(os.name == "nt", "native POSIX command route")
    def test_command_addressed_worker_registers_and_executes_the_nonce_route(self):
        root = self.path.parent
        adapter = root / "route adapter.py"
        invocations = root / "route-invocations.jsonl"
        adapter.write_text(
            "import json,pathlib,subprocess,sys\n"
            "with pathlib.Path(__file__).with_name('route-invocations.jsonl')"
            ".open('a') as out:\n"
            " out.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "assert sys.argv[1]=='execute'\n"
            "raise SystemExit(subprocess.run(['/bin/sh','-c',sys.argv[2]])"
            ".returncode)\n"
        )
        family = {"Darwin": "macos", "FreeBSD": "freebsd"}.get(
            platform.system(), "linux"
        )
        worker = ExecutionWorker(
            "routed",
            ExecutionWorkerKind.REMOTE,
            requirements=ExecutionRequirements(os_family=family),
            workspace=str(root),
            lifecycle_executable=sys.executable,
            reachability=WorkerReachability(
                (sys.executable, str(adapter), "execute", "{remote_command}"),
                (
                    sys.executable,
                    str(adapter),
                    "upload",
                    "{local_file}",
                    "{remote_file}",
                ),
                (
                    sys.executable,
                    str(adapter),
                    "download",
                    "{remote_file}",
                    "{local_file}",
                ),
            ),
        )
        descriptor = root / "remote.json"
        descriptor.write_text(json.dumps(worker.to_dict()))
        self.assertEqual(self.add("peer")[0], 0)
        peer = self.cli("show", "peer")[1]["result"]["workers"][0]
        status, response = self.cli("add", "routed", "--file", str(descriptor))
        self.assertEqual(status, 0, response)
        schemas = SchemaCatalog()
        registry = Registry().with_resources(
            (uri, Resource.from_contents(document, default_specification=DRAFT202012))
            for uri, document in schemas.resources.items()
        )
        # Validate the actual mixed catalog against the published schemas as
        # another consumer does, rather than only reading our own Python codec.
        Draft202012Validator(
            {"$ref": "urn:literate-ai:schema:v1:execution-worker-catalog"},
            registry=registry,
        ).validate(json.loads(self.path.read_text()))
        status, response = self.cli("test", "--worker-id", "routed")
        self.assertEqual(status, 0, response)
        self.assertEqual(response["result"]["workers"][0]["status"], "passed")
        route = json.loads(invocations.read_text())
        self.assertEqual(route[0], "execute")
        self.assertTrue(route[1].startswith("echo litai-ssh-"))
        self.assertEqual(self.cli("show", "peer")[1]["result"]["workers"][0], peer)
        before = self.path.read_bytes()
        invalid = worker.to_dict()
        invalid["reachability"]["execute"][-1] = "prefix-{remote_command}"
        invalids = [invalid]
        for path in ("relative", "~/safe/../other", "/tmp/unsafe\x00"):
            invalids.append({**worker.to_dict(), "workspace": path})
        invalids.append(
            {
                **worker.to_dict(),
                "action_protocol": "literate-ai/lifecycle-action-wire@1",
                "action_command": ["receiver", "{request_file}"],
            }
        )
        validator = Draft202012Validator(
            {"$ref": "urn:literate-ai:schema:v2:execution-worker"}, registry=registry
        )
        for invalid in invalids:
            with self.subTest(descriptor=invalid):
                self.assertFalse(validator.is_valid(invalid))
                descriptor.write_text(json.dumps(invalid))
                self.assertNotEqual(
                    self.cli("update", "routed", "--file", str(descriptor))[0], 0
                )
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(len(invocations.read_text().splitlines()), 1)

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
