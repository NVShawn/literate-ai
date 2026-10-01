"""Shared bounded supervision for admitted lifecycle children."""

from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
)
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import BuildError
from literate_ai.security import AuthorizationError


def run_admitted_worker_process(
    *,
    phase,
    inputs,
    launcher,
    input_record,
    input_identity,
    deadline,
    cwd,
    environment,
    cancelled=lambda: False,
    clock=lambda: datetime.now(UTC),
    cas_root=None,
    workspace_root=None,
):
    """Call only after phase-specific bounded input and grant admission."""
    if phase not in {"BUILD", "TEST", "EXECUTE", "ACCEPT"}:
        raise ValueError("unsupported authorized child phase")
    code_prefix = "action_" + phase.lower()
    tools = WorkerToolchainRegistry((launcher,))
    launcher_identity = tools.identities
    grant = inputs.authorization.grant
    request = inputs.intent.build_request

    def require_current():
        if cancelled():
            raise ActionWireError(
                f"{code_prefix}.cancelled", f"{phase} action was cancelled"
            )
        current = clock()
        deadline.remaining(now=current)
        try:
            grant.require_valid(request, now=current)
        except AuthorizationError as exc:
            raise ActionWireError(
                f"{code_prefix}.authority_invalid",
                f"{phase} grant is no longer current",
            ) from exc

    require_current()
    child_environment = dict(environment)
    child_environment.update(launcher.environment)
    control_environment = {
        "LITAI_" + phase + "_INPUT_IDENTITY": input_identity.uri,
        "LITAI_" + phase + "_DEADLINE": deadline.expires_at.isoformat(),
    }
    for name, path in (
        ("LITAI_" + phase + "_CAS", cas_root),
        ("LITAI_" + phase + "_WORKSPACE", workspace_root),
    ):
        if path is not None:
            if not isinstance(path, Path) or not path.is_absolute():
                raise ActionWireError(
                    f"{code_prefix}.private_path_invalid",
                    "worker path must be absolute",
                )
            control_environment[name] = str(path)
    reserved = {
        name.casefold()
        for name in (
            *control_environment,
            "LITAI_" + phase + "_CAS",
            "LITAI_" + phase + "_WORKSPACE",
        )
    }
    child_environment = {
        name: value
        for name, value in child_environment.items()
        if name.casefold() not in reserved
    }
    child_environment.update(control_environment)
    now = clock()
    timeout = min(deadline.remaining(now=now), (grant.expires_at - now).total_seconds())
    try:
        completed = run_bounded_process(
            launcher.command,
            cwd=cwd,
            environment=child_environment,
            timeout_seconds=timeout,
            stdout_limit_bytes=MAX_ACTION_RECORD_BYTES,
            stderr_limit_bytes=64 * 1024,
            error_prefix=code_prefix + "_process",
            input_bytes=input_record,
            input_limit_bytes=MAX_ACTION_RECORD_BYTES,
            interrupt_guard=require_current,
            terminate_descendants=True,
            poll_interval_seconds=0.1,
            trace=False,
        )
    except BuildError as exc:
        code = {
            code_prefix + "_process_timeout": f"{code_prefix}.expired",
            code_prefix + "_process_output_limit": f"{code_prefix}.output_oversized",
        }.get(exc.code, f"{code_prefix}.process_failed")
        raise ActionWireError(
            code, f"{phase} child failed its execution bounds"
        ) from exc
    require_current()
    tools.select(launcher_identity)
    if completed.returncode != 0:
        raise ActionWireError(
            f"{code_prefix}.process_failed", f"{phase} child exited unsuccessfully"
        )
    return completed.stdout
