"""Small, scheduler-free adapters for exact execution workers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from literate_ai.adapters._processes import (
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.contracts import (
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    LifecycleDispatchAction,
    canonical_json_bytes,
)
from literate_ai.diagnostics import inherited_verbose_environment, trace_subprocess

MAX_CATALOG_BYTES = 1024 * 1024
MAX_DISPATCH_OUTPUT_BYTES = 1024 * 1024
MAX_DISPATCH_DIAGNOSTIC_BYTES = 64 * 1024
_REQUEST_FILE_PLACEHOLDER = "{request_file}"


class ExecutionDispatchAdapterError(RuntimeError):
    """A private worker or external dispatcher violated the execution boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class DispatcherProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class DispatcherProcessRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stdin: bytes | None,
        timeout_seconds: int,
    ) -> DispatcherProcessResult: ...


class LocalExecutionRequestHandler(Protocol):
    """Existing lifecycle machinery adapted to the execution-worker boundary."""

    def execute(
        self, request: ExecutionDispatchRequest, *, cwd: Path
    ) -> ExecutionDispatchResult: ...


class SshExecutionRequestHandler(Protocol):
    """Bounded source-transfer/SSH lane adapted without owning worker selection."""

    def execute(
        self,
        worker: ExecutionWorker,
        request: ExecutionDispatchRequest,
        *,
        cwd: Path,
    ) -> ExecutionDispatchResult: ...


