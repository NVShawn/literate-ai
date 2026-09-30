from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.adapters.execution_dispatch import ExecutionDispatchAdapterError
from literate_ai.adapters.source_materialization import CapturedSourceArchive
from literate_ai.adapters.ssh_execution import SshLifecycleRequestHandler, _Deadline
from literate_ai.adapters.ssh_transport import SshProcessResult, SshTransportError
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionRequirements,
    ExecutionSourceMaterialization,
    ExecutionSourceMaterializationKind,
    ExecutionWorker,
    ExecutionWorkerKind,
    HashAlgorithm,
    LifecycleDispatchAction,
    ObservedExecutionEnvironment,
    RemoteExecutionControlResult,
    RemoteLifecycleEvidenceManifest,
    canonical_json_bytes,
)


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)


def worker() -> ExecutionWorker:
    return ExecutionWorker(
        "ssh",
        ExecutionWorkerKind.SSH,
        endpoint="user@host",
        workspace="~/literate-ai",
        requirements=ExecutionRequirements(os_family="linux"),
    )


def request() -> ExecutionDispatchRequest:
    selected = worker()
    return ExecutionDispatchRequest(
        LifecycleDispatchAction.BUILD,
        "component://example/app",
        "components/app",
        "host",
        ("+flavor://example/os-linux",),
        selected.identity,
        selected.requirements,
        (),
        (),
        identity("1"),
        identity("2"),
        identity("3"),
        identity("4"),
        identity("5"),
        identity("6"),
        identity("7"),
        None,
        30,
    )


class RecordingRunner:
    def __init__(self, result: ExecutionDispatchResult) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.executed = False
        manifest = RemoteLifecycleEvidenceManifest(
            result.request_identity,
            identity("a"),
            result.worker_identity,
            identity("b"),
            identity("c"),
            identity("d"),
            identity("e"),
            identity("f"),
            identity("1"),
            identity("2"),
            "build",
            result.status.value,
            result.evidence_identity,
            identity("3"),
            result.artifact_reference.identity,
            False,
            (),
            (),
            None,
            "0" * 32,
        )
        self.result = replace(
            result,
            evidence_manifest=manifest,
            evidence_reference=ContentReference(
                "remote-lifecycle-evidence",
                "staged:remote-evidence.tar.gz",
                identity("d"),
            ),
        )
        self.control = RemoteExecutionControlResult.from_dispatch_result(
            self.result,
            manifest_size=len(
                canonical_json_bytes(self.result.evidence_manifest.to_dict())
            ),
            bundle_size=4096,
            redacted_summary="completed",
        )

    def run(self, argv, *, cwd, timeout_seconds):
        self.calls.append(tuple(argv))
        if len(self.calls) == 6:
            self.executed = True
            output = canonical_json_bytes(
                {
                    "schema": "literate-ai/cli-result@1",
                    "ok": True,
                    "command": "worker.execute",
                    "result": self.control.to_dict(),
                }
            )
            return SshProcessResult(0, output, b"")
        if self.executed and argv[0] != "scp":
            assert self.result.evidence_manifest is not None
            assert self.result.evidence_reference is not None
            output = canonical_json_bytes(
                {
                    "schema": "literate-ai/cli-result@1",
                    "ok": True,
                    "command": "worker.acknowledge",
                    "result": {
                        "status": "cleaned",
                        "manifest_identity": (
                            self.result.evidence_manifest.identity.uri
                        ),
                        "bundle_identity": self.result.evidence_reference.identity.uri,
                        "acknowledgement_identity": identity("e").uri,
                    },
                }
            )
            return SshProcessResult(0, output, b"")
        return SshProcessResult(0, b"", b"")


class RetryingCustodyRunner(RecordingRunner):
    def __init__(
        self,
        result: ExecutionDispatchResult,
        *,
        download_failures: int,
        acknowledgement_failures: int,
    ) -> None:
        super().__init__(result)
        self.download_failures = download_failures
        self.acknowledgement_failures = acknowledgement_failures

    def run(self, argv, *, cwd, timeout_seconds):
        completed = super().run(argv, cwd=cwd, timeout_seconds=timeout_seconds)
        if self.executed and argv[0] == "scp" and self.download_failures:
            self.download_failures -= 1
            Path(argv[-1]).write_bytes(b"partial disconnected transfer")
            return SshProcessResult(1, b"", b"disconnected")
        if (
            self.executed
            and argv[0] != "scp"
            and len(self.calls) > 6
            and self.acknowledgement_failures
        ):
            self.acknowledgement_failures -= 1
            return SshProcessResult(1, b"", b"acknowledgement disconnected")
        return completed


