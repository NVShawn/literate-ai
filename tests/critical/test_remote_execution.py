from __future__ import annotations

import hashlib
import os
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.adapters.remote_execution import (
    RemoteExecutionError,
    _write_deterministic_tar_gz,
    import_remote_evidence_bundle,
    materialize_and_execute,
    remote_control_summary,
)
from literate_ai.adapters.source_materialization import capture_source_archive
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
    NvidiaProbeStatus,
    ObservedExecutionEnvironment,
    ObservedGpuDevice,
    RemoteFailureDiagnostic,
    WorkerHardwareObservation,
)
from literate_ai.remote_source_guard import source_tree_identity


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)


def worker() -> ExecutionWorker:
    return ExecutionWorker(
        "ssh",
        ExecutionWorkerKind.SSH,
        target_profile="linux-host",
        requirements=ExecutionRequirements(os_family="linux"),
        endpoint="user@host",
        workspace="~/literate-ai",
    )


def request(
    action: LifecycleDispatchAction,
    *,
    artifact_reference=None,
    arguments: tuple[str, ...] = (),
    entrypoint: str | None = None,
) -> ExecutionDispatchRequest:
    selected = worker()
    return ExecutionDispatchRequest(
        action,
        "component://example/app",
        "components/app",
        selected.target_profile,
        ("+flavor://example/os-linux",),
        selected.identity,
        selected.requirements,
        (),
        arguments,
        identity("1"),
        identity("2"),
        identity("3"),
        identity("4"),
        identity("5"),
        identity("6"),
        identity("7"),
        artifact_reference,
        30,
        entrypoint=entrypoint,
    )


def materialization(
    dispatch_request: ExecutionDispatchRequest, project: Path
) -> ExecutionSourceMaterialization:
    return ExecutionSourceMaterialization(
        ExecutionSourceMaterializationKind.ARCHIVE,
        dispatch_request.identity,
        ContentIdentity.parse_uri(source_tree_identity(project)),
        archive_reference=ContentReference(
            "source-archive", "staged:source.tar.gz", identity("9")
        ),
    )


def observation() -> WorkerHardwareObservation:
    return WorkerHardwareObservation(
        "local-probe",
        "2026-08-12T00:00:00Z",
        "linux",
        "ubuntu",
        "24.04",
        "x86_64",
        4,
        8,
        16384,
        (
            ObservedGpuDevice(
                "nvidia",
                "NVIDIA RTX PRO 4500 Blackwell Generation",
                index=0,
                memory_mib=24564,
                compute_capability="12.0",
            ),
        ),
        NvidiaProbeStatus.OK,
    )


