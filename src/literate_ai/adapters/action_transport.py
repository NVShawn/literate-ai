"""Literal invocation of explicitly configured command and SSH action receivers."""

from __future__ import annotations

import base64
import shlex

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.remote_transport import remote_execute_arguments
from literate_ai.adapters.ssh_transport import SshTransportError
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerKind,
)


def supports_action_transport(worker: ExecutionWorker) -> bool:
    return worker.action_protocol == LIFECYCLE_ACTION_WIRE_PROTOCOL and (
        worker.kind is ExecutionWorkerKind.COMMAND
        or (worker.is_remote and bool(worker.action_command))
    )


def action_receiver_command(
    worker: ExecutionWorker,
    deadline: ActionDispatchDeadline,
    *,
    mode: str | None = None,
) -> tuple[str, ...]:
    if mode not in (
        None,
        "--describe",
        "--describe-hardware",
        "--describe-tools",
        "--describe-tool-dependencies",
        "--verify-tool-selectors",
        "--describe-finalize-grant",
        "--read-return-blob",
    ):
        raise ActionWireError(
            "action_transport.mode_invalid", "unsupported receiver mode"
        )
    suffix = () if mode is None else (mode,)
    if worker.kind is ExecutionWorkerKind.COMMAND:
        return (*worker.command, *suffix)
    if not supports_action_transport(worker):
        raise ActionWireError(
            "action_transport.not_declared", "SSH action receiver is not configured"
        )
    argv = (*worker.action_command, *suffix)
    windows = worker.requirements.os_family == "windows"
    if windows:
        # Only literal single-quoted arguments enter PowerShell. The native receiver
        # inherits SSH stdin directly; no text pipeline re-encodes the wire bytes.
        command = "& " + " ".join("'" + item.replace("'", "''") + "'" for item in argv)
        command += "; exit $LASTEXITCODE"
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        command = (
            "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "
            + encoded
        )
        if len(command) > 7000:
            raise ActionWireError(
                "action_transport.command_oversized",
                "SSH receiver command exceeds native bounds",
            )
    else:
        command = "exec " + shlex.join(argv)
    if len(command.encode("utf-8")) > 32768:
        raise ActionWireError(
            "action_transport.command_oversized", "SSH receiver command exceeds bounds"
        )
    try:
        return remote_execute_arguments(
            worker,
            command,
            deadline.remaining(),
            # FreeBSD receivers use their exact configured argv through sshd's
            # native command shell. Interactive Bash is not a platform prerequisite
            # and can wait indefinitely in a pipe-only session.
            login_shell=worker.requirements.os_family not in {"windows", "freebsd"},
        )
    except SshTransportError as exc:
        raise ActionWireError(
            "action_transport.invalid", "SSH transport configuration is invalid"
        ) from exc
