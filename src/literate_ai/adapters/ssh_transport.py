"""Bounded, noninteractive SSH transport shared by lifecycle callers."""

from __future__ import annotations

import base64
import re
import shlex
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from literate_ai.adapters._processes import (
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.diagnostics import trace_subprocess

MAX_SSH_OUTPUT_BYTES = 1024 * 1024
MAX_SSH_DIAGNOSTIC_BYTES = 64 * 1024
SSH_DIAGNOSTIC_TRUNCATION_MARKER = b"[litai:ssh stderr truncated; tail follows]\n"
_TERMINATE_WAIT_TIMEOUT_SECONDS = 5.0
_ENDPOINT = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$")
_TRANSPORT = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


class SshTransportError(RuntimeError):
    """One bounded SSH operation could not satisfy its transport contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class SshProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class SshProcessRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> SshProcessResult: ...


class BoundedSshProcessRunner:
    """Run one direct SSH-family argv with bounded file-backed output."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> SshProcessResult:
        if timeout_seconds <= 0:
            raise SshTransportError(
                "execution.ssh_timeout_invalid", "SSH timeout must be positive"
            )
        try:
            working_directory = Path(cwd).resolve(strict=True)
        except OSError as exc:
            raise SshTransportError(
                "execution.ssh_working_directory_invalid",
                "SSH working directory is unavailable",
            ) from exc
        if not working_directory.is_dir() or working_directory.is_symlink():
            raise SshTransportError(
                "execution.ssh_working_directory_invalid",
                "SSH working directory must be a non-symlink directory",
            )
        with (
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            _started = datetime.now(UTC)
            trace_subprocess(argv, cwd=working_directory)
            ownership = create_process_tree_ownership()
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=working_directory,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    **ownership.popen_options,
                )
            except OSError as exc:
                ownership.release()
                raise SshTransportError(
                    "execution.ssh_unavailable",
                    f"SSH transport executable is unavailable: {argv[0]}",
                ) from exc
            ownership.bind(process.pid)
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                terminate_process_tree(process, ownership=ownership)
                try:
                    process.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    with suppress(OSError):
                        process.kill()
                    process.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)
                raise SshTransportError(
                    "execution.ssh_timed_out",
                    f"SSH operation exceeded {timeout_seconds:g} seconds",
                ) from exc
            finally:
                ownership.release()
            stdout_file.flush()
            stderr_file.flush()
            if stdout_file.tell() > MAX_SSH_OUTPUT_BYTES:
                raise SshTransportError(
                    "execution.ssh_output_too_large",
                    "SSH output exceeds the one MiB limit",
                )
            stdout_file.seek(0)
            stdout = stdout_file.read(MAX_SSH_OUTPUT_BYTES + 1)
            diagnostic_size = stderr_file.tell()
            if diagnostic_size > MAX_SSH_DIAGNOSTIC_BYTES:
                retained = MAX_SSH_DIAGNOSTIC_BYTES - len(
                    SSH_DIAGNOSTIC_TRUNCATION_MARKER
                )
                stderr_file.seek(-retained, 2)
                stderr = SSH_DIAGNOSTIC_TRUNCATION_MARKER + stderr_file.read(retained)
            else:
                stderr_file.seek(0)
                stderr = stderr_file.read(MAX_SSH_DIAGNOSTIC_BYTES)
            trace_subprocess(
                argv,
                cwd=working_directory,
                status=process.returncode,
                stdout=stdout,
                stderr=stderr,
                started_at=_started,
            )
            return SshProcessResult(process.returncode, stdout, stderr)


def _endpoint(value: str) -> str:
    if not isinstance(value, str) or not _ENDPOINT.fullmatch(value):
        raise SshTransportError(
            "execution.ssh_endpoint_invalid",
            "SSH endpoint must be one literal username@host value",
        )
    return value


def _timeout(value: float) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise SshTransportError(
            "execution.ssh_timeout_invalid", "SSH timeout must be positive"
        )
    return max(1, min(30, int(value)))


def _transport_binary(value: str) -> str:
    if not isinstance(value, str) or not _TRANSPORT.fullmatch(value):
        raise SshTransportError(
            "execution.ssh_transport_invalid",
            "SSH transport must be a portable lower-case identifier",
        )
    return value


