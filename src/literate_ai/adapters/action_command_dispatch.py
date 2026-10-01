"""Owned command-worker actuation for serializable lifecycle actions."""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import (
    ACTION_WIRE_PROTOCOL,
    MAX_ACTION_WIRE_BYTES,
    ActionDispatchDeadline,
    ActionWireError,
    decode_action_response,
    encode_action_request,
)
from literate_ai.adapters.action_transport import (
    action_receiver_command,
    supports_action_transport,
)
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import BuildError
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchOutcome,
    LifecycleActionDispatchRequest,
    LifecycleActionWorker,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.contracts.identity import ContentIdentity


def command_worker_environment(
    worker: ExecutionWorker,
    bindings: Mapping[str, str],
    *,
    protocol: str = ACTION_WIRE_PROTOCOL,
) -> dict[str, str]:
    """Resolve declared private bindings without forwarding the ambient environment."""
    windows = os.name == "nt"
    if windows:
        bindings = {name.upper(): value for name, value in bindings.items()}

    def lookup(name: str) -> str:
        return bindings.get(name.upper() if windows else name, "")

    environment = {
        key: lookup(key)
        for key in ("PATH", "SystemRoot", "ComSpec", "PATHEXT", "TMP", "TEMP")
        if lookup(key)
    }
    if worker.kind is ExecutionWorkerKind.SSH and lookup("SSH_AUTH_SOCK"):
        environment["SSH_AUTH_SOCK"] = lookup("SSH_AUTH_SOCK")
    for binding in worker.environment:
        value = lookup(binding.source_variable)
        if value:
            environment[binding.name] = value
        elif binding.required:
            raise ActionWireError(
                "action_dispatch.environment_missing",
                "required worker environment binding is unavailable",
            )
    environment["LITAI_DISPATCH_PROTOCOL"] = protocol
    return environment


