"""Built-in bounded SSH lifecycle handler for one exact selected worker."""

from __future__ import annotations

import base64
import json
import os
import shlex
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from literate_ai.adapters.source_materialization import (
    SourceMaterializationError,
    capture_source_archive,
)
from literate_ai.adapters.ssh_transport import (
    MAX_SSH_OUTPUT_BYTES,
    BoundedSshProcessRunner,
    SshProcessRunner,
    SshTransportError,
    scp_arguments,
    scp_download_arguments,
    ssh_arguments,
)
from literate_ai.contracts import (
    ContentIdentity,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionWorker,
    RemoteEvidenceCustodyReceipt,
    RemoteExecutionControlResult,
    canonical_json_bytes,
)
from literate_ai.diagnostics import log_operation

from .execution_dispatch import ExecutionDispatchAdapterError
from .remote_execution import (
    RemoteExecutionError,
    load_and_import_remote_evidence_bundle,
)


def _receiver_failure(completed) -> ExecutionDispatchAdapterError:
    """Preserve one bounded typed receiver error without exposing raw diagnostics."""

    try:
        # A bounded transport may retain a diagnostic tail beginning in the middle of
        # one UTF-8 code point. Replacement is safe here because only the final complete
        # line can become the strictly validated typed error envelope.
        lines = completed.stderr.decode("utf-8", errors="replace").splitlines()
        payload = next(line for line in reversed(lines) if line.strip())
        envelope = json.loads(payload)
        error = envelope["error"]
        code = error["code"]
        message = error["message"]
        if (
            envelope.get("schema") != "literate-ai/cli-error@1"
            or envelope.get("ok") is not False
            or not isinstance(code, str)
            or not code
            or not isinstance(message, str)
            or not message
        ):
            raise ValueError
    except (
        json.JSONDecodeError,
        KeyError,
        StopIteration,
        TypeError,
        ValueError,
    ):
        return ExecutionDispatchAdapterError(
            "execution.ssh_receiver_failed",
            f"SSH lifecycle receiver exited with status {completed.returncode}",
        )
    return ExecutionDispatchAdapterError(code, message)


def _posix_path(value: str) -> str:
    if value.startswith("~/"):
        return '"$HOME"/' + value.removeprefix("~/")
    return shlex.quote(value)


def _powershell_path(value: str) -> str:
    if value.startswith("~/"):
        suffix = value.removeprefix("~/").replace("/", "\\")
        return f"(Join-Path $HOME '{suffix}')"
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _powershell(command: str) -> str:
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return (
        f"powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}"
    )


def _windows_launcher_selection(worker: ExecutionWorker) -> tuple[str, ...]:
    """Select one configured or conventionally bootstrapped Windows receiver."""

    if worker.lifecycle_executable is not None:
        launcher = (
            "Join-Path $HOME "
            + _powershell_path(worker.lifecycle_executable.removeprefix("~/"))
            if worker.lifecycle_executable.startswith("~/")
            else _powershell_path(worker.lifecycle_executable)
        )
        return (
            f"$litaiPath={launcher}",
            (
                "if (-not (Test-Path -LiteralPath $litaiPath -PathType Leaf)) "
                "{ throw 'configured litai lifecycle executable is unavailable' }"
            ),
        )
    return (
        "$litai=$null",
        (
            "foreach ($fallback in @("
            "(Join-Path $HOME "
            "'.local\\share\\literate-ai\\venv\\Scripts\\litai.cmd'),"
            "(Join-Path $HOME "
            "'.local\\share\\literate-ai\\venv\\Scripts\\litai.exe'),"
            "(Join-Path $HOME '.local\\bin\\litai.cmd'),"
            "(Join-Path $HOME '.local\\bin\\litai.exe'))) { "
            "if (Test-Path $fallback -PathType Leaf) { "
            "$litai=Get-Item $fallback; break } }"
        ),
        ("if (-not $litai) { $litai=Get-Command litai -ErrorAction SilentlyContinue }"),
        "if (-not $litai) { throw 'litai is required on the SSH worker' }",
        ("$litaiPath=if ($litai.Source) { $litai.Source } else { $litai.FullName }"),
    )