class BoundedDispatcherProcessRunner:
    """Run one direct argv with file-backed, size-bounded process output."""

    # Bound on how long we wait for the process tree to actually die after a
    # terminate signal, before escalating to `kill()`/giving up. Kept short:
    # this is cleanup after the caller's own `timeout_seconds` has already
    # expired, not additional budget for the dispatched command.
    _TERMINATE_GRACE_SECONDS = 5

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stdin: bytes | None,
        timeout_seconds: int,
    ) -> DispatcherProcessResult:
        child_environment = inherited_verbose_environment(environment)
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            ownership = create_process_tree_ownership()
            try:
                trace_subprocess(argv, cwd=cwd, environment=child_environment)
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=child_environment,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    **ownership.popen_options,
                )
            except OSError as exc:
                ownership.release()
                raise ExecutionDispatchAdapterError(
                    "execution.dispatcher_unavailable",
                    f"dispatcher executable is unavailable: {argv[0]}",
                ) from exc
            # Bind the just-spawned root process to the tree-kill job (a
            # no-op on POSIX, where `start_new_session` already made it a
            # process-group leader) immediately, so late-spawned
            # grandchildren are covered too.
            ownership.bind(process.pid)
            try:
                try:
                    process.communicate(input=stdin, timeout=timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    terminate_process_tree(
                        process, environment=child_environment, ownership=ownership
                    )
                    self._await_termination_or_give_up(process)
                    raise ExecutionDispatchAdapterError(
                        "execution.dispatcher_timed_out",
                        f"dispatcher exceeded {timeout_seconds} seconds",
                    ) from exc
            finally:
                ownership.release()
            stdout.flush()
            stderr.flush()
            if stdout.tell() > MAX_DISPATCH_OUTPUT_BYTES:
                raise ExecutionDispatchAdapterError(
                    "execution.dispatcher_output_too_large",
                    "dispatcher result exceeds the one MiB limit",
                )
            if stderr.tell() > MAX_DISPATCH_DIAGNOSTIC_BYTES:
                raise ExecutionDispatchAdapterError(
                    "execution.dispatcher_diagnostic_too_large",
                    "dispatcher diagnostic exceeds the 64 KiB limit",
                )
            stdout.seek(0)
            stderr.seek(0)
            result = DispatcherProcessResult(
                process.returncode,
                stdout.read(MAX_DISPATCH_OUTPUT_BYTES + 1),
                stderr.read(MAX_DISPATCH_DIAGNOSTIC_BYTES + 1),
            )
            trace_subprocess(
                argv,
                cwd=cwd,
                environment=child_environment,
                status=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
            return result

    def _await_termination_or_give_up(self, process: subprocess.Popen[bytes]) -> None:
        """Bound the post-terminate wait; never block indefinitely.

        `terminate_process_tree` is best-effort: on a host where tree kill
        is incomplete (a surviving child/grandchild), the process can still
        be alive afterward. A bare `process.communicate()` here would then
        hang forever holding the pipe open. Give the tree a short grace
        period to actually exit, escalate to `kill()` once, and then give
        up bounded rather than block the caller indefinitely.
        """

        try:
            process.communicate(timeout=self._TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        with suppress(OSError):
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=self._TERMINATE_GRACE_SECONDS)


def load_execution_worker_catalog(path: str | Path) -> ExecutionWorkerCatalog:
    """Load one bounded, non-symlink private worker catalog."""

    configured = Path(path).expanduser()
    try:
        if configured.is_symlink() or not configured.is_file():
            raise ExecutionDispatchAdapterError(
                "execution.worker_catalog_unavailable",
                "worker catalog must be a regular non-symlink file",
            )
        if configured.stat().st_size > MAX_CATALOG_BYTES:
            raise ExecutionDispatchAdapterError(
                "execution.worker_catalog_too_large",
                "worker catalog exceeds the one MiB limit",
            )
        raw = configured.read_bytes()
    except ExecutionDispatchAdapterError:
        raise
    except OSError as exc:
        raise ExecutionDispatchAdapterError(
            "execution.worker_catalog_unavailable", "worker catalog cannot be read"
        ) from exc
    if len(raw) > MAX_CATALOG_BYTES:
        raise ExecutionDispatchAdapterError(
            "execution.worker_catalog_too_large",
            "worker catalog exceeds the one MiB limit",
        )
    try:
        value = json.loads(raw.decode("utf-8"))
        return ExecutionWorkerCatalog.from_dict(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        from literate_ai.contracts import ContractValidationError
        from literate_ai.diagnostics import redact_secrets

        detail = (
            redact_secrets(f"{exc.path}: {exc.message}")[:400]
            if isinstance(exc, ContractValidationError)
            else "expected a UTF-8 JSON execution-worker-catalog document"
        )
        raise ExecutionDispatchAdapterError(
            "execution.worker_catalog_invalid",
            f"Invalid worker catalog: {detail}",
        ) from exc


def _dispatch_context(
    worker: ExecutionWorker,
    request: ExecutionDispatchRequest,
    cwd: Path,
) -> Path:
    if request.worker_identity != worker.identity:
        raise ExecutionDispatchAdapterError(
            "execution.worker_identity_mismatch",
            "request does not bind the selected worker",
        )
    if request.target_profile != worker.target_profile:
        raise ExecutionDispatchAdapterError(
            "execution.worker_target_profile_mismatch",
            "request target profile does not equal the selected worker target profile",
        )
    if request.requirements != worker.requirements:
        raise ExecutionDispatchAdapterError(
            "execution.worker_requirements_mismatch",
            "request requirements do not equal the selected worker requirements",
        )
    try:
        working_directory = Path(cwd).resolve(strict=True)
    except OSError as exc:
        raise ExecutionDispatchAdapterError(
            "execution.dispatch_working_directory_invalid",
            "dispatcher working directory must be a directory",
        ) from exc
    if not working_directory.is_dir() or working_directory.is_symlink():
        raise ExecutionDispatchAdapterError(
            "execution.dispatch_working_directory_invalid",
            "dispatcher working directory must be a non-symlink directory",
        )
    return working_directory


def _validated_result(
    worker: ExecutionWorker,
    request: ExecutionDispatchRequest,
    result: ExecutionDispatchResult,
) -> ExecutionDispatchResult:
    if not isinstance(result, ExecutionDispatchResult):
        raise ExecutionDispatchAdapterError(
            "execution.dispatcher_result_invalid",
            "execution handler did not return a typed dispatch result",
        )
    if result.request_identity != request.identity:
        raise ExecutionDispatchAdapterError(
            "execution.dispatcher_request_mismatch",
            "dispatcher result binds a different request",
        )
    if result.worker_identity != worker.identity:
        raise ExecutionDispatchAdapterError(
            "execution.dispatcher_worker_mismatch",
            "dispatcher result binds a different worker",
        )
    if not result.observed_environment.satisfies(request.requirements):
        raise ExecutionDispatchAdapterError(
            "execution.dispatcher_requirements_unsatisfied",
            "observed environment contradicts requested hardware requirements",
        )
    if (
        result.status is DispatchResultStatus.PASSED
        and not result.observed_environment.toolchain_identities
    ):
        raise ExecutionDispatchAdapterError(
            "execution.dispatcher_toolchain_observation_missing",
            "successful dispatches must report exact observed toolchain identities",
        )
    if result.status is DispatchResultStatus.PASSED:
        if result.artifact_reference is None:
            raise ExecutionDispatchAdapterError(
                "execution.dispatcher_artifact_missing",
                "successful lifecycle dispatches must identify their exact artifact",
            )
        if (
            request.action is LifecycleDispatchAction.RUN
            and result.artifact_reference != request.artifact_reference
        ):
            raise ExecutionDispatchAdapterError(
                "execution.dispatcher_artifact_mismatch",
                "successful run dispatch must preserve its exact input artifact",
            )
    elif (
        result.status is not DispatchResultStatus.PASSED
        and result.artifact_reference is not None
    ):
        raise ExecutionDispatchAdapterError(
            "execution.dispatcher_artifact_untrusted",
            "unsuccessful dispatches cannot publish an accepted artifact export",
        )
    return result


class LocalExecutionDispatcher:
    """Delegate one local worker request to the already-authorized lifecycle."""

    def __init__(self, handler: LocalExecutionRequestHandler) -> None:
        self.handler = handler

    def dispatch(
        self,
        worker: ExecutionWorker,
        request: ExecutionDispatchRequest,
        *,
        cwd: Path,
    ) -> ExecutionDispatchResult:
        if worker.kind is not ExecutionWorkerKind.LOCAL:
            raise ExecutionDispatchAdapterError(
                "execution.worker_kind_invalid",
                "local dispatcher requires a local worker",
            )
        working_directory = _dispatch_context(worker, request, cwd)
        return _validated_result(
            worker,
            request,
            self.handler.execute(request, cwd=working_directory),
        )


class SshExecutionDispatcher:
    """Delegate an exact SSH worker to the existing bounded SSH lifecycle lane."""

    def __init__(self, handler: SshExecutionRequestHandler) -> None:
        self.handler = handler

    def dispatch(
        self,
        worker: ExecutionWorker,
        request: ExecutionDispatchRequest,
        *,
        cwd: Path,
    ) -> ExecutionDispatchResult:
        if worker.kind is not ExecutionWorkerKind.SSH:
            raise ExecutionDispatchAdapterError(
                "execution.worker_kind_invalid",
                "SSH dispatcher requires an SSH worker",
            )
        working_directory = _dispatch_context(worker, request, cwd)
        return _validated_result(
            worker,
            request,
            self.handler.execute(worker, request, cwd=working_directory),
        )


class CommandExecutionDispatcher:
    """Submit a canonical request to one exact external command worker."""

    def __init__(
        self,
        runner: DispatcherProcessRunner | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        temporary_root: Path | None = None,
    ) -> None:
        self.runner = runner or BoundedDispatcherProcessRunner()
        self.environment = dict(os.environ if environment is None else environment)
        self.temporary_root = temporary_root

    def dispatch(
        self,
        worker: ExecutionWorker,
        request: ExecutionDispatchRequest,
        *,
        cwd: Path,
    ) -> ExecutionDispatchResult:
        if worker.kind is not ExecutionWorkerKind.COMMAND:
            raise ExecutionDispatchAdapterError(
                "execution.worker_kind_invalid",
                "command dispatcher requires a command worker",
            )
        working_directory = _dispatch_context(worker, request, cwd)
        dispatch_environment = self._environment(worker)
        request_bytes = canonical_json_bytes(request.to_dict())
        if _REQUEST_FILE_PLACEHOLDER not in worker.command:
            return self._run(
                worker,
                request,
                worker.command,
                working_directory,
                dispatch_environment,
                request_bytes,
            )
        temporary_root = self.temporary_root
        if temporary_root is not None:
            temporary_root = temporary_root.resolve(strict=True)
            if not temporary_root.is_dir() or temporary_root.is_symlink():
                raise ExecutionDispatchAdapterError(
                    "execution.dispatch_temporary_root_invalid",
                    "temporary root must be a non-symlink directory",
                )
        with tempfile.TemporaryDirectory(
            prefix="litai-dispatch-", dir=temporary_root
        ) as directory:
            request_file = Path(directory) / "request.json"
            request_file.write_bytes(request_bytes)
            argv = tuple(
                str(request_file) if item == _REQUEST_FILE_PLACEHOLDER else item
                for item in worker.command
            )
            return self._run(
                worker,
                request,
                argv,
                working_directory,
                dispatch_environment,
                None,
            )

    def _environment(self, worker: ExecutionWorker) -> dict[str, str]:
        selected: dict[str, str] = {}
        for name in ("PATH", "SystemRoot", "ComSpec", "PATHEXT", "TMP", "TEMP"):
            value = self.environment.get(name)
            if value:
                selected[name] = value
        selected["LITAI_DISPATCH_PROTOCOL"] = ExecutionDispatchRequest.SCHEMA
        for binding in worker.environment:
            value = self.environment.get(binding.source_variable)
            if not value:
                if binding.required:
                    raise ExecutionDispatchAdapterError(
                        "execution.dispatcher_authentication_required",
                        "required dispatcher credential or environment binding "
                        f"{binding.source_variable!r} is unavailable",
                    )
                continue
            selected[binding.name] = value
        return selected

    def _run(
        self,
        worker: ExecutionWorker,
        request: ExecutionDispatchRequest,
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        stdin: bytes | None,
    ) -> ExecutionDispatchResult:
        completed = self.runner.run(
            argv,
            cwd=cwd,
            environment=environment,
            stdin=stdin,
            timeout_seconds=request.timeout_seconds,
        )
        if completed.returncode != 0:
            raise ExecutionDispatchAdapterError(
                "execution.dispatcher_failed",
                f"dispatcher exited with status {completed.returncode}",
            )
        if len(completed.stdout) > MAX_DISPATCH_OUTPUT_BYTES:
            raise ExecutionDispatchAdapterError(
                "execution.dispatcher_output_too_large",
                "dispatcher result exceeds the one MiB limit",
            )
        try:
            value = json.loads(completed.stdout.decode("utf-8"))
            result = ExecutionDispatchResult.from_dict(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ExecutionDispatchAdapterError(
                "execution.dispatcher_result_invalid",
                "dispatcher did not return one valid dispatch-result document",
            ) from exc
        return _validated_result(worker, request, result)


__all__ = [
    "BoundedDispatcherProcessRunner",
    "CommandExecutionDispatcher",
    "DispatcherProcessResult",
    "DispatcherProcessRunner",
    "ExecutionDispatchAdapterError",
    "LocalExecutionDispatcher",
    "LocalExecutionRequestHandler",
    "MAX_CATALOG_BYTES",
    "MAX_DISPATCH_DIAGNOSTIC_BYTES",
    "MAX_DISPATCH_OUTPUT_BYTES",
    "SshExecutionDispatcher",
    "SshExecutionRequestHandler",
    "load_execution_worker_catalog",
]