class RemoteExecutionTests(unittest.TestCase):
    def test_control_summary_is_redacted(self) -> None:
        failure = RemoteFailureDiagnostic(
            "lifecycle.failed",
            "password=<redacted> at <private-path>",
            stderr="diagnostic detail",
        )
        result = ExecutionDispatchResult(
            identity("1"),
            identity("2"),
            "task",
            DispatchResultStatus.FAILED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(identity("5"),),
            ),
            1,
            None,
            identity("3"),
            "",
            "token=hunter2 at /Users/worker/private/build.log",
            failure.identity.uri,
        )
        summary = remote_control_summary(result)
        self.assertNotIn("hunter2", summary)
        self.assertNotIn("/Users/worker", summary)
        self.assertIn("<redacted>", summary)
        self.assertIn("<private-path>", summary)

    def test_materialize_and_execute_fails_closed_on_cache_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            cache_root = project / "generated" / "accepted-source-cache"
            cache_root.mkdir(parents=True)
            (cache_root / "format.json").write_text("{}", encoding="utf-8")

            build_request = replace(
                request(LifecycleDispatchAction.BUILD), accepted_source_only=True
            )
            with patch.dict(os.environ, {"BUILD_DIR": str(project / "generated")}):
                captured = capture_source_archive(
                    project, build_request, directory=staging
                )
            self.assertIsNotNone(captured.accepted_source_cache_path)
            captured.accepted_source_cache_path.write_bytes(b"tampered")

            with self.assertRaises(RemoteExecutionError) as raised:
                materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=root / "workspace",
                    cas_root=root / "cas",
                    rebuild=Mock(),
                    accepted_source_cache_archive=captured.accepted_source_cache_path,
                )
            self.assertEqual(
                raised.exception.code,
                "execution.remote_accepted_source_cache_mismatch",
            )

    def test_remote_evidence_import_rejects_tampered_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            build_request = request(LifecycleDispatchAction.BUILD)
            captured = capture_source_archive(project, build_request, directory=staging)

            def rebuild(**values):
                custody = values["custody"]
                custody.runtime_root.mkdir()
                artifact = custody.runtime_root / "app.bin"
                artifact.write_bytes(b"binary")
                return {
                    "artifact": str(artifact),
                    "execution_command": {
                        "argv": [__import__("sys").executable, str(artifact)],
                        "environment": {},
                    },
                    "observed_toolchain_identities": [identity("5").uri],
                    "runtime_root": str(custody.runtime_root),
                }

            bundle = staging / "remote-evidence.tar.gz"
            with patch(
                "literate_ai.adapters.remote_execution.probe_worker_capabilities",
                return_value=observation(),
            ):
                result = materialize_and_execute(
                    worker(),
                    build_request,
                    captured.materialization,
                    archive=captured.path,
                    workspace=root
                    / (build_request.identity.digest[:24] + "-tamper-attempt"),
                    cas_root=root / "worker-cas",
                    rebuild=rebuild,
                    evidence_output=bundle,
                    cleanup_ticket=staging / "cleanup-ticket.json",
                )
            assert result.evidence_manifest is not None
            assert result.evidence_reference is not None

            tampered = staging / "tampered.tar.gz"
            tampered.write_bytes(bundle.read_bytes() + b"tampered")
            with self.assertRaises(RemoteExecutionError) as raised:
                import_remote_evidence_bundle(
                    tampered,
                    result.evidence_manifest,
                    expected_bundle_identity=result.evidence_reference.identity,
                    store_root=root / "coordinator-cas",
                )
            self.assertEqual(
                raised.exception.code, "execution.remote_evidence_bundle_mismatch"
            )

            extracted = staging / "extracted"
            extracted.mkdir()
            with tarfile.open(bundle, mode="r:gz") as archive:
                archive.extractall(extracted, filter="data")
            artifact_path = next(extracted.glob("artifact/*"))
            artifact_path.unlink()
            missing = staging / "missing.tar.gz"
            missing_identity = _write_deterministic_tar_gz(extracted, missing)
            with self.assertRaises(RemoteExecutionError) as raised:
                import_remote_evidence_bundle(
                    missing,
                    result.evidence_manifest,
                    expected_bundle_identity=missing_identity,
                    store_root=root / "coordinator-cas",
                )
            self.assertEqual(
                raised.exception.code, "execution.remote_evidence_file_mismatch"
            )

            duplicate = staging / "duplicate.tar.gz"
            duplicate_source = next(
                item for item in extracted.rglob("*") if item.is_file()
            )
            with tarfile.open(duplicate, mode="w:gz") as archive:
                for item in sorted(extracted.rglob("*")):
                    if item.is_file():
                        archive.add(
                            item, arcname=item.relative_to(extracted).as_posix()
                        )
                archive.add(
                    duplicate_source,
                    arcname=duplicate_source.relative_to(extracted).as_posix(),
                )
            duplicate_identity = ContentIdentity(
                HashAlgorithm.SHA256,
                hashlib.sha256(duplicate.read_bytes()).hexdigest(),
            )
            with self.assertRaises(RemoteExecutionError) as raised:
                import_remote_evidence_bundle(
                    duplicate,
                    result.evidence_manifest,
                    expected_bundle_identity=duplicate_identity,
                    store_root=root / "coordinator-cas",
                )
            self.assertEqual(
                raised.exception.code, "execution.remote_evidence_bundle_invalid"
            )


if __name__ == "__main__":
    unittest.main()