def _posix_launcher_selection(worker: ExecutionWorker) -> str:
    """Select one configured or conventionally bootstrapped POSIX receiver."""

    if worker.lifecycle_executable is not None:
        configured = (
            '"$HOME"/' + _posix_path(worker.lifecycle_executable.removeprefix("~/"))
            if worker.lifecycle_executable.startswith("~/")
            else _posix_path(worker.lifecycle_executable)
        )
        return (
            f"litai_command={configured}; "
            'if [ ! -x "$litai_command" ]; then '
            "echo 'configured litai lifecycle executable is unavailable' >&2; "
            "exit 127; fi"
        )
    return (
        'if [ -x "$HOME/.local/share/literate-ai/venv/bin/litai" ]; then '
        'litai_command="$HOME/.local/share/literate-ai/venv/bin/litai"; '
        'elif [ -x "$HOME/.local/bin/litai" ]; then '
        'litai_command="$HOME/.local/bin/litai"; '
        "else litai_command=$(command -v litai 2>/dev/null || true); fi; "
        'if [ -z "$litai_command" ]; then '
        "echo 'litai is required on the SSH worker' >&2; exit 127; fi"
    )


@dataclass(frozen=True, slots=True)
class _Deadline:
    """A monotonic deadline plus the whole-second budget it was struck from."""

    monotonic_deadline: float
    timeout_seconds: int

    def remaining(self) -> int:
        remaining = self.monotonic_deadline - time.monotonic()
        if remaining <= 0:
            raise SshTransportError(
                "execution.ssh_timed_out", "SSH lifecycle exceeded its total deadline"
            )
        # Whole-second resolution only: monotonic() arithmetic can round a hair
        # past timeout_seconds on floating-point deadlines, and no caller here
        # schedules SSH timeouts to millisecond or finer precision anyway.
        return max(1, min(self.timeout_seconds, int(remaining)))


