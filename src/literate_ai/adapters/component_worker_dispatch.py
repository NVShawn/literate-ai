"""Kind-routed adapters for complete Component-node lifecycle execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, Protocol, TypeVar

from literate_ai.application.component_workers import (
    ComponentNodeCancellation,
    ComponentNodeDispatchOutcome,
    ComponentNodeDispatchRequest,
    ComponentWorkerError,
    component_node_payload_result,
)
from literate_ai.contracts.component_workers import ComponentArtifactImportReceipt
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)

NodeResultT = TypeVar("NodeResultT")


class ComponentNodeTransportHandler(Protocol, Generic[NodeResultT]):
    """A private transport implementation for one exact selected worker."""

    def execute(
        self,
        worker: ExecutionWorker,
        request: ComponentNodeDispatchRequest,
        execute: Callable[[], NodeResultT],
        *,
        cancellation: ComponentNodeCancellation | None,
    ) -> ComponentNodeDispatchOutcome[NodeResultT]: ...

    def cancel(
        self, worker: ExecutionWorker, request: ComponentNodeDispatchRequest
    ) -> None: ...


class LocalComponentNodeHandler(Generic[NodeResultT]):
    """Run an already-authorized Standard node in the controller process."""

    def execute(
        self,
        worker: ExecutionWorker,
        request: ComponentNodeDispatchRequest,
        execute: Callable[[], NodeResultT],
        *,
        cancellation: ComponentNodeCancellation | None,
    ) -> ComponentNodeDispatchOutcome[NodeResultT]:
        if cancellation is not None and cancellation.is_set():
            raise ComponentWorkerError("component node dispatch was cancelled")
        result = execute()
        result_identity = getattr(
            component_node_payload_result(result), "identity", None
        )
        if result_identity is None:
            raise ComponentWorkerError("component node result has no exact identity")
        return ComponentNodeDispatchOutcome(
            request.identity,
            worker.identity,
            result_identity,
            ComponentArtifactImportReceipt(
                request.handoff_identity,
                worker.identity,
                tuple(
                    sorted(
                        {
                            *request.provider_artifact_identities,
                            *request.package_artifact_identities,
                        },
                        key=lambda item: item.uri,
                    )
                ),
            ),
            result,
        )

    def cancel(
        self, worker: ExecutionWorker, request: ComponentNodeDispatchRequest
    ) -> None:
        # The in-process lifecycle has no private process tree to terminate. Its
        # adapters observe the same cancellation signal at their own boundaries.
        return None


class RoutedComponentNodeDispatcher(Generic[NodeResultT]):
    """Select local, command, or SSH transport without creating another scheduler."""

    def __init__(
        self,
        catalog: ExecutionWorkerCatalog,
        *,
        local: ComponentNodeTransportHandler[NodeResultT] | None = None,
        command: ComponentNodeTransportHandler[NodeResultT] | None = None,
        ssh: ComponentNodeTransportHandler[NodeResultT] | None = None,
    ) -> None:
        self.catalog = catalog
        self.handlers = {
            ExecutionWorkerKind.LOCAL: local or LocalComponentNodeHandler(),
            ExecutionWorkerKind.COMMAND: command,
            ExecutionWorkerKind.SSH: ssh,
        }

    def _selection(
        self, request: ComponentNodeDispatchRequest
    ) -> tuple[ExecutionWorker, ComponentNodeTransportHandler[NodeResultT]]:
        worker = self.catalog.worker(request.assignment.worker_id)
        if (
            worker.identity != request.assignment.worker_identity
            or worker.target_profile != request.assignment.target_profile
        ):
            raise ComponentWorkerError("component node worker selection changed")
        handler = self.handlers[worker.kind]
        if handler is None:
            raise ComponentWorkerError(
                f"no {worker.kind.value} Component node transport is configured"
            )
        return worker, handler

    def dispatch(
        self,
        request: ComponentNodeDispatchRequest,
        execute: Callable[[], NodeResultT],
        *,
        cancellation: ComponentNodeCancellation | None,
    ) -> ComponentNodeDispatchOutcome[NodeResultT]:
        worker, handler = self._selection(request)
        outcome = handler.execute(worker, request, execute, cancellation=cancellation)
        if not isinstance(outcome, ComponentNodeDispatchOutcome):
            raise ComponentWorkerError("component node transport returned no outcome")
        return outcome

    def cancel(self, request: ComponentNodeDispatchRequest) -> None:
        worker, handler = self._selection(request)
        handler.cancel(worker, request)
