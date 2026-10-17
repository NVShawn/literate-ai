"""Bound a private FINALIZE child to exact intent and continuously checked authority."""

from datetime import UTC, datetime

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_worker_process import _run_worker_process


def run_finalize_worker_process(
    *,
    launcher,
    input_record,
    input_identity,
    deadline,
    cwd,
    environment,
    require_execution_authority,
    cancelled=lambda: False,
    clock=lambda: datetime.now(UTC),
    cas_root=None,
    workspace_root=None,
):
    """Caller owns proof admission and private authority; child must reopen proof.

    The mandatory callback receives the admitted exact FINALIZE intent on every
    supervision poll. It must validate current execution permission for that
    project/package; descriptor admission alone grants no execution authority.
    Returned stdout is untrusted until independent result admission.
    """
    if not callable(require_execution_authority):
        raise TypeError("FINALIZE requires explicit private execution authority")
    admitted = FinalizeWorkerInput.admit(input_record, input_identity, deadline)

    def current():
        if cancelled():
            raise ActionWireError(
                "action_finalize.cancelled", "FINALIZE action was cancelled"
            )
        deadline.remaining(now=clock())
        require_execution_authority(admitted)
        deadline.remaining(now=clock())

    return _run_worker_process(
        phase="FINALIZE",
        launcher=launcher,
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
        cwd=cwd,
        environment=environment,
        clock=clock,
        cas_root=cas_root,
        workspace_root=workspace_root,
        require_current=current,
        expires_at=deadline.expires_at,
    )
