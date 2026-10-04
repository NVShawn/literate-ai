"""Shared test fixtures extracted from test_cli_command_worker_lifecycle."""

from __future__ import annotations

from pathlib import Path

from literate_ai.contracts import (
    ContentReference,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionWorker,
    LifecycleDispatchAction,
    ObservedExecutionEnvironment,
    canonical_identity,
)


def _request(
    _args: object,
    *,
    component: str,
    selected: object,
    action: LifecycleDispatchAction,
    artifact_reference: ContentReference | None = None,
    application_arguments: tuple[str, ...] = (),
    entrypoint: str | None = None,
    **_: object,
) -> ExecutionDispatchRequest:
    worker = selected.worker
    identities = tuple(canonical_identity({"authority": index}) for index in range(7))
    accepted_source_only = bool(getattr(_args, "from_accepted_source", False))
    provider_identity = canonical_identity({"provider": "accepted-source"})
    return ExecutionDispatchRequest(
        action,
        component,
        component,
        worker.target_profile,
        (),
        worker.identity,
        worker.requirements,
        selected.parameters,
        application_arguments,
        *identities,
        artifact_reference,
        60,
        accepted_source_only=accepted_source_only,
        accepted_source_provider_id=(
            "accepted-source" if accepted_source_only else None
        ),
        accepted_source_provider_identity=(
            provider_identity if accepted_source_only else None
        ),
        entrypoint=entrypoint,
    )


class _Dispatcher:
    calls: list[ExecutionDispatchRequest] = []

    def __init__(self, *_: object) -> None:
        pass

    def dispatch(
        self,
        worker: ExecutionWorker,
        request: ExecutionDispatchRequest,
        *,
        cwd: Path,
    ) -> ExecutionDispatchResult:
        self.calls.append(request)
        artifact = request.artifact_reference
        if request.action in {
            LifecycleDispatchAction.BUILD,
            LifecycleDispatchAction.TEST,
        }:
            artifact = ContentReference(
                "artifact-export",
                "cas:sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                canonical_identity({"artifact": "demo"}),
            )
        return ExecutionDispatchResult(
            request.identity,
            worker.identity,
            "synthetic-task",
            DispatchResultStatus.PASSED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(canonical_identity({"tool": "python"}),),
            ),
            0,
            artifact,
            canonical_identity({"evidence": request.action.value}),
            (
                "remote-app-output\n"
                if request.action is LifecycleDispatchAction.RUN
                else ""
            ),
            "",
        )
