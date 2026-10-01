"""Worker-owned BUILD child supervision with live authority and process bounds."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.action_build_record import (
    BuildWorkerInput,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
)
from literate_ai.adapters.action_worker_process import run_admitted_worker_process
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.contracts.identity import ContentIdentity


def run_build_worker_process(
    *,
    launcher: LocalComponentToolBinding,
    input_record: bytes,
    input_identity: ContentIdentity,
    deadline: ActionDispatchDeadline,
    cwd: Path,
    environment: Mapping[str, str],
    cancelled: Callable[[], bool] = lambda: False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    cas_root: Path | None = None,
    workspace_root: Path | None = None,
) -> bytes:
    """Admit exact BUILD authority before supervising a private worker child."""
    admitted = BuildWorkerInput.admit(
        input_record, input_identity, deadline, now=clock()
    )
    return run_admitted_worker_process(
        phase="BUILD",
        inputs=admitted.inputs,
        launcher=launcher,
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
        cwd=cwd,
        environment=environment,
        cancelled=cancelled,
        clock=clock,
        cas_root=cas_root,
        workspace_root=workspace_root,
    )
