"""Real read-only delivery without constructing retained execution authority."""

import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import ActionDispatchDeadline
from literate_ai.contracts import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.retained_package_receiver import CONFIG_SCHEMA
from literate_ai.return_cas import MAX_RETURN_BLOB_BYTES, READ_SCHEMA
from literate_ai.storage.cas import FileSystemCAS


@unittest.skipUnless(
    sys.platform.startswith("freebsd"), "native FreeBSD retained receiver entry point"
)
class RetainedReturnBlobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        project, build, objects = (
            self.root / name for name in ("project", "build", "obj")
        )
        for path in (project, build, objects):
            path.mkdir()
        self.store = FileSystemCAS(build / "retained-project-cas")
        self.content = b"historical-assertion\n" * (128 * 1024)
        self.reference = self.store.put_bytes(self.content)
        self.worker = ExecutionWorker(
            "native",
            ExecutionWorkerKind.SSH,
            endpoint="reader@example.invalid",
            workspace="/tmp/reader",
        )
        self.catalog = self.root / "workers.json"
        self.catalog.write_bytes(
            canonical_json_bytes(ExecutionWorkerCatalog((self.worker,)).to_dict())
        )
        self.configuration = self.root / "receiver.json"
        self.settings = {
            "schema": CONFIG_SCHEMA,
            "project": str(project),
            "worker_config": str(self.catalog),
            "worker": "native",
            "target": "host",
            "retained_project": "missing-manifest.json",
            "retained_project_profile": "missing-profile.json",
            "policy": "missing-policy.json",
            "tools": "missing-tools.json",
            "authorization_identity": canonical_identity(
                {"not-an-execution-grant": True}
            ).uri,
            "build_dir": str(build),
            "obj_dir": str(objects),
            "child_environment": {},
        }
        self.configuration.write_bytes(canonical_json_bytes(self.settings))

    def request(self, **changes):
        value = {
            "schema": READ_SCHEMA,
            "worker_identity": self.worker.identity.uri,
            "registration_identity": canonical_identity(self.settings).uri,
            "reference": self.reference.to_dict(),
            "deadline": ActionDispatchDeadline(
                datetime.now(UTC) + timedelta(seconds=20)
            ).to_dict(),
        }
        value.update(changes)
        return canonical_json_bytes(value)

    def run_reader(self, request=None):
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "literate_ai.retained_package_receiver",
                "--config",
                str(self.configuration),
                "--read-return-blob",
            ],
            input=self.request() if request is None else request,
            capture_output=True,
            timeout=30,
        )

    def test_real_subprocess_delivers_assertions_with_no_executable_project(self):
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.content)
        self.assertFalse((self.root / "project/missing-manifest.json").exists())

    def test_wrong_worker_and_registration_refuse_before_any_output(self):
        for field in ("worker_identity", "registration_identity"):
            with self.subTest(field=field):
                result = self.run_reader(
                    self.request(**{field: canonical_identity({"wrong": field}).uri})
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")

    def test_expired_and_oversized_requests_refuse_before_output(self):
        expired = ActionDispatchDeadline(
            datetime.now(UTC) - timedelta(seconds=1)
        ).to_dict()
        oversized = dict(self.reference.to_dict(), size=MAX_RETURN_BLOB_BYTES + 1)
        for changes in ({"deadline": expired}, {"reference": oversized}):
            with self.subTest(changes=changes):
                result = self.run_reader(self.request(**changes))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")

    def test_actual_cas_corruption_refuses_and_original_evidence_is_restored(self):
        path = self.store.path_for(self.reference)
        path.write_bytes(b"!" + self.content[1:])
        try:
            result = self.run_reader()
            self.assertNotEqual(result.returncode, 0)
        finally:
            path.write_bytes(self.content)
        self.assertEqual(self.run_reader().stdout, self.content)

    def test_mid_copy_registration_and_catalog_mutation_refuse_provisional_bytes(self):
        for path in (self.configuration, self.catalog):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                with subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "literate_ai.retained_package_receiver",
                        "--config",
                        str(self.configuration),
                        "--read-return-blob",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ) as process:
                    try:
                        process.stdin.write(self.request())
                        process.stdin.close()
                        process.stdin = None
                        first = process.stdout.read(65536)
                        self.assertTrue(first)
                        path.write_bytes(original + b" ")
                        rest, error = process.communicate(timeout=30)
                        self.assertNotEqual(process.returncode, 0, error)
                        self.assertLessEqual(len(first) + len(rest), len(self.content))
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=5)
                        path.write_bytes(original)
                restored = self.run_reader()
                self.assertEqual(restored.returncode, 0, restored.stderr)
                self.assertEqual(restored.stdout, self.content)

    def test_non_ssh_private_worker_and_oversized_catalog_refuse_before_output(self):
        original = self.catalog.read_bytes()
        local = ExecutionWorker("native", ExecutionWorkerKind.LOCAL)
        try:
            self.catalog.write_bytes(
                canonical_json_bytes(ExecutionWorkerCatalog((local,)).to_dict())
            )
            result = self.run_reader(self.request(worker_identity=local.identity.uri))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            with self.catalog.open("wb") as output:
                output.truncate(1024**2 + 1)
            result = self.run_reader()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
        finally:
            self.catalog.write_bytes(original)
        self.assertEqual(self.run_reader().returncode, 0)

    def test_mid_copy_cas_namespace_replacement_refuses_and_preserves_original(self):
        moved = self.store.root.with_name("preserved-cas")
        replaced = False
        with subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "literate_ai.retained_package_receiver",
                "--config",
                str(self.configuration),
                "--read-return-blob",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                process.stdin.write(self.request())
                process.stdin.close()
                process.stdin = None
                first = process.stdout.read(65536)
                self.assertTrue(first)
                self.store.root.rename(moved)
                self.store.root.mkdir()
                replaced = True
                _rest, error = process.communicate(timeout=30)
                self.assertNotEqual(process.returncode, 0, error)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
                if replaced:
                    self.store.root.rmdir()
                    moved.rename(self.store.root)
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.content)


if __name__ == "__main__":
    unittest.main()
