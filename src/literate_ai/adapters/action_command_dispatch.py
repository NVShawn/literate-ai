"""Owned command-worker actuation for serializable lifecycle actions."""

from __future__ import annotations

import base64
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
    decode_attested_action_response,
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
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)


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
        record_failure: Callable[[ContentIdentity, bytes], None] | None = None,
        on_unknown_ownership: Callable[[LifecycleActionDispatchRequest], None]
        | None = None,
        revalidate_worker: Callable[[LifecycleActionWorker], None],
        environment: Mapping[str, str] | None = None,
        attested_boundary: Callable[[LifecycleActionWorker], ContentIdentity]
        | None = None,
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
        self.record_failure = record_failure
        self.on_unknown_ownership = on_unknown_ownership
        self.revalidate_worker = revalidate_worker
        # When set, boundaries skip the separate capability probe: the receiver
        # measures the admitted capability in process and attests it. A response
        # without that attestation falls back to full revalidation.
        self.attested_boundary = attested_boundary
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
        return self._exchange(request, mode=None)[0]

    def dispatch_record(
        self, request: LifecycleActionDispatchRequest
    ) -> tuple[LifecycleActionDispatchOutcome, bytes | None]:
        """Actuate the exact request and expose its verified result record.

        Result recording and all transport/worker/deadline checks remain owned
        by the same exchange as ordinary dispatch. This permits typed artifact
        custody adapters without reaching into the transport's private exchange.
        """
        return self._exchange(request, mode=None)

    def validate_request(self, request: LifecycleActionDispatchRequest) -> None:
        """Revalidate exact routing/current worker before local artifact admission."""
        self._selection(request)
        self.deadline.remaining()
        self.revalidate_worker(request.worker)
        with self._lock:
            if request.identity in self._cancelled:
                raise ActionWireError(
                    "action_dispatch.cancelled", "action was cancelled"
                )

    def describe(
        self, request: LifecycleActionDispatchRequest, *, mode: str
    ) -> LifecycleActionDispatchOutcome | bytes:
        """Run a read-only receiver mode; its result is returned, never recorded."""
        if mode != "--describe-finalize-grant":
            raise ActionWireError(
                "action_transport.mode_invalid", "unsupported receiver mode"
            )
        outcome, result_record = self._exchange(request, mode=mode)
        return outcome if result_record is None else result_record

    def _exchange(
        self, request: LifecycleActionDispatchRequest, *, mode: str | None
    ) -> tuple[LifecycleActionDispatchOutcome, bytes | None]:
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
        actuated = False
        try:
            capability = None
            if self.attested_boundary is None:
                self.revalidate_worker(request.worker)
            else:
                capability = self.attested_boundary(request.worker)
                if not isinstance(capability, ContentIdentity):
                    raise ActionWireError(
                        "action_dispatch.admission_mismatch",
                        "attested boundary returned no admitted capability",
                    )
            content = encode_action_request(
                request,
                self.deadline,
                self.input_records(request),
                capability=capability,
            )
            environment = self._environment(worker)
            with tempfile.TemporaryDirectory(prefix="litai-action-") as directory:
                argv = action_receiver_command(worker, self.deadline, mode=mode)
                stdin = content
                if "{request_file}" in argv:
                    path = Path(directory) / "request.json"
                    path.write_bytes(content)
                    path.chmod(0o600)
                    argv = tuple(
                        str(path) if item == "{request_file}" else item for item in argv
                    )
                    stdin = None
                actuated = True
                output = self._run(argv, environment, stdin, event)
            outcome, result_record, attested = decode_attested_action_response(
                output, request
            )
            self.deadline.remaining()
            if attested is None:
                self.revalidate_worker(request.worker)
            elif attested != capability or (
                self.attested_boundary(request.worker) != capability
            ):
                raise ActionWireError(
                    "action_admission.runtime_changed",
                    "worker runtime or supported capabilities changed",
                )
            if (
                mode is None
                and outcome.failure_code is not None
                and self.on_unknown_ownership is not None
            ):
                self.on_unknown_ownership(request)
            with self._lock:
                if event.is_set():
                    raise ActionWireError(
                        "action_dispatch.cancelled", "action was cancelled"
                    )
                self.deadline.remaining()
                if mode is None and result_record is not None:
                    assert outcome.result_identity is not None
                    self.record_result(outcome.result_identity, result_record)
            return outcome, result_record
        except BaseException as exc:
            if (
                mode is None
                and actuated
                and not getattr(exc, "process_not_started", False)
            ):
                exc.unknown_ownership_request = request
                if self.on_unknown_ownership is not None:
                    self.on_unknown_ownership(request)
            if mode is None and self.record_failure is not None:
                diagnostic = {
                    "schema": "literate-ai/action-command-failure@1",
                    "ownership_scope": "transport-process-only",
                    "request_identity": request.identity.uri,
                    "worker_identity": request.worker.worker_identity.uri,
                    "failure_code": getattr(exc, "code", None) or type(exc).__name__,
                    "exit_status": getattr(exc, "process_exit_status", None),
                    "elapsed_ns": None
                    if not hasattr(exc, "process_elapsed_seconds")
                    else int(exc.process_elapsed_seconds * 1_000_000_000),
                    "stdout_base64": None
                    if not hasattr(exc, "process_stdout")
                    else base64.b64encode(exc.process_stdout).decode("ascii"),
                    "stderr_base64": None
                    if not hasattr(exc, "process_stderr")
                    else base64.b64encode(exc.process_stderr).decode("ascii"),
                }
                try:
                    self.record_failure(
                        canonical_identity(diagnostic), canonical_json_bytes(diagnostic)
                    )
                except BaseException as retention_error:
                    if hasattr(exc, "unknown_ownership_request"):
                        retention_error.unknown_ownership_request = request
                    raise
                exc.failure_evidence_identity = canonical_identity(diagnostic)
                # A command may itself be a remote dispatcher. Its local exit
                # is diagnostic transport evidence, never native-job termination.
            raise
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
            error = ActionWireError(
                code, "command action process failed its execution bounds"
            )
            error.process_not_started = exc.code == "action_dispatch_launch_failed"
            raise error from exc
        if completed.returncode != 0:
            error = ActionWireError(
                "action_dispatch.failed",
                f"worker exited with status {completed.returncode}",
            )
            error.process_exit_status = completed.returncode
            error.process_elapsed_seconds = completed.elapsed_seconds
            error.process_stdout = completed.stdout
            error.process_stderr = completed.stderr
            raise error
        return completed.stdout