class FailingReceiverRunner:
    def __init__(self, stderr: bytes) -> None:
        self.calls = 0
        self.stderr = stderr

    def run(self, argv, *, cwd, timeout_seconds):
        del argv, cwd, timeout_seconds
        self.calls += 1
        if self.calls == 6:
            return SshProcessResult(2, b"", self.stderr)
        return SshProcessResult(0, b"", b"")


class FailingSetupRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, argv, *, cwd, timeout_seconds):
        del argv, cwd, timeout_seconds
        self.calls += 1
        return SshProcessResult(1, b"", b"untyped remote diagnostic")


class OversizedControlRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, argv, *, cwd, timeout_seconds):
        del argv, cwd, timeout_seconds
        self.calls += 1
        if self.calls == 6:
            return SshProcessResult(0, b"x" * (1024 * 1024 + 1), b"")
        return SshProcessResult(0, b"", b"")


class SshExecutionTests(unittest.TestCase):
    def test_windows_handler_does_not_require_a_remote_bash(self) -> None:
        selected = ExecutionWorker(
            "windows",
            ExecutionWorkerKind.SSH,
            endpoint="user@windows",
            workspace="~/literate-ai",
            requirements=ExecutionRequirements(os_family="windows"),
        )
        command = SshLifecycleRequestHandler._setup_command(
            selected,
            "~/literate-ai/requests/request",
            "~/literate-ai/work/request",
        )
        runner = Mock()
        runner.run.return_value = SshProcessResult(0, b"", b"")

        SshLifecycleRequestHandler(runner)._run(
            selected,
            command,
            Path.cwd(),
            _Deadline(time.monotonic() + 10, 10),
        )

        argv = runner.run.call_args.args[0]
        self.assertEqual(argv[-1], command)
        self.assertNotIn("bash", argv[-1])

    def test_handler_fails_fast_when_staging_setup_fails(self) -> None:
        selected = worker()
        dispatch_request = request()
        runner = FailingSetupRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"archive")
            materialization = ExecutionSourceMaterialization(
                ExecutionSourceMaterializationKind.ARCHIVE,
                dispatch_request.identity,
                identity("a"),
                archive_reference=ContentReference(
                    "source-archive", "staged:source.tar.gz", identity("b")
                ),
            )
            with (
                patch(
                    "literate_ai.adapters.ssh_execution.capture_source_archive",
                    return_value=CapturedSourceArchive(archive, materialization),
                ),
                self.assertRaisesRegex(
                    ExecutionDispatchAdapterError,
                    "SSH transport phase exited with status 1",
                ) as raised,
            ):
                SshLifecycleRequestHandler(runner).execute(
                    selected, dispatch_request, cwd=root
                )

        self.assertEqual(raised.exception.code, "execution.ssh_transport_phase_failed")
        self.assertEqual(runner.calls, 1)

    def test_handler_rejects_oversized_malformed_worker_control(self) -> None:
        selected = worker()
        dispatch_request = request()
        archive_reference = ContentReference(
            "source-archive", "staged:source.tar.gz", identity("b")
        )
        materialization = ExecutionSourceMaterialization(
            ExecutionSourceMaterializationKind.ARCHIVE,
            dispatch_request.identity,
            identity("a"),
            archive_reference=archive_reference,
        )
        runner = OversizedControlRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"archive")
            with (
                patch(
                    "literate_ai.adapters.ssh_execution.capture_source_archive",
                    return_value=CapturedSourceArchive(archive, materialization),
                ),
                self.assertRaises(ExecutionDispatchAdapterError) as raised,
            ):
                SshLifecycleRequestHandler(runner).execute(
                    selected, dispatch_request, cwd=root
                )
        self.assertEqual(raised.exception.code, "execution.ssh_output_too_large")

    def test_windows_receiver_discovers_cmd_exe_and_private_venv_launchers(
        self,
    ) -> None:
        selected = ExecutionWorker(
            "windows",
            ExecutionWorkerKind.SSH,
            endpoint="user@windows",
            workspace="~/literate-ai",
            requirements=ExecutionRequirements(os_family="windows"),
        )

        command = SshLifecycleRequestHandler._execute_command(
            selected,
            "~/literate-ai/requests/request",
            "~/literate-ai/work/request",
            "~/literate-ai/cache/worker-cas",
        )

        self.assertIn("powershell.exe", command)
        decoded = (
            __import__("base64")
            .b64decode(command.rsplit(" ", 1)[1])
            .decode("utf-16-le")
        )
        self.assertIn(".local\\bin\\litai.cmd", decoded)
        self.assertIn(".local\\bin\\litai.exe", decoded)
        self.assertIn(".local\\share\\literate-ai\\venv\\Scripts\\litai.cmd", decoded)
        self.assertIn(".local\\share\\literate-ai\\venv\\Scripts\\litai.exe", decoded)
        self.assertIn("$litai.FullName", decoded)
        self.assertLess(
            decoded.index(".local\\share\\literate-ai\\venv\\Scripts\\litai.exe"),
            decoded.index("Get-Command litai"),
        )
        acknowledgement = SshLifecycleRequestHandler._acknowledgement_command(
            selected,
            "~/literate-ai/requests/request",
            "~/literate-ai/cache/worker-cas/pending/request.json",
            "~/literate-ai/cache/worker-cas/acknowledgements",
            identity("a"),
            identity("b"),
        )
        decoded_acknowledgement = (
            __import__("base64")
            .b64decode(acknowledgement.rsplit(" ", 1)[1])
            .decode("utf-16-le")
        )
        self.assertLess(
            decoded_acknowledgement.index(
                ".local\\share\\literate-ai\\venv\\Scripts\\litai.exe"
            ),
            decoded_acknowledgement.index("Get-Command litai"),
        )
        self.assertIn("$ProgressPreference='SilentlyContinue'", decoded)

    def test_posix_receiver_adds_conventional_tool_locations_to_path(self) -> None:
        command = SshLifecycleRequestHandler._execute_command(
            worker(),
            "~/literate-ai/requests/request",
            "~/literate-ai/work/request",
            "~/literate-ai/cache/worker-cas",
        )

        self.assertIn(
            'PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"', command
        )
        self.assertIn("export PATH", command)
        self.assertIn(
            "$HOME/.local/share/literate-ai/venv/bin/litai",
            command,
        )
        self.assertLess(
            command.index("$HOME/.local/share/literate-ai/venv/bin/litai"),
            command.index("command -v litai"),
        )
        acknowledgement = SshLifecycleRequestHandler._acknowledgement_command(
            worker(),
            "~/literate-ai/requests/request",
            "~/literate-ai/cache/worker-cas/pending/request.json",
            "~/literate-ai/cache/worker-cas/acknowledgements",
            identity("a"),
            identity("b"),
        )
        self.assertLess(
            acknowledgement.index("$HOME/.local/share/literate-ai/venv/bin/litai"),
            acknowledgement.index("command -v litai"),
        )

    def test_receivers_use_configured_lifecycle_executable_without_discovery(
        self,
    ) -> None:
        linux = ExecutionWorker(
            "linux",
            ExecutionWorkerKind.SSH,
            endpoint="user@linux",
            workspace="~/literate-ai",
            requirements=ExecutionRequirements(os_family="linux"),
            lifecycle_executable="/opt/literate-ai/verified/bin/litai",
        )
        linux_command = SshLifecycleRequestHandler._execute_command(
            linux,
            "~/literate-ai/requests/request",
            "~/literate-ai/work/request",
            "~/literate-ai/cache/worker-cas",
        )
        self.assertIn(
            "litai_command=/opt/literate-ai/verified/bin/litai", linux_command
        )
        self.assertNotIn("command -v litai", linux_command)
        self.assertNotIn("$HOME/.local/share/literate-ai", linux_command)
        linux_acknowledgement = SshLifecycleRequestHandler._acknowledgement_command(
            linux,
            "~/literate-ai/requests/request",
            "~/literate-ai/cache/worker-cas/pending/request.json",
            "~/literate-ai/cache/worker-cas/acknowledgements",
            identity("a"),
            identity("b"),
        )
        self.assertIn(
            "litai_command=/opt/literate-ai/verified/bin/litai",
            linux_acknowledgement,
        )
        self.assertNotIn("command -v litai", linux_acknowledgement)

        windows = ExecutionWorker(
            "windows",
            ExecutionWorkerKind.SSH,
            endpoint="user@windows",
            workspace="~/literate-ai",
            requirements=ExecutionRequirements(os_family="windows"),
            lifecycle_executable=("~/AppData/Local/literate-ai/venv/Scripts/litai.cmd"),
        )
        windows_command = SshLifecycleRequestHandler._execute_command(
            windows,
            "~/literate-ai/requests/request",
            "~/literate-ai/work/request",
            "~/literate-ai/cache/worker-cas",
        )
        decoded = (
            __import__("base64")
            .b64decode(windows_command.rsplit(" ", 1)[1])
            .decode("utf-16-le")
        )
        self.assertIn(
            "$litaiPath=Join-Path $HOME "
            "'AppData/Local/literate-ai/venv/Scripts/litai.cmd'",
            decoded,
        )
        self.assertNotIn("Get-Command litai", decoded)
        self.assertNotIn(".local\\share\\literate-ai", decoded)

    def test_handler_stages_four_files_and_returns_one_typed_result(self) -> None:
        selected = worker()
        dispatch_request = request()
        reference = ContentReference(
            "artifact-export", "litai-worker-cas:sha256:" + "8" * 64, identity("8")
        )
        result = ExecutionDispatchResult(
            dispatch_request.identity,
            selected.identity,
            "task",
            DispatchResultStatus.PASSED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(identity("5"),),
            ),
            0,
            reference,
            identity("9"),
        )
        runner = RecordingRunner(result)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"archive")
            materialization = ExecutionSourceMaterialization(
                ExecutionSourceMaterializationKind.ARCHIVE,
                dispatch_request.identity,
                identity("a"),
                archive_reference=ContentReference(
                    "source-archive", "staged:source.tar.gz", identity("b")
                ),
            )
            with (
                patch(
                    "literate_ai.adapters.ssh_execution.capture_source_archive",
                    return_value=CapturedSourceArchive(archive, materialization),
                ),
                patch(
                    "literate_ai.adapters.ssh_execution.load_and_import_remote_evidence_bundle",
                    return_value=(runner.result.evidence_manifest, identity("f"), ()),
                ),
            ):
                actual = SshLifecycleRequestHandler(runner).execute(
                    selected, dispatch_request, cwd=root
                )

        self.assertEqual(actual.request_identity, result.request_identity)
        self.assertIsNotNone(actual.custody_receipt)
        self.assertEqual(len(runner.calls), 8)
        self.assertEqual(runner.calls[0][0], "ssh")
        self.assertTrue(all(call[0] == "scp" for call in runner.calls[1:5]))
        self.assertEqual(runner.calls[5][0], "ssh")
        self.assertEqual(runner.calls[6][0], "scp")
        self.assertEqual(runner.calls[7][0], "ssh")
        self.assertTrue(
            all(
                any("user@host" in argument for argument in call)
                for call in runner.calls
            )
        )
        self.assertFalse(
            any(
                dispatch_request.component in argument
                for call in runner.calls
                for argument in call
            )
        )
        staged = [argument for argument in runner.calls[1] if "requests/" in argument]
        self.assertEqual(len(staged), 1)
        run_key = staged[0].split("requests/", 1)[1].split("/", 1)[0]
        self.assertTrue(run_key.startswith(dispatch_request.identity.digest[:24] + "-"))
        self.assertEqual(len(run_key), 37)

    def test_handler_uses_the_worker_configured_transport_for_ssh_calls_only(
        self,
    ) -> None:
        # A private fleet may route some workers through a drop-in SSH-compatible
        # wrapper instead of plain OpenSSH; scp-shaped file staging stays on scp
        # regardless (see issue #65).
        selected = ExecutionWorker(
            "ssh",
            ExecutionWorkerKind.SSH,
            endpoint="user@host",
            workspace="~/literate-ai",
            requirements=ExecutionRequirements(os_family="linux"),
            transport="s",
        )
        dispatch_request = request()
        reference = ContentReference(
            "artifact-export", "litai-worker-cas:sha256:" + "8" * 64, identity("8")
        )
        result = ExecutionDispatchResult(
            dispatch_request.identity,
            selected.identity,
            "task",
            DispatchResultStatus.PASSED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(identity("5"),),
            ),
            0,
            reference,
            identity("9"),
        )
        runner = RecordingRunner(result)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"archive")
            materialization = ExecutionSourceMaterialization(
                ExecutionSourceMaterializationKind.ARCHIVE,
                dispatch_request.identity,
                identity("a"),
                archive_reference=ContentReference(
                    "source-archive", "staged:source.tar.gz", identity("b")
                ),
            )
            with (
                patch(
                    "literate_ai.adapters.ssh_execution.capture_source_archive",
                    return_value=CapturedSourceArchive(archive, materialization),
                ),
                patch(
                    "literate_ai.adapters.ssh_execution.load_and_import_remote_evidence_bundle",
                    return_value=(runner.result.evidence_manifest, identity("f"), ()),
                ),
            ):
                SshLifecycleRequestHandler(runner).execute(
                    selected, dispatch_request, cwd=root
                )

        self.assertEqual(runner.calls[0][0], "s")
        self.assertTrue(all(call[0] == "scp" for call in runner.calls[1:5]))
        self.assertEqual(runner.calls[5][0], "s")
        self.assertEqual(runner.calls[6][0], "scp")
        self.assertEqual(runner.calls[7][0], "s")

    def test_handler_retries_transfer_and_idempotent_cleanup_acknowledgement(
        self,
    ) -> None:
        selected = worker()
        dispatch_request = request()
        result = ExecutionDispatchResult(
            dispatch_request.identity,
            selected.identity,
            "task",
            DispatchResultStatus.PASSED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(identity("5"),),
            ),
            0,
            ContentReference(
                "artifact-export",
                "litai-worker-cas:sha256:" + "8" * 64,
                identity("8"),
            ),
            identity("9"),
        )
        runner = RetryingCustodyRunner(
            result,
            download_failures=2,
            acknowledgement_failures=2,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"archive")
            materialization = ExecutionSourceMaterialization(
                ExecutionSourceMaterializationKind.ARCHIVE,
                dispatch_request.identity,
                identity("a"),
                archive_reference=ContentReference(
                    "source-archive", "staged:source.tar.gz", identity("b")
                ),
            )
            with (
                patch(
                    "literate_ai.adapters.ssh_execution.capture_source_archive",
                    return_value=CapturedSourceArchive(archive, materialization),
                ),
                patch(
                    "literate_ai.adapters.ssh_execution.load_and_import_remote_evidence_bundle",
                    return_value=(runner.result.evidence_manifest, identity("f"), ()),
                ),
            ):
                actual = SshLifecycleRequestHandler(runner).execute(
                    selected, dispatch_request, cwd=root
                )

        self.assertIsNotNone(actual.custody_receipt)
        self.assertEqual(len(runner.calls), 12)
        self.assertEqual(sum(call[0] == "scp" for call in runner.calls), 7)

    def test_handler_fails_closed_without_cleanup_when_transfer_never_arrives(
        self,
    ) -> None:
        selected = worker()
        dispatch_request = request()
        result = ExecutionDispatchResult(
            dispatch_request.identity,
            selected.identity,
            "task",
            DispatchResultStatus.PASSED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(identity("5"),),
            ),
            0,
            ContentReference(
                "artifact-export",
                "litai-worker-cas:sha256:" + "8" * 64,
                identity("8"),
            ),
            identity("9"),
        )
        runner = RetryingCustodyRunner(
            result,
            download_failures=3,
            acknowledgement_failures=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"archive")
            materialization = ExecutionSourceMaterialization(
                ExecutionSourceMaterializationKind.ARCHIVE,
                dispatch_request.identity,
                identity("a"),
                archive_reference=ContentReference(
                    "source-archive", "staged:source.tar.gz", identity("b")
                ),
            )
            with (
                patch(
                    "literate_ai.adapters.ssh_execution.capture_source_archive",
                    return_value=CapturedSourceArchive(archive, materialization),
                ),
                self.assertRaises(ExecutionDispatchAdapterError) as raised,
            ):
                SshLifecycleRequestHandler(runner).execute(
                    selected, dispatch_request, cwd=root
                )

        self.assertEqual(raised.exception.code, "execution.ssh_transport_phase_failed")
        self.assertEqual(len(runner.calls), 9)
        self.assertTrue(all(call[0] == "scp" for call in runner.calls[6:]))

    def test_handler_preserves_one_typed_receiver_failure(self) -> None:
        selected = worker()
        dispatch_request = request()
        archive_reference = ContentReference(
            "source-archive", "staged:source.tar.gz", identity("b")
        )
        materialization = ExecutionSourceMaterialization(
            ExecutionSourceMaterializationKind.ARCHIVE,
            dispatch_request.identity,
            identity("a"),
            archive_reference=archive_reference,
        )
        error = canonical_json_bytes(
            {
                "schema": "literate-ai/cli-error@1",
                "ok": False,
                "command": "worker.execute",
                "error": {
                    "code": "coding_cli.workspace_write_unavailable",
                    "message": "coding CLI did not write the detached workspace",
                },
            }
        )
        runner = FailingReceiverRunner(error)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"archive")
            with (
                patch(
                    "literate_ai.adapters.ssh_execution.capture_source_archive",
                    return_value=CapturedSourceArchive(archive, materialization),
                ),
                self.assertRaisesRegex(
                    ExecutionDispatchAdapterError,
                    "coding CLI did not write the detached workspace",
                ) as raised,
            ):
                SshLifecycleRequestHandler(runner).execute(
                    selected, dispatch_request, cwd=root
                )

        self.assertEqual(
            raised.exception.code, "coding_cli.workspace_write_unavailable"
        )

    def test_handler_preserves_typed_failure_after_verbose_diagnostics(self) -> None:
        selected = worker()
        dispatch_request = request()
        materialization = ExecutionSourceMaterialization(
            ExecutionSourceMaterializationKind.ARCHIVE,
            dispatch_request.identity,
            identity("a"),
            archive_reference=ContentReference(
                "source-archive", "staged:source.tar.gz", identity("b")
            ),
        )
        error = canonical_json_bytes(
            {
                "schema": "literate-ai/cli-error@1",
                "ok": False,
                "command": "worker.execute",
                "error": {
                    "code": "lifecycle.failed",
                    "message": "lifecycle command failed",
                },
            }
        )
        runner = FailingReceiverRunner(
            b"\x80[litai:exception] lifecycle: RuntimeError: useful detail\n" + error
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"archive")
            with (
                patch(
                    "literate_ai.adapters.ssh_execution.capture_source_archive",
                    return_value=CapturedSourceArchive(archive, materialization),
                ),
                self.assertRaisesRegex(
                    ExecutionDispatchAdapterError, "lifecycle command failed"
                ) as raised,
            ):
                SshLifecycleRequestHandler(runner).execute(
                    selected, dispatch_request, cwd=root
                )

        self.assertEqual(raised.exception.code, "lifecycle.failed")

    def test_handler_does_not_expose_untyped_receiver_diagnostics(self) -> None:
        selected = worker()
        dispatch_request = request()
        materialization = ExecutionSourceMaterialization(
            ExecutionSourceMaterializationKind.ARCHIVE,
            dispatch_request.identity,
            identity("a"),
            archive_reference=ContentReference(
                "source-archive", "staged:source.tar.gz", identity("b")
            ),
        )
        runner = FailingReceiverRunner(b"secret provider diagnostic")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            archive.write_bytes(b"archive")
            with (
                patch(
                    "literate_ai.adapters.ssh_execution.capture_source_archive",
                    return_value=CapturedSourceArchive(archive, materialization),
                ),
                self.assertRaisesRegex(
                    ExecutionDispatchAdapterError, "exited with status 2"
                ) as raised,
            ):
                SshLifecycleRequestHandler(runner).execute(
                    selected, dispatch_request, cwd=root
                )

        self.assertEqual(raised.exception.code, "execution.ssh_receiver_failed")


