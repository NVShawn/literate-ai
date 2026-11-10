"""Select one exact remote route without synthesizing an SSH endpoint.

Protocol callers retain their bounded process and evidence owners. This adapter
supplies the same selected route to probes, transfers and receiver execution;
constructing an argv does not prove transport success or artifact custody.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts import ExecutionWorker, ExecutionWorkerKind
from literate_ai.contracts.execution_dispatch import MAX_REMOTE_EVIDENCE_BUNDLE_BYTES

from .builders._process import run_bounded_process
from .builders.python import BuildError
from .ssh_transport import (
    MAX_SSH_DIAGNOSTIC_BYTES,
    MAX_SSH_OUTPUT_BYTES,
    SSH_DIAGNOSTIC_TRUNCATION_MARKER,
    SshProcessResult,
    SshTransportError,
    scp_arguments,
    scp_download_arguments,
    ssh_arguments,
)


class BoundedRemoteProcessRunner:
    """Use the shared process owner for both SSH and command-addressed routes.

    Diagnostic tails stay in bounded memory while stdout overflows terminate the
    owned process tree. A successful connector cannot leave controller children
    holding the channel open. This does not prove that a disconnected remote
    mutation stopped; its receiver fence and evidence owner still decide that.
    """

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        interrupt_guard: Callable[[], None] | None = None,
        file_limit_bytes: int | None = None,
    ) -> SshProcessResult:
        _deadline(timeout_seconds)
        if file_limit_bytes is not None:
            if os.name != "posix":
                raise SshTransportError(
                    "execution.remote_staging_limit_unavailable",
                    "this controller lacks a qualified native file-size boundary",
                )
            if (
                type(file_limit_bytes) is not int
                or not 1 <= file_limit_bytes <= MAX_REMOTE_EVIDENCE_BUNDLE_BYTES
            ):
                raise SshTransportError(
                    "execution.remote_staging_limit_invalid",
                    "finite staging limit required",
                )
            # Apply the hard bound in a fresh trusted process, avoiding a Python
            # preexec_fn in a controller that may have concurrent worker threads.
            # The allowance is an upper bound, never permission to increase a
            # stricter inherited soft or hard ceiling (including a zero bound).
            limiter = (
                "import os,resource,sys;limit=int(sys.argv[1]);"
                "soft,hard=resource.getrlimit(resource.RLIMIT_FSIZE);"
                "soft=limit if soft==resource.RLIM_INFINITY else min(soft,limit);"
                "hard=limit if hard==resource.RLIM_INFINITY else min(hard,limit);"
                "resource.setrlimit(resource.RLIMIT_FSIZE,(soft,hard));"
                "os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"
            )
            argv = (
                sys.executable,
                "-I",
                "-S",
                "-c",
                limiter,
                str(file_limit_bytes),
                *argv,
            )
        try:
            working_directory = Path(cwd).resolve(strict=True)
            if not working_directory.is_dir():
                raise OSError("unavailable directory")
        except OSError as exc:
            raise SshTransportError(
                "execution.ssh_working_directory_invalid",
                "remote working directory is unavailable",
            ) from exc
        try:
            result = run_bounded_process(
                argv,
                cwd=working_directory,
                environment=os.environ,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=MAX_SSH_OUTPUT_BYTES,
                stderr_limit_bytes=MAX_SSH_DIAGNOSTIC_BYTES,
                stderr_tail_bytes=MAX_SSH_DIAGNOSTIC_BYTES,
                terminate_descendants=True,
                error_prefix="worker.remote",
                interrupt_guard=interrupt_guard,
            )
        except BuildError as exc:
            suffix = {
                "worker.remote_launch_failed": "unavailable",
                "worker.remote_timeout": "timed_out",
                "worker.remote_output_limit": "output_too_large",
            }.get(exc.code, "output_failed")
            raise SshTransportError(
                "execution.ssh_" + suffix,
                "selected remote transport could not complete its bounded operation",
            ) from exc
        diagnostic = result.stderr
        if result.stderr_truncated:
            retained = MAX_SSH_DIAGNOSTIC_BYTES - len(SSH_DIAGNOSTIC_TRUNCATION_MARKER)
            diagnostic = SSH_DIAGNOSTIC_TRUNCATION_MARKER + diagnostic[-retained:]
        return SshProcessResult(result.returncode, result.stdout, diagnostic)


def _substitute(route: tuple[str, ...], values: dict[str, str]) -> tuple[str, ...]:
    # The descriptor already validated complete placeholders and the executable.
    # There is deliberately no format(), shell, environment or substring expansion.
    return tuple(values.get(argument, argument) for argument in route)


def _remote_file(value: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise SshTransportError(
            "execution.remote_file_invalid", "remote file must be one literal path"
        )
    return value


def _deadline(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds < float("inf")
    ):
        raise SshTransportError(
            "execution.remote_timeout_invalid",
            "remote timeout must be finite and positive",
        )


def _signature(node):
    return (
        node.st_dev,
        node.st_ino,
        node.st_mode,
        node.st_nlink,
        node.st_size,
        node.st_mtime_ns,
        node.st_ctime_ns,
    )


def _byte_limit(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_REMOTE_EVIDENCE_BUNDLE_BYTES:
        raise SshTransportError(
            "execution.remote_transfer_limit_invalid",
            "finite transfer allowance required",
        )
    return value


@contextmanager
def _filesystem_boundary():
    try:
        yield
    except OSError as exc:
        raise SshTransportError(
            "execution.remote_file_unavailable",
            "remote transfer custody could not be established",
        ) from exc


@contextmanager
def _source_file(path: Path, maximum_bytes: int, guard=None):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        original = os.fstat(descriptor)
        if not stat.S_ISREG(original.st_mode) or original.st_size > maximum_bytes:
            raise SshTransportError(
                "execution.remote_source_invalid",
                "staged source exceeds its regular-file allowance",
            )

        def current():
            if guard is not None:
                guard()
            if _signature(os.fstat(descriptor)) != _signature(original) or _signature(
                path.lstat()
            ) != _signature(original):
                raise SshTransportError(
                    "execution.remote_source_changed",
                    "staged source changed during transfer",
                )

        def digest():
            current()
            os.lseek(descriptor, 0, os.SEEK_SET)
            result = hashlib.sha256()
            remaining = original.st_size
            while remaining:
                current()
                block = os.read(descriptor, min(1024 * 1024, remaining))
                if not block:
                    raise SshTransportError(
                        "execution.remote_source_changed", "staged source was truncated"
                    )
                result.update(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise SshTransportError(
                    "execution.remote_source_changed",
                    "staged source grew during transfer",
                )
            current()
            return result.hexdigest()

        before = digest()
        yield current, before, original.st_size
        if digest() != before:
            raise SshTransportError(
                "execution.remote_source_changed",
                "staged source bytes changed during transfer",
            )
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class RemoteWorkerTransport:
    worker: ExecutionWorker

    def __post_init__(self) -> None:
        if not isinstance(self.worker, ExecutionWorker) or not self.worker.is_remote:
            raise SshTransportError(
                "execution.remote_worker_invalid", "select an exact remote worker"
            )
        routes = self.worker.reachability
        if routes is not None:
            for operation in ("execute", "upload", "download"):
                if shutil.which(getattr(routes, operation)[0]) is None:
                    raise SshTransportError(
                        "execution.remote_client_unavailable",
                        f"the declared {operation} executable is unavailable; "
                        "all three routes are required for remote readiness",
                    )

    def execute_arguments(
        self, command: str, timeout_seconds: float, *, login_shell: bool = True
    ) -> tuple[str, ...]:
        worker = self.worker
        if worker.kind is ExecutionWorkerKind.SSH:
            return ssh_arguments(
                worker.endpoint,
                command,
                timeout_seconds,
                transport=worker.transport,
                login_shell=login_shell,
            )
        _deadline(timeout_seconds)
        if not isinstance(command, str) or not command or "\x00" in command:
            raise SshTransportError(
                "execution.remote_command_invalid",
                "receiver command must be nonempty and NUL-free",
            )
        # The operator supplies the remote shell in the route when one is needed.
        # Applying the SSH login-shell convention here would change that authority.
        return _substitute(worker.reachability.execute, {"{remote_command}": command})

    def upload_arguments(
        self, source: Path, destination: str, timeout_seconds: float
    ) -> tuple[str, ...]:
        worker = self.worker
        if worker.kind is ExecutionWorkerKind.SSH:
            return scp_arguments(
                source,
                worker.endpoint,
                destination,
                timeout_seconds,
                transport=worker.transport,
            )
        _deadline(timeout_seconds)
        selected = Path(source)
        if selected.is_symlink() or not selected.is_file():
            raise SshTransportError(
                "execution.remote_source_invalid",
                "upload source must be a regular non-symlink file",
            )
        return _substitute(
            worker.reachability.upload,
            {"{local_file}": str(selected), "{remote_file}": _remote_file(destination)},
        )

    def download_arguments(
        self, source: str, destination: Path, timeout_seconds: float
    ) -> tuple[str, ...]:
        worker = self.worker
        if worker.kind is ExecutionWorkerKind.SSH:
            return scp_download_arguments(
                worker.endpoint,
                source,
                destination,
                timeout_seconds,
                transport=worker.transport,
            )
        _deadline(timeout_seconds)
        source = _remote_file(source)
        if not source.endswith("/remote-evidence.tar.gz"):
            raise SshTransportError(
                "execution.remote_source_invalid",
                "download source must be the transport-defined evidence bundle",
            )
        selected = Path(destination)
        if selected.exists() or selected.is_symlink() or not selected.parent.is_dir():
            raise SshTransportError(
                "execution.remote_destination_invalid",
                "evidence destination must be a new file below an existing directory",
            )
        return _substitute(
            worker.reachability.download,
            {"{local_file}": str(selected), "{remote_file}": source},
        )

    def upload(
        self,
        source: Path,
        destination: str,
        *,
        cwd: Path,
        timeout_seconds: float,
        runner=None,
        maximum_bytes: int = MAX_REMOTE_EVIDENCE_BUNDLE_BYTES,
    ) -> SshProcessResult:
        maximum_bytes = _byte_limit(maximum_bytes)
        _deadline(timeout_seconds)
        process_runner = runner or BoundedRemoteProcessRunner()
        if self.worker.reachability is None:
            # Preserve v1 transfer semantics. Its existing source/evidence owners
            # perform custody checks at their original protocol boundaries.
            return process_runner.run(
                self.upload_arguments(source, destination, timeout_seconds),
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )
        expires = time.monotonic() + timeout_seconds
        selected = Path(source)
        if not selected.is_absolute():
            selected = Path(cwd) / selected

        def alive():
            _deadline(expires - time.monotonic())

        with (
            _filesystem_boundary(),
            _source_file(selected, maximum_bytes, alive) as (current, _digest, _size),
        ):
            arguments = self.upload_arguments(
                selected, destination, expires - time.monotonic()
            )
            options = (
                {} if self.worker.reachability is None else {"interrupt_guard": current}
            )
            result = process_runner.run(
                arguments,
                cwd=cwd,
                timeout_seconds=expires - time.monotonic(),
                **options,
            )
        return result

    def download(
        self,
        source: str,
        destination: Path,
        *,
        cwd: Path,
        timeout_seconds: float,
        runner=None,
        maximum_bytes: int = MAX_REMOTE_EVIDENCE_BUNDLE_BYTES,
        expected_digest: str | None = None,
    ) -> SshProcessResult:
        maximum_bytes = _byte_limit(maximum_bytes)
        _deadline(timeout_seconds)
        process_runner = runner or BoundedRemoteProcessRunner()
        if self.worker.reachability is None:
            return process_runner.run(
                self.download_arguments(source, destination, timeout_seconds),
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )
        expires = time.monotonic() + timeout_seconds
        selected = Path(destination)
        if not selected.is_absolute():
            selected = Path(cwd) / selected
        if selected.exists() or selected.is_symlink():
            raise SshTransportError(
                "execution.remote_destination_invalid",
                "evidence destination already exists",
            )
        with (
            _filesystem_boundary(),
            tempfile.TemporaryDirectory(
                prefix="litai-route-download-", dir=selected.parent
            ) as raw,
        ):
            staged = Path(raw) / "remote-evidence.tar.gz"
            original = [None]

            def current():
                _deadline(expires - time.monotonic())
                try:
                    node = staged.lstat()
                except FileNotFoundError:
                    return
                if (
                    not stat.S_ISREG(node.st_mode)
                    or node.st_nlink != 1
                    or node.st_size > maximum_bytes
                ):
                    raise SshTransportError(
                        "execution.remote_download_invalid",
                        "download exceeds its regular-file allowance",
                    )
                physical = (node.st_dev, node.st_ino)
                if original[0] is None:
                    original[0] = physical
                elif original[0] != physical:
                    raise SshTransportError(
                        "execution.remote_download_changed",
                        "download staging was replaced",
                    )

            arguments = self.download_arguments(
                source, staged, expires - time.monotonic()
            )
            options = (
                {}
                if self.worker.reachability is None
                else {
                    "interrupt_guard": current,
                    "file_limit_bytes": max(64 * 1024**2, maximum_bytes),
                }
            )
            result = process_runner.run(
                arguments,
                cwd=cwd,
                timeout_seconds=expires - time.monotonic(),
                **options,
            )
            if result.returncode:
                return result
            current()
            os.chmod(staged, 0o600)
            with _source_file(staged, maximum_bytes, current) as (
                _current,
                digest,
                _size,
            ):
                if expected_digest is not None and digest != expected_digest:
                    raise SshTransportError(
                        "execution.remote_download_digest_mismatch",
                        "download bytes differ from receiver evidence",
                    )
            # A link creates the final name without replacing a racing destination.
            # The private staging name is removed before the caller can import it.
            current()
            try:
                os.link(staged, selected, follow_symlinks=False)
            except FileExistsError as exc:
                raise SshTransportError(
                    "execution.remote_destination_invalid",
                    "evidence destination appeared during transfer",
                ) from exc
        return result


def remote_execute_arguments(
    worker: ExecutionWorker,
    command: str,
    timeout_seconds: float,
    *,
    login_shell: bool = True,
) -> tuple[str, ...]:
    return RemoteWorkerTransport(worker).execute_arguments(
        command, timeout_seconds, login_shell=login_shell
    )


def remote_upload_arguments(
    source: Path, worker: ExecutionWorker, destination: str, timeout_seconds: float
) -> tuple[str, ...]:
    return RemoteWorkerTransport(worker).upload_arguments(
        source, destination, timeout_seconds
    )


def remote_download_arguments(
    worker: ExecutionWorker, source: str, destination: Path, timeout_seconds: float
) -> tuple[str, ...]:
    return RemoteWorkerTransport(worker).download_arguments(
        source, destination, timeout_seconds
    )
