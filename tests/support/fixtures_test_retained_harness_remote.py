from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_retained_harness_remote``."""




















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

