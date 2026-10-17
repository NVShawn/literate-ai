"""Bounded GENERATE child entry point for trusted worker startup composition."""

from __future__ import annotations

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
)
from literate_ai.adapters.action_generate_execution import (
    execute_worker_generation,
)
from literate_ai.adapters.action_generate_record import (
    GENERATE_CAS_ENV,
    GENERATE_DEADLINE_ENV,
    GENERATE_INPUT_IDENTITY_ENV,
    GENERATE_WORKSPACE_ENV,
)
from literate_ai.contracts import ContentIdentity
from literate_ai.storage import FileSystemCAS


def main(argv=None, *, runtime_factory=None, admission_guard=None) -> int:
    """Called by a measured startup wrapper with its private runtime factory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cas", type=Path, default=os.environ.get(GENERATE_CAS_ENV))
    parser.add_argument(
        "--workspace", type=Path, default=os.environ.get(GENERATE_WORKSPACE_ENV)
    )
    args = parser.parse_args(argv)
    try:
        if not callable(runtime_factory) or not callable(admission_guard):
            raise ValueError("worker runtime is not configured")
        identity = ContentIdentity.parse_uri(os.environ[GENERATE_INPUT_IDENTITY_ENV])
        deadline = ActionDispatchDeadline(
            datetime.fromisoformat(os.environ[GENERATE_DEADLINE_ENV])
        )
        deadline.remaining()
        for name, path in (
            (GENERATE_CAS_ENV, args.cas),
            (GENERATE_WORKSPACE_ENV, args.workspace),
        ):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError("private worker paths must be absolute")
            if name in os.environ and path != Path(os.environ[name]):
                raise ValueError("private worker path differs from supervisor")
        require_safe_directory(args.workspace)
        cas = FileSystemCAS(args.cas, create=False)
        content = sys.stdin.buffer.read(MAX_ACTION_RECORD_BYTES + 1)
        if len(content) > MAX_ACTION_RECORD_BYTES:
            raise ValueError("GENERATE input is oversized")
        with redirect_stdout(sys.stderr):
            result = execute_worker_generation(
                input_record=content,
                input_identity=identity,
                deadline=deadline,
                cas=cas,
                workspace_root=args.workspace,
                runtime_factory=runtime_factory,
                admission_guard=admission_guard,
            )
        if not isinstance(result, bytes) or len(result) > MAX_ACTION_RECORD_BYTES:
            raise ValueError("GENERATE result is invalid")
    except Exception:
        # This process boundary must not disclose factory exceptions, host paths,
        # credentials, or partial result bytes. The supervisor owns the exit code.
        print("GENERATE child input or private runtime refused", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
