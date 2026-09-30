"""Run pytest under structured operation/subprocess logging for CI (#57).

litai profile only wraps litai's own rebuild/build/test/generate
subcommands, not an arbitrary pytest invocation, so CI's test step never
produced any timing data -- the structured-log sink diagnostics.py adds is
only opened by litai profile's own implementation. Reuse the same sink and
report machinery directly around pytest so a CI run always has an NDJSON
trace and a literate-ai/profile-report@1 hotspot summary to pull, without
re-running anything.

Usage: python scripts/profile_pytest.py <log-path> <report-path> -- <pytest args...>

Caveat: the structured-log sink is a contextvars.ContextVar in the
coordinating process. pytest-xdist workers (this repo's default
"-n auto --dist=loadfile") run in separate OS processes, so instrumented
operation/subprocess calls made inside a test body executing on a worker
are not captured here -- only whatever happens in the coordinator (test
collection, plugin hooks) is. Still worth running for the CI cases where
that's the interesting part; a complete per-test trace would require
running single-process, trading away xdist's parallelism.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))


_bootstrap()

import json  # noqa: E402

from literate_ai.cli.profile import (  # noqa: E402
    build_profile_report,
    parse_profile_log,
)
from literate_ai.diagnostics import operation_log  # noqa: E402


@contextmanager
def _suppress_source_tree_bytecode() -> Iterator[None]:
    """Keep coordinator and xdist bytecode out of the admitted checkout.

    The Makefile redirects bytecode beneath ``OBJ_DIR``, but this CI adapter is
    also invoked directly.  ``sys.dont_write_bytecode`` protects the coordinator;
    the environment setting carries the same invariant into xdist workers.  Restore
    both values because unit tests call :func:`main` in-process.
    """

    previous_environment = os.environ.get("PYTHONDONTWRITEBYTECODE")
    previous_runtime = sys.dont_write_bytecode
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous_runtime
        if previous_environment is None:
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        else:
            os.environ["PYTHONDONTWRITEBYTECODE"] = previous_environment


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[2] != "--":
        raise SystemExit(
            "usage: profile_pytest.py <log-path> <report-path> -- <pytest args...>"
        )
    log_path = Path(argv[0])
    report_path = Path(argv[1])
    pytest_args = argv[3:]

    with _suppress_source_tree_bytecode():
        import pytest

        with operation_log(log_path):
            exit_code = int(pytest.main(pytest_args))

    records = parse_profile_log(log_path)
    report = build_profile_report(records, {"exit_code": exit_code})
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
