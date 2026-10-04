"""Shared test fixtures extracted from test_retained_harness_remote."""

from __future__ import annotations

from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
)


def _worker() -> ExecutionWorker:
    return ExecutionWorker(
        "linux",
        ExecutionWorkerKind.SSH,
        endpoint="runner@linux.example",
        workspace="~/literate-ai",
        requirements=ExecutionRequirements(os_family="linux"),
        lifecycle_executable="~/.local/bin/litai",
    )
