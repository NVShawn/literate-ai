from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_remote_execution``."""


from pathlib import Path

from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    ExecutionDispatchRequest,
    ExecutionRequirements,
    ExecutionSourceMaterialization,
    ExecutionSourceMaterializationKind,
    ExecutionWorker,
    ExecutionWorkerKind,
    HashAlgorithm,
    LifecycleDispatchAction,
    NvidiaProbeStatus,
    ObservedGpuDevice,
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
