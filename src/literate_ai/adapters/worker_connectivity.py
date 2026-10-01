"""Bounded SSH-only connectivity tests, independent of worker toolchains."""

from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor

from literate_ai.contracts import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)

from .builders._process import run_bounded_process
from .builders.python import BuildError
from .ssh_transport import SshTransportError, ssh_arguments
from .worker_capabilities import ssh_failure_details


def test_worker_connection(
    worker: ExecutionWorker, *, timeout_seconds: int = 10
) -> dict:
    result = {
        "worker_id": worker.worker_id,
        "status": "failed",
        "cause": None,
        "exit_status": None,
        "diagnostic": "",
        "remedy": "",
    }
    if worker.kind is not ExecutionWorkerKind.SSH:
        result.update(
            status="unsupported",
            cause="not-ssh",
            remedy="Select an SSH worker; this command tests SSH connectivity only.",
        )
        return result
    marker = "litai-ssh-" + secrets.token_hex(16)
    try:
        command = ssh_arguments(
            worker.endpoint,
            f"echo {marker}",
            timeout_seconds,
            transport=worker.transport,
            login_shell=False,
        )
        completed = run_bounded_process(
            command,
            cwd=None,
            environment=os.environ,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=65536,
            stderr_limit_bytes=65536,
            error_prefix="worker.test",
        )
    except (BuildError, SshTransportError) as exc:
        cause = (
            "timeout"
            if "timeout" in exc.code
            else "output-limit"
            if "output_limit" in exc.code
            else "client-unavailable"
            if "launch_failed" in exc.code
            else "transport"
        )
        result.update(
            cause=cause,
            diagnostic=str(exc),
            remedy="Check the SSH executable, network route and timeout.",
        )
        return result
    result["exit_status"] = completed.returncode
    if completed.returncode:
        result.update(ssh_failure_details(completed.stderr))
    elif marker.encode() not in completed.stdout.splitlines():
        result.update(
            cause="handshake",
            diagnostic="SSH did not return the expected handshake.",
            remedy="Check the remote login shell and forced-command configuration.",
        )
    else:
        result["status"] = "passed"
    return result


def test_worker_connections(
    catalog: ExecutionWorkerCatalog,
    *,
    worker_ids: list[str] | None = None,
    timeout_seconds: int = 10,
) -> list[dict]:
    selected = (
        catalog.workers
        if worker_ids is None
        else tuple(catalog.worker(key) for key in sorted(set(worker_ids)))
    )
    with ThreadPoolExecutor(max_workers=min(len(selected), 16) or 1) as pool:
        return list(
            pool.map(
                lambda worker: test_worker_connection(
                    worker, timeout_seconds=timeout_seconds
                ),
                selected,
            )
        )
