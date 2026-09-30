from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.harness_inventory import HARNESS_INVENTORY_SCHEMA
from literate_ai.adapters.harness_tree import (
    capture_retained_source_scope,
    observe_retained_tree,
)
from literate_ai.adapters.retained_harness_remote import (
    RetainedHarnessRemoteError,
    RetainedHarnessRemoteRequest,
    RetainedHarnessRemoteResult,
    RetainedHarnessSshExecutor,
    _retained_receiver_failure,
    _runtime_archive,
    _source_archive,
    execute_retained_harness_receiver,
    retained_harness_runtime_requirements,
)
from literate_ai.adapters.ssh_transport import SshProcessResult
from literate_ai.cli import main
from literate_ai.cli.dispatch import _parser
from literate_ai.contracts import (
    ContentIdentity,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.remote_source_guard import SourceGuardError


def _worker() -> ExecutionWorker:
    return ExecutionWorker(
        "linux",
        ExecutionWorkerKind.SSH,
        endpoint="runner@linux.example",
        workspace="~/literate-ai",
        requirements=ExecutionRequirements(os_family="linux"),
        lifecycle_executable="~/.local/bin/litai",
    )


def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def _phase() -> dict[str, object]:
    return {
        "phase": "test",
        "command": "make test",
        "exit_code": 0,
        "timed_out": False,
        "test_collection": {"state": "nonempty", "total": 1},
    }


class RecordingRetainedRunner:
    def __init__(self, *, tamper_worker: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[int] = []
        self.request: RetainedHarnessRemoteRequest | None = None
        self.tamper_worker = tamper_worker

    def run(self, argv, *, cwd, timeout_seconds):
        del cwd
        call = tuple(argv)
        self.calls.append(call)
        self.timeouts.append(timeout_seconds)
        if call[0] == "scp":
            source = Path(call[-2])
            if source.name == "request.json":
                self.request = RetainedHarnessRemoteRequest.from_dict(
                    json.loads(source.read_text(encoding="utf-8"))
                )
            return SshProcessResult(0, b"", b"")
        command = call[-1]
        if "worker execute-retained" in command:
            assert self.request is not None
            request = self.request
            result = RetainedHarnessRemoteResult(
                request.identity,
                request.lifecycle_request_identity,
                _identity("tampered-worker")
                if self.tamper_worker
                else request.worker_identity,
                request.inventory_identity,
                request.source_identity,
                {
                    "operating_system": "linux",
                    "operating_system_release": "6.8.0",
                    "machine": "x86_64",
                    "python_implementation": "cpython",
                    "python_version": "3.13.7",
                },
                (_phase(),),
            )
            envelope = {
                "schema": "literate-ai/cli-result@1",
                "ok": True,
                "command": "worker.execute-retained",
                "result": result.to_dict(),
            }
            return SshProcessResult(0, canonical_json_bytes(envelope) + b"\n", b"")
        return SshProcessResult(0, b"", b"")


class RetainedHarnessRemoteTests(unittest.TestCase):
    def test_source_archive_contains_initialized_gitlink_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            child_source = Path(directory) / "child-source"
            root.mkdir()
            child_source.mkdir()
            for repository in (root, child_source):
                subprocess.run(("git", "init", "-q", str(repository)), check=True)
                subprocess.run(
                    ("git", "-C", str(repository), "config", "user.name", "Fixture"),
                    check=True,
                )
                subprocess.run(
                    (
                        "git",
                        "-C",
                        str(repository),
                        "config",
                        "user.email",
                        "fixture@example.test",
                    ),
                    check=True,
                )
            (child_source / "nested.cpp").write_text("int nested = 1;\n")
            subprocess.run(("git", "-C", str(child_source), "add", "."), check=True)
            subprocess.run(
                ("git", "-C", str(child_source), "commit", "-qm", "child"),
                check=True,
            )
            (root / "Makefile").write_text("test:\n\t@true\n")
            subprocess.run(("git", "-C", str(root), "add", "."), check=True)
            subprocess.run(
                ("git", "-C", str(root), "commit", "-qm", "root"), check=True
            )
            subprocess.run(
                (
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "-C",
                    str(root),
                    "submodule",
                    "add",
                    "-q",
                    str(child_source),
                    "extensions/nested",
                ),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(root), "commit", "-qam", "gitlink"), check=True
            )
            scope = capture_retained_source_scope(root)
            archive = Path(directory) / "source.tar.gz"

            _source_archive(root, {"source_scope": scope}, archive)

            with tarfile.open(archive, "r:gz") as captured:
                names = set(captured.getnames())
            self.assertIn("literate-ai/extensions/nested/nested.cpp", names)
            self.assertFalse(any(".git" in name.split("/") for name in names))

    def test_canonical_receiver_failure_preserves_sanitized_code_and_message(
        self,
    ) -> None:
        envelope = {
            "schema": "literate-ai/cli-error@1",
            "ok": False,
            "command": "worker.execute-retained",
            "error": {
                "code": "retained_receipt.remote_runtime_invalid",
                "message": "runtime archive differs from the request",
            },
        }

        failure = _retained_receiver_failure(
            SshProcessResult(
                2,
                b"",
                b"bash: no job control in this shell\n"
                + canonical_json_bytes(envelope)
                + b"\n",
            )
        )

        self.assertEqual(
            failure.code,
            "retained_receipt.remote_runtime_invalid",
        )
        self.assertEqual(
            failure.message,
            "runtime archive differs from the request",
        )

    def test_canonical_receiver_failure_preserves_bounded_diagnostic_tail(
        self,
    ) -> None:
        message = "setup\n" + ("x" * 8192) + "\nretained-tail"
        envelope = {
            "schema": "literate-ai/cli-error@1",
            "ok": False,
            "command": "worker.execute-retained",
            "error": {
                "code": "project.convert_legacy_gate_failed",
                "message": message,
            },
        }

        failure = _retained_receiver_failure(
            SshProcessResult(
                2,
                b"",
                canonical_json_bytes(envelope) + b"\n",
            )
        )

        self.assertEqual(failure.code, "project.convert_legacy_gate_failed")
        self.assertIn("retained-tail", failure.message)

    def test_noncanonical_receiver_failure_is_bounded_and_redacted(self) -> None:
        with patch.dict(
            os.environ,
            {"PRIVATE_WORKER_TOKEN": "s3cr3t"},
            clear=False,
        ):
            failure = _retained_receiver_failure(
                SshProcessResult(
                    2,
                    b"",
                    (
                        b"Traceback under /home/private/work "
                        b"token=s3cr3t password=also-secret\n"
                    ),
                )
            )

        self.assertEqual(failure.code, "execution.ssh_receiver_failed")
        self.assertIn("SSH lifecycle receiver exited with status 2", failure.message)
        self.assertIn("<private-path>", failure.message)
        self.assertIn("<redacted>", failure.message)
        self.assertNotIn("s3cr3t", failure.message)
        self.assertNotIn("also-secret", failure.message)

    def test_internal_receiver_command_has_only_staged_attempt_inputs(self) -> None:
        parsed = _parser().parse_args(
            [
                "worker",
                "execute-retained",
                "--worker-file",
                "worker.json",
                "--request",
                "request.json",
                "--inventory",
                "inventory.json",
                "--archive",
                "source.tar.gz",
                "--runtime-archive",
                "literate-ai-runtime.zip",
                "--workspace",
                "workspace",
            ]
        )

        self.assertEqual(parsed.worker_command, "execute-retained")
        self.assertEqual(parsed.inventory, "inventory.json")
        self.assertEqual(parsed.runtime_archive, "literate-ai-runtime.zip")

    def test_receiver_cli_redacts_private_paths_and_secrets_but_keeps_code(
        self,
    ) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        message = (
            "token=s3cr3t password=also-secret failed under "
            r"/home/private/work/project and C:\Users\worker\private"
        )
        with (
            patch.dict(
                os.environ,
                {
                    "PRIVATE_WORKER_TOKEN": "s3cr3t",
                    "PRIVATE_WORKER_PASSWORD": "also-secret",
                },
                clear=False,
            ),
            patch(
                "literate_ai.adapters.retained_harness_remote."
                "execute_retained_harness_receiver",
                side_effect=RetainedHarnessRemoteError(
                    "retained_receipt.remote_execution_failed",
                    message,
                ),
            ),
        ):
            status = main(
                (
                    "worker",
                    "execute-retained",
                    "--worker-file",
                    "worker.json",
                    "--request",
                    "request.json",
                    "--inventory",
                    "inventory.json",
                    "--archive",
                    "source.tar.gz",
                    "--runtime-archive",
                    "literate-ai-runtime.zip",
                    "--workspace",
                    "workspace",
                ),
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(status, 2)
        envelope = json.loads(stderr.getvalue())
        self.assertEqual(
            envelope["error"]["code"],
            "retained_receipt.remote_execution_failed",
        )
        public = envelope["error"]["message"]
        self.assertLessEqual(len(public), 32_768)
        self.assertNotIn("/home/private", public)
        self.assertNotIn(r"C:\Users", public)
        self.assertNotIn("s3cr3t", public)
        self.assertNotIn("also-secret", public)
        self.assertIn("<private-path>", public)
        self.assertIn("<redacted>", public)

    def test_runtime_archive_is_deterministic_portable_and_excludes_bytecode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package" / "literate_ai"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            module = package / "worker.py"
            module.write_text("VALUE = 1\n", encoding="utf-8")
            # Windows chmod does not set POSIX executable bits on Python files.
            module.chmod(0o755)
            cache = package / "__pycache__"
            cache.mkdir()
            (cache / "worker.cpython-313.pyc").write_bytes(b"cached")
            (package / "legacy.pyo").write_bytes(b"cached")
            first = root / "first.zip"
            second = root / "second.zip"

            first_identity, first_size = _runtime_archive(package, first)
            second_identity, second_size = _runtime_archive(package, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_identity, second_identity)
            self.assertEqual(first_size, second_size)
            with zipfile.ZipFile(first) as archive:
                members = archive.infolist()
            self.assertEqual(
                [member.filename for member in members],
                ["literate_ai/__init__.py", "literate_ai/worker.py"],
            )
            self.assertTrue(
                all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in members)
            )
            self.assertEqual(
                [stat.S_IMODE(member.external_attr >> 16) for member in members],
                [0o644, 0o644 if os.name == "nt" else 0o755],
            )

    def test_runtime_archive_rejects_package_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package" / "literate_ai"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "linked.py").symlink_to("__init__.py")

            with self.assertRaises(RetainedHarnessRemoteError) as raised:
                _runtime_archive(package, root / "runtime.zip")

        self.assertEqual(
            raised.exception.code,
            "retained_receipt.remote_runtime_invalid",
        )

    def test_runtime_archive_carries_pinned_tools_for_admitted_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package" / "literate_ai"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            source = root / "source"
            (source / "tests").mkdir(parents=True)
            (source / "tests" / "run.sh").write_text(
                "cmake -G Ninja -S . -B build\n",
                encoding="utf-8",
            )
            runtime = root / "runtime.zip"
            inventory = {
                "stages": [
                    {"id": "build", "command": "uv build"},
                    {
                        "id": "test",
                        "command": "./run.sh",
                        "evidence": "tests/run.sh",
                    },
                ],
            }

            with patch(
                "literate_ai.adapters.retained_harness_remote._download_runtime_tool",
                side_effect=lambda descriptor: (
                    b"ninja-binary" if descriptor["name"] == "ninja" else b"uv-binary"
                ),
            ):
                _runtime_archive(
                    package,
                    runtime,
                    inventory=inventory,
                    source_root=source,
                )

            with zipfile.ZipFile(runtime) as archive:
                self.assertEqual(
                    archive.read("literate_ai_tools/x86_64/uv"),
                    b"uv-binary",
                )
                self.assertEqual(
                    archive.read("literate_ai_tools/x86_64/ninja"),
                    b"ninja-binary",
                )
                manifest = json.loads(archive.read("literate_ai_tools/manifest.json"))
            self.assertEqual(
                manifest["requirements"],
                ["ninja==1.13.2", "uv==0.12.12"],
            )
            self.assertEqual(
                retained_harness_runtime_requirements(
                    inventory,
                    source_root=source,
                ),
                ("ninja==1.13.2", "uv==0.12.12"),
            )

    def test_runtime_archive_supplies_current_receiver_over_exact_pythonpath(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "literate-ai-runtime.zip"
            package = Path(__file__).parents[2] / "src" / "literate_ai"
            _runtime_archive(package, runtime)
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(runtime)
            environment["PYTHONSAFEPATH"] = "1"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONNOUSERSITE"] = "1"
            completed = subprocess.run(
                (
                    sys.executable,
                    "-c",
                    (
                        "from literate_ai.adapters import retained_harness_remote as m;"
                        "from literate_ai.cli.dispatch import _parser;"
                        "a=_parser().parse_args(['worker','execute-retained',"
                        "'--worker-file','w','--request','r','--inventory','i',"
                        "'--archive','a','--runtime-archive','runtime.zip',"
                        "'--workspace','x']);"
                        "print(m.__file__);print(a.runtime_archive)"
                    ),
                ),
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                # Importing the complete deterministic runtime archive can exceed
                # ten seconds under parallel Windows hosted-runner I/O scanning.
                # Keep a finite deadline without mistaking that startup cost for
                # a receiver hang.
                timeout=60,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(
            Path(lines[0]),
            runtime / "literate_ai" / "adapters" / "retained_harness_remote.py",
        )
        self.assertEqual(lines[1], "runtime.zip")

    def _execute(self, root: Path, runner: RecordingRetainedRunner):
        source = root / "source"
        source.mkdir()
        (source / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        scope = capture_retained_source_scope(source)
        inventory = {
            "schema": HARNESS_INVENTORY_SCHEMA,
            "source_scope": scope,
        }
        source_identity = ContentIdentity.parse_uri(
            str(observe_retained_tree(source, scope)["source_tree"]["identity"])
        )
        worker = _worker()
        return RetainedHarnessSshExecutor(runner).execute(
            worker,
            project_id="fixture",
            project_revision_identity=_identity("project"),
            worker_catalog_identity=_identity("catalog"),
            runner_identity=_identity("runner"),
            lifecycle_request_identity=_identity("lifecycle-request"),
            inventory=inventory,
            inventory_bytes=canonical_json_bytes(inventory),
            source_root=source,
            source_identity=source_identity,
            timeout_seconds=30,
            cwd=root,
        )

    def test_mocked_transport_stages_only_bound_inputs_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRetainedRunner()
            result = self._execute(root, runner)

        self.assertEqual(result.platform["operating_system"], "linux")
        self.assertEqual(len(runner.calls), 8)
        self.assertEqual(runner.calls[0][0], "ssh")
        self.assertTrue(all(call[0] == "scp" for call in runner.calls[1:6]))
        self.assertEqual(
            {Path(call[-2]).name for call in runner.calls[1:6]},
            {
                "source.tar.gz",
                "literate-ai-runtime.zip",
                "inventory.json",
                "request.json",
                "worker.json",
            },
        )
        command = runner.calls[6][-1]
        self.assertIn('"$HOME"/.local/bin/litai', command)
        self.assertIn(
            'PYTHONPATH="$incoming/literate-ai-runtime.zip"',
            command,
        )
        self.assertNotIn("$PYTHONPATH", command)
        self.assertIn("LITAI_NO_SELF_UPDATE=1", command)
        self.assertIn("worker execute-retained", command)
        self.assertIn(
            '--runtime-archive "$incoming/literate-ai-runtime.zip"',
            command,
        )
        self.assertIn("rm -rf", runner.calls[7][-1])
        self.assertGreaterEqual(runner.timeouts[7], 299)
        self.assertFalse(
            any(str(root) in argument for call in runner.calls for argument in call)
        )

    def test_nonportable_source_guard_failure_is_typed_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRetainedRunner()
            with (
                patch(
                    "literate_ai.adapters.retained_harness_remote._source_archive",
                    side_effect=SourceGuardError(
                        "source symlink target is not portable: private/link"
                    ),
                ),
                self.assertRaises(RetainedHarnessRemoteError) as raised,
            ):
                self._execute(root, runner)

        self.assertEqual(
            raised.exception.code,
            "retained_receipt.remote_source_invalid",
        )
        self.assertIn("private/link", raised.exception.message)
        self.assertEqual(runner.calls, [])

    def test_response_worker_identity_tampering_is_rejected_after_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRetainedRunner(tamper_worker=True)
            with self.assertRaises(RetainedHarnessRemoteError) as raised:
                self._execute(root, runner)

        self.assertEqual(
            raised.exception.code, "retained_receipt.remote_identity_mismatch"
        )
        self.assertIn("rm -rf", runner.calls[-1][-1])

    def test_internal_receiver_validates_runtime_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
            scope = capture_retained_source_scope(source)
            inventory = {
                "schema": HARNESS_INVENTORY_SCHEMA,
                "source_scope": scope,
                "retained_padding": "x" * (1024 * 1024),
                "stages": [{"id": "build", "command": "uv build"}],
            }
            self.assertGreater(len(canonical_json_bytes(inventory)), 1024 * 1024)
            source_identity = ContentIdentity.parse_uri(
                str(observe_retained_tree(source, scope)["source_tree"]["identity"])
            )
            archive_path = root / "source.tar.gz"
            manifest, archive_identity, archive_size = _source_archive(
                source, inventory, archive_path
            )
            runtime_package = root / "runtime-package" / "literate_ai"
            runtime_package.mkdir(parents=True)
            (runtime_package / "__init__.py").write_text("", encoding="utf-8")
            runtime_path = root / "literate-ai-runtime.zip"
            with patch(
                "literate_ai.adapters.retained_harness_remote._download_runtime_tool",
                return_value=b"#!/bin/sh\nexit 0\n",
            ):
                runtime_identity, runtime_size = _runtime_archive(
                    runtime_package,
                    runtime_path,
                    inventory=inventory,
                )
            worker = _worker()
            request = RetainedHarnessRemoteRequest(
                "fixture",
                _identity("project"),
                worker.identity,
                _identity("catalog"),
                _identity("runner"),
                _identity("lifecycle-request"),
                canonical_identity(inventory),
                source_identity,
                manifest,
                archive_identity,
                archive_size,
                runtime_identity,
                runtime_size,
                30,
            )
            worker_path = root / "worker.json"
            request_path = root / "request.json"
            inventory_path = root / "inventory.json"
            worker_path.write_bytes(canonical_json_bytes(worker.to_dict()))
            request_path.write_bytes(canonical_json_bytes(request.to_dict()))
            inventory_path.write_bytes(canonical_json_bytes(inventory))
            workspace = root / "workspace"
            phase = {
                **_phase(),
                "stdout_excerpt": str(root / "private"),
                "stderr_excerpt": "secret-shaped diagnostic",
            }
            observed_environment = {}

            def execute_without_receiver_environment(*_args, **_kwargs):
                observed_environment.update(
                    {
                        name: os.environ.get(name)
                        for name in (
                            "PYTHONPATH",
                            "PYTHONSAFEPATH",
                            "PYTHONDONTWRITEBYTECODE",
                            "PYTHONNOUSERSITE",
                        )
                    }
                )
                observed_environment["uv"] = next(
                    (
                        Path(entry) / "uv"
                        for entry in os.environ["PATH"].split(os.pathsep)
                        if (Path(entry) / "uv").is_file()
                    ),
                    None,
                )
                return {"phases": [phase]}

            receiver_environment = {
                "PYTHONPATH": str(runtime_path),
                "PYTHONSAFEPATH": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            }
            with (
                patch.dict(os.environ, receiver_environment, clear=False),
                patch(
                    "literate_ai.adapters.retained_harness_remote."
                    "execute_retained_harness",
                    side_effect=execute_without_receiver_environment,
                ),
                patch(
                    "literate_ai.adapters.retained_harness_remote.platform.machine",
                    return_value="x86_64",
                ),
            ):
                result = execute_retained_harness_receiver(
                    worker_file=worker_path,
                    request_file=request_path,
                    inventory_file=inventory_path,
                    archive=archive_path,
                    runtime_archive=runtime_path,
                    workspace=workspace,
                )
                self.assertEqual(
                    {name: os.environ.get(name) for name in receiver_environment},
                    receiver_environment,
                )

            self.assertEqual(result.request_identity, request.identity)
            self.assertEqual(
                {name: observed_environment[name] for name in receiver_environment},
                {name: None for name in receiver_environment},
            )
            self.assertEqual(
                Path(observed_environment["uv"]).parts[-3:],
                (".literate-ai-tools", "bin", "uv"),
            )
            self.assertNotIn("stdout_excerpt", result.phases[0])
            self.assertNotIn("stderr_excerpt", result.phases[0])
            self.assertFalse(workspace.exists())
            original_runtime = runtime_path.read_bytes()
            tampered = bytearray(original_runtime)
            tampered[10] ^= 1
            runtime_path.write_bytes(tampered)

            with self.assertRaises(RetainedHarnessRemoteError) as raised:
                execute_retained_harness_receiver(
                    worker_file=worker_path,
                    request_file=request_path,
                    inventory_file=inventory_path,
                    archive=archive_path,
                    runtime_archive=runtime_path,
                    workspace=workspace,
                )

            self.assertEqual(
                raised.exception.code,
                "retained_receipt.remote_runtime_invalid",
            )
            self.assertFalse(workspace.exists())
            runtime_path.write_bytes(original_runtime)
            wrong_size_request = replace(
                request,
                runtime_archive_size=request.runtime_archive_size + 1,
            )
            request_path.write_bytes(canonical_json_bytes(wrong_size_request.to_dict()))

            with self.assertRaises(RetainedHarnessRemoteError) as raised:
                execute_retained_harness_receiver(
                    worker_file=worker_path,
                    request_file=request_path,
                    inventory_file=inventory_path,
                    archive=archive_path,
                    runtime_archive=runtime_path,
                    workspace=workspace,
                )

            self.assertEqual(
                raised.exception.code,
                "retained_receipt.remote_runtime_invalid",
            )


if __name__ == "__main__":
    unittest.main()