class CommandLifecycleActionDispatcher:
    """Execute bytes on the selected command worker, never a controller callback.

    Record providers and admission checks are controller custody operations. The
    worker command owns phase execution and must implement ACTION_WIRE_PROTOCOL.
    This adapter does not make a legacy whole-lifecycle command phase-capable.
    """

    def __init__(
        self,
        catalog: ExecutionWorkerCatalog,
        workers: Sequence[LifecycleActionWorker],
        deadline: ActionDispatchDeadline,
        *,
        cwd: Path,
        input_records: Callable[
            [LifecycleActionDispatchRequest], Mapping[ContentIdentity, bytes]
        ],
        record_result: Callable[[ContentIdentity, bytes], None],
        revalidate_worker: Callable[[LifecycleActionWorker], None],
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(catalog, ExecutionWorkerCatalog) or not isinstance(
            deadline, ActionDispatchDeadline
        ):
            raise TypeError("action dispatch requires typed catalog and deadline")
        if not workers or any(
            not isinstance(item, LifecycleActionWorker) for item in workers
        ):
            raise TypeError("action dispatch requires admitted workers")
        self.workers = {item.worker_id: item for item in workers}
        if len(self.workers) != len(workers) or any(
            item.catalog_identity != catalog.identity for item in workers
        ):
            raise ActionWireError(
                "action_dispatch.admission_mismatch",
                "worker admission differs from the catalog",
            )
        path = Path(cwd)
        if path.is_symlink() or not path.is_dir():
            raise ActionWireError(
                "action_dispatch.cwd_invalid",
                "action workspace is not a real directory",
            )
        self.cwd = path.resolve(strict=True)
        self.catalog = catalog
        self.deadline = deadline
        self.input_records = input_records
        self.record_result = record_result
        self.revalidate_worker = revalidate_worker
        self.environment = dict(os.environ if environment is None else environment)
        self._lock = threading.Lock()
        self._active: dict[ContentIdentity, threading.Event] = {}
        self._cancelled: set[ContentIdentity] = set()

    def _selection(self, request: LifecycleActionDispatchRequest) -> ExecutionWorker:
        if self.workers.get(request.worker.worker_id) != request.worker:
            raise ActionWireError(
                "action_dispatch.admission_mismatch",
                "request differs from admitted worker",
            )
        worker = self.catalog.worker(request.worker.worker_id)
        if (
            worker.identity != request.worker.worker_identity
            or (
                worker.kind is not ExecutionWorkerKind.COMMAND
                and not supports_action_transport(worker)
            )
            or request.worker.slots > worker.slots
        ):
            raise ActionWireError(
                "action_dispatch.worker_mismatch",
                "request does not select an exact command worker",
            )
        if request.deadline_identity != self.deadline.identity:
            raise ActionWireError(
                "action_wire.deadline_mismatch",
                "request differs from admitted deadline",
            )
        return worker

    def _environment(self, worker: ExecutionWorker) -> dict[str, str]:
        return command_worker_environment(worker, self.environment)

    def cancel(self, request: LifecycleActionDispatchRequest) -> None:
        self._selection(request)
        with self._lock:
            self._cancelled.add(request.identity)
            event = self._active.get(request.identity)
            if event is not None:
                event.set()

    def dispatch(
        self, request: LifecycleActionDispatchRequest
    ) -> LifecycleActionDispatchOutcome:
        worker = self._selection(request)
        self.deadline.remaining()
        event = threading.Event()
        with self._lock:
            if request.identity in self._cancelled:
                raise ActionWireError(
                    "action_dispatch.cancelled", "action was cancelled"
                )
            if request.identity in self._active:
                raise ActionWireError(
                    "action_dispatch.duplicate", "action is already running"
                )
            self._active[request.identity] = event
        try:
            self.revalidate_worker(request.worker)
            content = encode_action_request(
                request, self.deadline, self.input_records(request)
            )
            environment = self._environment(worker)
            with tempfile.TemporaryDirectory(prefix="litai-action-") as directory:
                argv = action_receiver_command(worker, self.deadline)
                stdin = content
                if "{request_file}" in argv:
                    path = Path(directory) / "request.json"
                    path.write_bytes(content)
                    path.chmod(0o600)
                    argv = tuple(
                        str(path) if item == "{request_file}" else item for item in argv
                    )
                    stdin = None
                output = self._run(argv, environment, stdin, event)
            outcome, result_record = decode_action_response(output, request)
            self.deadline.remaining()
            self.revalidate_worker(request.worker)
            with self._lock:
                if event.is_set():
                    raise ActionWireError(
                        "action_dispatch.cancelled", "action was cancelled"
                    )
                self.deadline.remaining()
                if result_record is not None:
                    assert outcome.result_identity is not None
                    self.record_result(outcome.result_identity, result_record)
            return outcome
        finally:
            with self._lock:
                self._active.pop(request.identity, None)

    def _run(
        self,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
        stdin: bytes | None,
        event: threading.Event,
    ) -> bytes:
        def interrupt() -> None:
            if event.is_set():
                raise ActionWireError(
                    "action_dispatch.cancelled", "action was cancelled"
                )
            self.deadline.remaining()

        try:
            completed = run_bounded_process(
                argv,
                cwd=self.cwd,
                environment=environment,
                timeout_seconds=self.deadline.remaining(),
                stdout_limit_bytes=MAX_ACTION_WIRE_BYTES,
                stderr_limit_bytes=64 * 1024,
                error_prefix="action_dispatch",
                input_bytes=stdin,
                input_limit_bytes=MAX_ACTION_WIRE_BYTES,
                interrupt_guard=interrupt,
                terminate_descendants=True,
                poll_interval_seconds=0.1,
                trace=False,
            )
        except BuildError as exc:
            code = {
                "action_dispatch_timeout": "action_wire.expired",
                "action_dispatch_output_limit": "action_dispatch.output_oversized",
            }.get(exc.code, "action_dispatch.failed")
            raise ActionWireError(
                code, "command action process failed its execution bounds"
            ) from exc
        if completed.returncode != 0:
            raise ActionWireError(
                "action_dispatch.failed",
                f"worker exited with status {completed.returncode}",
            )
        return completed.stdout
