"""Bounded POSIX observations after asynchronous process-tree termination."""

from __future__ import annotations

import subprocess
import time


def stopped_process_state(pid: int, *, timeout_seconds: float = 5.0) -> str:
    """Return the final state, allowing only absence or a zombie to stop polling."""

    deadline = time.monotonic() + timeout_seconds
    state = "unobserved"
    while (remaining := deadline - time.monotonic()) > 0:
        observed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=min(remaining, 1.0),
        )
        if observed.returncode not in (0, 1) or observed.stderr.strip():
            raise AssertionError(
                f"process-state observation failed: {observed.stderr!r}"
            )
        state = observed.stdout.strip()
        if not state or state.startswith("Z"):
            return state
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    return state
