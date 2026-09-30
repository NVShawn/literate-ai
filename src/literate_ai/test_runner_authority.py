"""Exact source-closure identities for repository-owned test runners."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from literate_ai.contracts.identity import ContentIdentity, canonical_identity

SAMPLE_TEST_RUNNER_SOURCE_PATHS = (
    "scripts/run_samples.py",
    "tests/conformance/support/durable_split_service.py",
    "tests/conformance/support/runtime_oracles.py",
    "tests/conformance/support/sample_runner.py",
    "tests/conformance/support/standard_service_stack.py",
)


def source_closure_identity(
    repository: Path, *, schema: str, paths: Sequence[str]
) -> ContentIdentity:
    """Identify canonical relative paths and their exact current file bytes."""

    return canonical_identity(
        {
            "schema": schema,
            "files": [
                {
                    "path": path,
                    "digest": hashlib.sha256(
                        (repository / path).read_bytes()
                    ).hexdigest(),
                }
                for path in paths
            ],
        }
    )


def sample_test_runner_source_closure_identity(repository: Path) -> ContentIdentity:
    """Identify the complete self-hosting sample runner source closure."""

    return source_closure_identity(
        repository,
        schema="literate-ai/sample-test-runner-source-closure@1",
        paths=SAMPLE_TEST_RUNNER_SOURCE_PATHS,
    )


__all__ = [
    "SAMPLE_TEST_RUNNER_SOURCE_PATHS",
    "sample_test_runner_source_closure_identity",
    "source_closure_identity",
]