class DeadlineTests(unittest.TestCase):
    def test_remaining_never_exceeds_the_original_budget(self):
        # A deadline struck from a large monotonic() base (e.g. long system uptime)
        # can, after floating-point subtraction, round a hair past timeout_seconds
        # even though no real time has elapsed -- reproduce that deterministically
        # instead of depending on real clock skew.
        base = 123_456.000000000003
        deadline = _Deadline(base + 19, 19)
        with patch(
            "literate_ai.adapters.ssh_execution.time.monotonic", return_value=base
        ):
            remaining = deadline.remaining()
        self.assertIsInstance(remaining, int)
        self.assertLessEqual(remaining, 19)
        self.assertGreaterEqual(remaining, 1)

    def test_remaining_truncates_to_whole_seconds(self):
        deadline = _Deadline(104.9, 19)
        with patch(
            "literate_ai.adapters.ssh_execution.time.monotonic", return_value=100.0
        ):
            self.assertEqual(deadline.remaining(), 4)

    def test_remaining_raises_at_or_past_the_deadline(self):
        deadline = _Deadline(100.0, 19)
        with patch(
            "literate_ai.adapters.ssh_execution.time.monotonic", return_value=100.0
        ):
            with self.assertRaises(SshTransportError) as raised:
                deadline.remaining()
        self.assertEqual(raised.exception.code, "execution.ssh_timed_out")


if __name__ == "__main__":
    unittest.main()
