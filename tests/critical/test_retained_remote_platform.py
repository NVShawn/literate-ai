"""Native routing and tool-provider refusal at both retained SSH boundaries."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.adapters import retained_harness_remote as remote
from literate_ai.adapters.harness_inventory import HARNESS_INVENTORY_SCHEMA
from literate_ai.adapters.harness_tree import (
    capture_retained_source_scope,
    observe_retained_tree,
)
from literate_ai.contracts import (
    ContentIdentity,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
    canonical_identity,
    canonical_json_bytes,
)


def worker(os_family="freebsd", architecture="amd64"):
    return ExecutionWorker(
        "native",
        ExecutionWorkerKind.SSH,
        endpoint="runner@worker.example",
        workspace="~/retained",
        requirements=ExecutionRequirements(
            os_family=os_family,
            cpu_architecture=architecture,
        ),
        lifecycle_executable="~/.local/bin/litai",
    )


@unittest.skipIf(os.name == "nt", "retained SSH receiver requires POSIX")
class RetainedRemotePlatformTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def stage(self, *, body="", tools=False, selected=None):
        selected = selected or worker()
        source = self.root / "source"
        source.mkdir()
        (source / "test_app.py").write_text(
            "import pathlib, unittest\n"
            "class TestBehavior(unittest.TestCase):\n"
            " def test_behavior(self):\n"
            + (body or "  self.assertEqual(6 * 7, 42)\n")
            + "unittest.main()\n"
        )
        inventory = {
            "schema": HARNESS_INVENTORY_SCHEMA,
            "commands": {},
            "findings": [],
            "source_scope": capture_retained_source_scope(source),
            "stages": [
                {
                    "id": "test",
                    "evidence": "test_app.py",
                    "command": "uv run test_app.py"
                    if tools
                    else f"{sys.executable} -B test_app.py",
                }
            ],
        }
        archive = self.root / "source.tar.gz"
        manifest, archive_id, archive_size = remote._source_archive(
            source, inventory, archive
        )
        package = self.root / "literate_ai"
        package.mkdir()
        (package / "__init__.py").write_text("")
        runtime = self.root / "runtime.zip"
        # A foreign-tool attempt is staged as a valid Linux archive deliberately;
        # the FreeBSD receiver must refuse it independently of the coordinator.
        with patch.object(
            remote, "_download_runtime_tool", return_value=b"foreign executable"
        ):
            runtime_id, runtime_size = remote._runtime_archive(
                package,
                runtime,
                inventory=inventory,
                os_family="linux",
                cpu_architecture="x86_64",
            )
        identity = canonical_identity({"fixture": "native-retained"})
        request = remote.RetainedHarnessRemoteRequest(
            "fixture",
            identity,
            selected.identity,
            identity,
            identity,
            identity,
            canonical_identity(inventory),
            ContentIdentity.parse_uri(
                observe_retained_tree(source, inventory["source_scope"])["source_tree"][
                    "identity"
                ]
            ),
            manifest,
            archive_id,
            archive_size,
            runtime_id,
            runtime_size,
            30,
        )
        for name, value in (
            ("worker", selected.to_dict()),
            ("request", request.to_dict()),
            ("inventory", inventory),
        ):
            (self.root / f"{name}.json").write_bytes(canonical_json_bytes(value))
        return request, selected

    def receive(self):
        return remote.execute_retained_harness_receiver(
            worker_file=self.root / "worker.json",
            request_file=self.root / "request.json",
            inventory_file=self.root / "inventory.json",
            archive=self.root / "source.tar.gz",
            runtime_archive=self.root / "runtime.zip",
            workspace=self.root / "workspace",
        )

    @patch.object(remote.platform, "system", new=lambda: "FreeBSD")
    @patch.object(remote.platform, "machine", new=lambda: "amd64")
    def test_source_only_receiver_runs_real_test_and_preserves_freebsd_identity(self):
        request, selected = self.stage()
        result = self.receive()
        remote.validate_retained_remote_result(result, request, selected)
        self.assertEqual(result.platform["operating_system"], "freebsd")
        self.assertEqual(result.phases[0]["exit_code"], 0)
        self.assertEqual(result.phases[0]["test_collection"]["passed"], 1)
        self.assertFalse((self.root / "workspace").exists())
        for family, architecture in (("linux", "amd64"), ("freebsd", "aarch64")):
            with self.subTest(family=family, architecture=architecture):
                altered = worker(family, architecture)
                altered_request = replace(request, worker_identity=altered.identity)
                response = replace(
                    result,
                    worker_identity=altered.identity,
                    request_identity=altered_request.identity,
                )
                with self.assertRaises(remote.RetainedHarnessRemoteError) as caught:
                    remote.validate_retained_remote_result(
                        response, altered_request, altered
                    )
                self.assertEqual(
                    caught.exception.code, "retained_receipt.remote_platform_mismatch"
                )

    @patch.object(remote.platform, "system", new=lambda: "FreeBSD")
    @patch.object(remote.platform, "machine", new=lambda: "amd64")
    def test_real_failed_test_cannot_produce_a_result_and_workspace_is_cleaned(self):
        self.stage(body="  self.fail('original behavior failed')\n")
        with self.assertRaises(remote.RetainedHarnessRemoteError):
            self.receive()
        self.assertFalse((self.root / "workspace").exists())

    @patch.object(remote.platform, "system", new=lambda: "FreeBSD")
    @patch.object(remote.platform, "machine", new=lambda: "amd64")
    def test_real_source_mutation_refuses_and_cleans_workspace(self):
        self.stage(body="  pathlib.Path('test_app.py').write_text('changed')\n")
        with self.assertRaises(remote.RetainedHarnessRemoteError) as caught:
            self.receive()
        self.assertEqual(caught.exception.code, "retained_receipt.source_mutated")
        self.assertFalse((self.root / "workspace").exists())

    @patch.object(remote.platform, "system", new=lambda: "FreeBSD")
    @patch.object(remote.platform, "machine", new=lambda: "amd64")
    def test_foreign_tool_archive_refuses_before_harness_execution(self):
        self.stage(tools=True)
        with patch.object(remote, "execute_retained_harness") as execute:
            with self.assertRaises(remote.RetainedHarnessRemoteError) as caught:
                self.receive()
            execute.assert_not_called()
        self.assertEqual(
            caught.exception.code, "retained_receipt.runtime_dependency_unsupported"
        )
        self.assertFalse((self.root / "workspace").exists())

    @patch.object(remote.platform, "system", new=lambda: "Linux")
    def test_wrong_receiver_os_refuses_before_materialization_or_execution(self):
        self.stage()
        with patch.object(remote, "execute_retained_harness") as execute:
            with self.assertRaises(remote.RetainedHarnessRemoteError) as caught:
                self.receive()
            execute.assert_not_called()
        self.assertEqual(
            caught.exception.code, "retained_receipt.remote_platform_mismatch"
        )
        self.assertFalse((self.root / "workspace").exists())

    def test_missing_tool_provider_refuses_without_downloading_foreign_binaries(self):
        with patch.object(remote, "_download_runtime_tool") as download:
            for family, architecture in (
                ("freebsd", "amd64"),
                ("macos", "x86_64"),
                ("linux", "aarch64"),
                ("linux", None),
            ):
                with self.subTest(family=family, architecture=architecture):
                    with self.assertRaises(remote.RetainedHarnessRemoteError) as caught:
                        remote._runtime_tool_files(
                            ("uv==0.12.12",),
                            os_family=family,
                            cpu_architecture=architecture,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "retained_receipt.runtime_dependency_unsupported",
                    )
                    self.assertEqual(
                        remote._runtime_tool_files(
                            (), os_family=family, cpu_architecture=architecture
                        ),
                        (),
                    )
            download.assert_not_called()

    def test_freebsd_optional_tool_request_refuses_before_any_ssh_or_scp(self):
        request, selected = self.stage(tools=True)
        inventory = json.loads((self.root / "inventory.json").read_bytes())
        runner = Mock()
        with patch.object(remote, "_download_runtime_tool") as download:
            with self.assertRaises(remote.RetainedHarnessRemoteError) as caught:
                remote.RetainedHarnessSshExecutor(runner).execute(
                    selected,
                    project_id=request.project_id,
                    project_revision_identity=request.project_revision_identity,
                    worker_catalog_identity=request.worker_catalog_identity,
                    runner_identity=request.runner_identity,
                    lifecycle_request_identity=request.lifecycle_request_identity,
                    inventory=inventory,
                    inventory_bytes=canonical_json_bytes(inventory),
                    source_root=self.root / "source",
                    source_identity=request.source_identity,
                    timeout_seconds=30,
                    cwd=self.root,
                )
            self.assertEqual(
                caught.exception.code, "retained_receipt.runtime_dependency_unsupported"
            )
            download.assert_not_called()
        runner.run.assert_not_called()