def ssh_arguments(
    endpoint: str,
    command: str,
    timeout_seconds: float,
    *,
    transport: str = "ssh",
    login_shell: bool = True,
) -> tuple[str, ...]:
    """Build one noninteractive SSH-flag-compatible argv for the configured transport.

    The transport binary is per-worker data (``ExecutionWorker.transport``), not a
    framework constant: a private fleet may route some workers through a drop-in
    SSH-compatible wrapper (e.g. a VPN/Tailscale-aware launcher) instead of plain
    OpenSSH, while other workers keep using ``ssh`` directly.

    A bare ``ssh host command`` has sshd run ``$SHELL -c command`` -- for bash that
    is neither interactive nor a login shell, so it sources neither ``.bashrc`` nor
    ``.bash_profile``/``.profile``. A worker whose real toolchain (Homebrew, a
    pyenv/nvm shim, ...) only reaches ``PATH`` through one of those files then fails
    closed with a spurious "no compatible tool found" error. Login alone is not
    always enough either: a conventional ``.bash_profile`` that sources
    ``.bashrc``, and a ``.bashrc`` that (per common Bash convention) bails out
    early for non-interactive shells, means only a shell that is both a login
    shell *and* interactive reaches that PATH setup. Explicitly running the
    remote command through ``bash -lic`` forces both, regardless of how sshd
    would have invoked the remote's default shell, without requiring any
    worker-side dotfile change. The interactive flag prints a harmless
    "no job control in this shell" notice to stderr; that is expected and does
    not affect the command's exit status or stdout.

    Windows OpenSSH workers have no Bash requirement: their OS-authorized caller sets
    ``login_shell=False`` and sends the already encoded PowerShell receiver command
    directly to the server's native command shell.
    """

    if not isinstance(command, str) or not command or "\x00" in command:
        raise SshTransportError(
            "execution.ssh_command_invalid",
            "remote command must be nonempty and NUL-free",
        )
    if not isinstance(login_shell, bool):
        raise SshTransportError(
            "execution.ssh_shell_invalid",
            "SSH login-shell selection must be a boolean",
        )
    connect_timeout = _timeout(timeout_seconds)
    return (
        _transport_binary(transport),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        _endpoint(endpoint),
        f"bash -lic {shlex.quote(command)}" if login_shell else command,
    )


def scp_arguments(
    source: Path, endpoint: str, destination: str, timeout_seconds: float
) -> tuple[str, ...]:
    """Build one noninteractive SCP argv for an exact local regular file."""

    selected = Path(source)
    if selected.is_symlink() or not selected.is_file():
        raise SshTransportError(
            "execution.scp_source_invalid",
            "SCP source must be a regular non-symlink file",
        )
    if not isinstance(destination, str) or not destination or "\x00" in destination:
        raise SshTransportError(
            "execution.scp_destination_invalid",
            "SCP destination must be nonempty and NUL-free",
        )
    connect_timeout = _timeout(timeout_seconds)
    return (
        "scp",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        str(selected),
        f"{_endpoint(endpoint)}:{destination}",
    )


def scp_download_arguments(
    endpoint: str, source: str, destination: Path, timeout_seconds: float
) -> tuple[str, ...]:
    """Build one noninteractive SCP argv for one transport-defined remote file."""

    if (
        not isinstance(source, str)
        or not source
        or "\x00" in source
        or any(character in source for character in "\r\n")
        or not source.endswith("/remote-evidence.tar.gz")
    ):
        raise SshTransportError(
            "execution.scp_source_invalid",
            "SCP evidence source must be the transport-defined bundle path",
        )
    selected = Path(destination)
    if selected.exists() or selected.is_symlink() or not selected.parent.is_dir():
        raise SshTransportError(
            "execution.scp_destination_invalid",
            "SCP evidence destination must be a new file below an existing directory",
        )
    connect_timeout = _timeout(timeout_seconds)
    return (
        "scp",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        f"{_endpoint(endpoint)}:{source}",
        str(selected),
    )


__all__ = [
    "BoundedSshProcessRunner",
    "MAX_SSH_DIAGNOSTIC_BYTES",
    "MAX_SSH_OUTPUT_BYTES",
    "SSH_DIAGNOSTIC_TRUNCATION_MARKER",
    "SshProcessResult",
    "SshProcessRunner",
    "SshTransportError",
    "powershell_command",
    "powershell_home_path",
    "powershell_literal",
    "scp_arguments",
    "scp_download_arguments",
    "ssh_arguments",
]


def powershell_literal(value: str) -> str:
    """Quote ``value`` as one verbatim PowerShell string.

    PowerShell also treats U+2018 to U+201B as single quotes, so each is
    doubled like ``'``; otherwise one could end the literal early.
    """
    return "'" + re.sub("(['\u2018-\u201b])", r"\1\1", value) + "'"


def powershell_home_path(value: str) -> str:
    if value.startswith(("~/", "~\\")):
        relative = value[2:].replace("/", "\\")
        return f"(Join-Path $HOME {powershell_literal(relative)})"
    return powershell_literal(value)


def powershell_command(script: str) -> str:
    """Encode one PowerShell script for a Windows OpenSSH worker.

    The script's last native exit status becomes the SSH exit status, and
    progress records are suppressed so they never reach captured output.
    """

    body = f"$ProgressPreference = 'SilentlyContinue'; {script}; exit $LASTEXITCODE"
    encoded = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
    return (
        f"powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}"
    )
