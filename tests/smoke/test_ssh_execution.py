from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.execution_dispatch import ExecutionDispatchAdapterError
from literate_ai.adapters.source_materialization import CapturedSourceArchive
from literate_ai.adapters.ssh_execution import SshLifecycleRequestHandler
from literate_ai.adapters.ssh_transport import SshProcessResult
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


class SshExecutionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