class SshLifecycleRequestHandler:
    """Materialize current authority and invoke the installed worker receiver."""

    def __init__(self, runner: SshProcessRunner | None = None) -> None:
        self.runner = runner or BoundedSshProcessRunner()

    def execute(
        self,
        worker: ExecutionWorker,
        request: ExecutionDispatchRequest,
        *,
        cwd: Path,
    ) -> ExecutionDispatchResult:
        if worker.endpoint is None or worker.workspace is None:
            raise ExecutionDispatchAdapterError(
                "execution.ssh_worker_invalid",
                "SSH worker requires an endpoint and workspace",
            )
        with log_operation(
            "ssh_lifecycle",
            worker=worker.worker_id,
            phase=request.action.value,
        ):
            deadline = _Deadline(
                time.monotonic() + request.timeout_seconds, request.timeout_seconds
            )
            # The request identity is stable across exact retries, while custody
            # workspaces are attempt-scoped.  A nonce prevents a failed or concurrent
            # attempt from deleting or colliding with another attempt's staging tree.
            run_key = f"{request.identity.digest[:24]}-{uuid.uuid4().hex[:12]}"
            incoming = f"{worker.workspace.rstrip('/')}/requests/{run_key}"
            workspace = f"{worker.workspace.rstrip('/')}/work/{run_key}"
            cas_root = f"{worker.workspace.rstrip('/')}/cache/worker-cas"
            evidence_bundle = f"{incoming}/remote-evidence.tar.gz"
            cleanup_ticket = f"{cas_root}/pending/{run_key}.json"
            acknowledgement_root = f"{cas_root}/acknowledgements"
            try:
                with tempfile.TemporaryDirectory(
                    prefix="litai-ssh-dispatch-"
                ) as directory:
                    staging = Path(directory)
                    captured = capture_source_archive(cwd, request, directory=staging)
                    request_path = staging / "request.json"
                    materialization_path = staging / "materialization.json"
                    worker_path = staging / "worker.json"
                    request_path.write_bytes(canonical_json_bytes(request.to_dict()))
                    materialization_path.write_bytes(
                        canonical_json_bytes(captured.materialization.to_dict())
                    )
                    worker_path.write_bytes(canonical_json_bytes(worker.to_dict()))
                    self._run(
                        worker,
                        self._setup_command(worker, incoming, workspace),
                        cwd,
                        deadline,
                    )
                    transfers = [
                        (captured.path, "source.tar.gz"),
                        (request_path, "request.json"),
                        (materialization_path, "materialization.json"),
                        (worker_path, "worker.json"),
                    ]
                    if captured.accepted_source_cache_path is not None:
                        transfers.append(
                            (
                                captured.accepted_source_cache_path,
                                "accepted-source-cache.tar.gz",
                            )
                        )
                    for source, name in transfers:
                        self._run_argv(
                            scp_arguments(
                                source,
                                worker.endpoint,
                                f"{incoming}/{name}",
                                deadline.remaining(),
                            ),
                            cwd,
                            deadline,
                        )
                    completed = self._run(
                        worker,
                        self._execute_command(
                            worker,
                            incoming,
                            workspace,
                            cas_root,
                            evidence_bundle,
                            cleanup_ticket,
                            has_accepted_source_cache=(
                                captured.accepted_source_cache_path is not None
                            ),
                        ),
                        cwd,
                        deadline,
                        require_success=False,
                    )
            except SourceMaterializationError as exc:
                raise ExecutionDispatchAdapterError(exc.code, exc.message) from exc
            except SshTransportError as exc:
                raise ExecutionDispatchAdapterError(exc.code, exc.message) from exc
            try:
                if len(completed.stdout) > MAX_SSH_OUTPUT_BYTES:
                    raise ExecutionDispatchAdapterError(
                        "execution.ssh_output_too_large",
                        "SSH control result exceeds the fixed one MiB limit",
                    )
                envelope = json.loads(completed.stdout.decode("utf-8"))
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("schema") != "literate-ai/cli-result@1"
                    or envelope.get("ok") is not True
                ):
                    raise ValueError
                control = RemoteExecutionControlResult.from_dict(envelope["result"])
                if (
                    control.request_identity != request.identity
                    or control.worker_identity != worker.identity
                ):
                    raise ValueError
            except ExecutionDispatchAdapterError:
                raise
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                if completed.returncode != 0:
                    raise _receiver_failure(completed) from exc
                raise ExecutionDispatchAdapterError(
                    "execution.ssh_receiver_result_invalid",
                    "SSH lifecycle receiver did not return one typed result",
                ) from exc
            transfer_custody = tempfile.TemporaryDirectory(
                prefix="litai-ssh-evidence-transfer-"
            )
            local_bundle = Path(transfer_custody.name) / "remote-evidence.tar.gz"
            try:
                transfer_error: SshTransportError | None = None
                for _attempt in range(3):
                    try:
                        local_bundle.unlink(missing_ok=True)
                        self._run_argv(
                            scp_download_arguments(
                                worker.endpoint,
                                evidence_bundle,
                                local_bundle,
                                deadline.remaining(),
                            ),
                            cwd,
                            deadline,
                        )
                        transfer_error = None
                        break
                    except SshTransportError as exc:
                        transfer_error = exc
                if transfer_error is not None:
                    raise transfer_error
                configured_object_root = Path(
                    os.environ.get("OBJ_DIR", str(cwd / "_build"))
                )
                if not configured_object_root.is_absolute():
                    configured_object_root = cwd / configured_object_root
                manifest, store_identity, imported = (
                    load_and_import_remote_evidence_bundle(
                        local_bundle,
                        expected_manifest_identity=control.manifest_identity,
                        expected_manifest_size=control.manifest_size,
                        expected_bundle_identity=control.evidence_reference.identity,
                        expected_bundle_size=control.bundle_size,
                        store_root=configured_object_root.resolve()
                        / "remote-evidence-cas",
                    )
                )
                result = control.bind_imported_manifest(manifest)
            except (RemoteExecutionError, SshTransportError, ValueError) as exc:
                transfer_custody.cleanup()
                raise ExecutionDispatchAdapterError(
                    getattr(exc, "code", "execution.ssh_evidence_transfer_failed"),
                    getattr(
                        exc,
                        "message",
                        "SSH lifecycle evidence transfer or import failed",
                    ),
                ) from exc
            transfer_custody.cleanup()
            acknowledgement_identity: ContentIdentity | None = None
            acknowledgement_error: Exception | None = None
            acknowledged = None
            for _attempt in range(3):
                acknowledged = self._run(
                    worker,
                    self._acknowledgement_command(
                        worker,
                        incoming,
                        cleanup_ticket,
                        acknowledgement_root,
                        control.manifest_identity,
                        control.evidence_reference.identity,
                    ),
                    cwd,
                    deadline,
                    require_success=False,
                )
                if acknowledged.returncode != 0:
                    acknowledgement_error = _receiver_failure(acknowledged)
                    continue
                try:
                    ack_envelope = json.loads(acknowledged.stdout.decode("utf-8"))
                    ack_result = ack_envelope["result"]
                    acknowledgement_identity = ContentIdentity.parse_uri(
                        str(ack_result["acknowledgement_identity"])
                    )
                    if (
                        ack_envelope.get("schema") != "literate-ai/cli-result@1"
                        or ack_envelope.get("ok") is not True
                        or ack_result.get("status") != "cleaned"
                        or ack_result.get("manifest_identity")
                        != control.manifest_identity.uri
                        or ack_result.get("bundle_identity")
                        != control.evidence_reference.identity.uri
                    ):
                        raise ValueError
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as exc:
                    acknowledgement_error = exc
                    acknowledgement_identity = None
                    continue
                break
            if acknowledgement_identity is None:
                raise ExecutionDispatchAdapterError(
                    "execution.ssh_cleanup_acknowledgement_invalid",
                    "SSH worker did not acknowledge exact evidence custody cleanup "
                    "after three attempts",
                ) from acknowledgement_error
            custody_receipt = RemoteEvidenceCustodyReceipt(
                request.identity,
                worker.identity,
                result.evidence_manifest.identity,
                result.evidence_reference.identity,
                store_identity,
                imported,
                result.evidence_manifest.artifact_identity,
                acknowledgement_identity,
            )
            result = replace(result, custody_receipt=custody_receipt)
            if (
                completed.returncode == 0
                and result.status is not DispatchResultStatus.PASSED
            ):
                raise ExecutionDispatchAdapterError(
                    "execution.ssh_receiver_status_mismatch",
                    "SSH receiver process succeeded but reported an "
                    "unsuccessful result",
                )
            if (
                completed.returncode != 0
                and result.status is DispatchResultStatus.PASSED
            ):
                raise ExecutionDispatchAdapterError(
                    "execution.ssh_receiver_status_mismatch",
                    "SSH receiver process failed but reported a passing result",
                )
            return result

    def _run(
        self,
        worker: ExecutionWorker,
        command: str,
        cwd: Path,
        deadline: _Deadline,
        *,
        require_success: bool = True,
    ):
        assert worker.endpoint is not None
        return self._run_argv(
            ssh_arguments(
                worker.endpoint,
                command,
                deadline.remaining(),
                transport=worker.transport,
                login_shell=worker.requirements.os_family != "windows",
            ),
            cwd,
            deadline,
            require_success=require_success,
        )

    def _run_argv(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        deadline: _Deadline,
        *,
        require_success: bool = True,
    ):
        completed = self.runner.run(argv, cwd=cwd, timeout_seconds=deadline.remaining())
        if require_success and completed.returncode != 0:
            raise SshTransportError(
                "execution.ssh_transport_phase_failed",
                f"SSH transport phase exited with status {completed.returncode}",
            )
        return completed

    @staticmethod
    def _setup_command(worker: ExecutionWorker, incoming: str, workspace: str) -> str:
        if worker.requirements.os_family == "windows":
            command = "; ".join(
                (
                    "$ErrorActionPreference='Stop'",
                    "$ProgressPreference='SilentlyContinue'",
                    f"$incoming={_powershell_path(incoming)}",
                    f"$workspace={_powershell_path(workspace)}",
                    (
                        "if ((Test-Path $incoming) -or (Test-Path $workspace)) { "
                        "throw 'request workspace already exists' }"
                    ),
                    (
                        "New-Item -ItemType Directory -Force -Path "
                        "(Split-Path -Parent $incoming) | Out-Null"
                    ),
                    "New-Item -ItemType Directory -Path $incoming | Out-Null",
                )
            )
            return _powershell(command)
        return "; ".join(
            (
                "set -eu",
                f"incoming={_posix_path(incoming)}",
                f"workspace={_posix_path(workspace)}",
                'test ! -e "$incoming"',
                'test ! -e "$workspace"',
                'mkdir -p -- "${incoming%/*}"',
                'mkdir -- "$incoming"',
            )
        )

    @staticmethod
    def _execute_command(
        worker: ExecutionWorker,
        incoming: str,
        workspace: str,
        cas_root: str,
        evidence_bundle: str | None = None,
        cleanup_ticket: str | None = None,
        *,
        has_accepted_source_cache: bool = False,
    ) -> str:
        evidence_bundle = evidence_bundle or f"{incoming}/remote-evidence.tar.gz"
        cleanup_ticket = cleanup_ticket or (
            f"{cas_root}/pending/{incoming.rsplit('/', 1)[-1]}.json"
        )
        if worker.requirements.os_family == "windows":
            launcher_selection = _windows_launcher_selection(worker)
            command = "; ".join(
                (
                    "$ErrorActionPreference='Stop'",
                    "$ProgressPreference='SilentlyContinue'",
                    f"$incoming={_powershell_path(incoming)}",
                    f"$workspace={_powershell_path(workspace)}",
                    f"$cas={_powershell_path(cas_root)}",
                    f"$evidence={_powershell_path(evidence_bundle)}",
                    f"$cleanupTicket={_powershell_path(cleanup_ticket)}",
                    *launcher_selection,
                    (
                        "& $litaiPath worker execute --worker-file "
                        "(Join-Path $incoming 'worker.json') --request "
                        "(Join-Path $incoming 'request.json') --materialization "
                        "(Join-Path $incoming 'materialization.json') --archive "
                        "(Join-Path $incoming 'source.tar.gz') --workspace "
                        "$workspace --cas-root $cas --evidence-output $evidence "
                        "--cleanup-ticket $cleanupTicket"
                        + (
                            " --accepted-source-cache (Join-Path $incoming "
                            "'accepted-source-cache.tar.gz')"
                            if has_accepted_source_cache
                            else ""
                        )
                    ),
                    "$status=$LASTEXITCODE",
                    "exit $status",
                )
            )
            return _powershell(command)
        launcher_selection = _posix_launcher_selection(worker)
        return "; ".join(
            (
                f"incoming={_posix_path(incoming)}",
                f"workspace={_posix_path(workspace)}",
                f"cas={_posix_path(cas_root)}",
                f"evidence={_posix_path(evidence_bundle)}",
                f"cleanup_ticket={_posix_path(cleanup_ticket)}",
                (
                    'PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"; '
                    "export PATH"
                ),
                launcher_selection,
                (
                    '"$litai_command" worker execute --worker-file '
                    '"$incoming/worker.json" '
                    '--request "$incoming/request.json" --materialization '
                    '"$incoming/materialization.json" --archive '
                    '"$incoming/source.tar.gz" --workspace "$workspace" '
                    '--cas-root "$cas" --evidence-output "$evidence" '
                    '--cleanup-ticket "$cleanup_ticket"'
                    + (
                        " --accepted-source-cache "
                        '"$incoming/accepted-source-cache.tar.gz"'
                        if has_accepted_source_cache
                        else ""
                    )
                ),
                "status=$?",
                'exit "$status"',
            )
        )

    @staticmethod
    def _acknowledgement_command(
        worker: ExecutionWorker,
        incoming: str,
        cleanup_ticket: str,
        acknowledgement_root: str,
        manifest_identity: ContentIdentity,
        bundle_identity: ContentIdentity,
    ) -> str:
        if worker.requirements.os_family == "windows":
            selection = _windows_launcher_selection(worker)
            return _powershell(
                "; ".join(
                    (
                        "$ErrorActionPreference='Stop'",
                        f"$incoming={_powershell_path(incoming)}",
                        f"$ticket={_powershell_path(cleanup_ticket)}",
                        f"$acks={_powershell_path(acknowledgement_root)}",
                        *selection,
                        (
                            "& $litaiPath worker acknowledge --cleanup-ticket $ticket "
                            f"--manifest-identity '{manifest_identity.uri}' "
                            f"--bundle-identity '{bundle_identity.uri}' "
                            "--acknowledgement-root $acks"
                        ),
                        "$status=$LASTEXITCODE",
                        (
                            "if ($status -eq 0) { Remove-Item -LiteralPath $incoming "
                            "-Recurse -Force -ErrorAction SilentlyContinue }"
                        ),
                        "exit $status",
                    )
                )
            )
        selection = _posix_launcher_selection(worker)
        return "; ".join(
            (
                "set -u",
                f"incoming={_posix_path(incoming)}",
                f"ticket={_posix_path(cleanup_ticket)}",
                f"acks={_posix_path(acknowledgement_root)}",
                (
                    'PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"; '
                    "export PATH"
                ),
                selection,
                (
                    '"$litai_command" worker acknowledge --cleanup-ticket "$ticket" '
                    f"--manifest-identity {shlex.quote(manifest_identity.uri)} "
                    f"--bundle-identity {shlex.quote(bundle_identity.uri)} "
                    '--acknowledgement-root "$acks"'
                ),
                "status=$?",
                'if [ "$status" -eq 0 ]; then rm -rf -- "$incoming"; fi',
                'exit "$status"',
            )
        )


__all__ = ["SshLifecycleRequestHandler"]
