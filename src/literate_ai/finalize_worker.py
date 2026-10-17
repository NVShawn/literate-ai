"""Bounded FINALIZE child entrypoint for private worker startup composition."""

import argparse
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.action_finalize_execution import (
    execute_worker_finalize_from_cas,
)
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_finalize_result_record import FinalizeWorkerResult
from literate_ai.contracts import ContentIdentity
from literate_ai.storage import FileSystemCAS


def main(
    argv=None,
    *,
    runtime_factory=None,
    proof_loader=None,
    verify_package=None,
    admission_guard=None,
    require_execution_authority=None,
):
    """Only private startup supplies code and proof lookup; stdin selects no plugins.

    The loader retrieves the exact predecessor records from already staged CAS.
    Those records are still untrusted and execution independently reopens them.
    The parent must supervise process lifetime and continuously validate authority.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cas", type=Path, default=os.environ.get("LITAI_FINALIZE_CAS")
    )
    parser.add_argument(
        "--workspace", type=Path, default=os.environ.get("LITAI_FINALIZE_WORKSPACE")
    )
    args = parser.parse_args(argv)
    try:
        if not all(
            callable(item)
            for item in (
                runtime_factory,
                proof_loader,
                verify_package,
                admission_guard,
                require_execution_authority,
            )
        ):
            raise ValueError("private FINALIZE configuration is incomplete")
        identity = ContentIdentity.parse_uri(
            os.environ["LITAI_FINALIZE_INPUT_IDENTITY"]
        )
        deadline = ActionDispatchDeadline(
            datetime.fromisoformat(os.environ["LITAI_FINALIZE_DEADLINE"])
        )
        deadline.remaining()
        for name, path in (
            ("LITAI_FINALIZE_CAS", args.cas),
            ("LITAI_FINALIZE_WORKSPACE", args.workspace),
        ):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError("private worker paths must be absolute")
            if name in os.environ and path != Path(os.environ[name]):
                raise ValueError("private path differs from supervisor")
        require_safe_directory(args.workspace)
        cas = FileSystemCAS(args.cas, create=False)
        content = sys.stdin.buffer.read(MAX_ACTION_RECORD_BYTES + 1)
        value = FinalizeWorkerInput.admit(content, identity, deadline)
        with redirect_stdout(sys.stderr):
            admission_guard()
            records = proof_loader(value, cas, deadline)
            admission_guard()
            deadline.remaining()
            result = execute_worker_finalize_from_cas(
                input_record=content,
                input_identity=identity,
                records=records,
                cas=cas,
                workspace_root=args.workspace,
                deadline=deadline,
                admission_guard=admission_guard,
                verify_package=verify_package,
                runtime_factory=runtime_factory,
                require_execution_authority=require_execution_authority,
            )
            FinalizeWorkerResult.admit(
                result,
                record_identity(result),
                input_record=content,
                input_identity=identity,
                deadline=deadline,
            )
            admission_guard()
            deadline.remaining()
    except Exception:
        print("FINALIZE child input or private runtime refused", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
