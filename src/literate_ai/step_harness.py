"""Evidence-aware adapter for one command-line release step."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .diagnostics import report_progress
from .evidence_ledger import (
    EVIDENCE_PARENT_ENVIRONMENT,
    attach_run,
    open_run,
    record_subprocess,
)


def run_step(
    argv: Sequence[str],
    *,
    name: str,
    path: str | None = None,
    cwd: Path | str | None = None,
) -> int:
    """Run one step while retaining evidence in an ambient or owned run."""

    project_root = Path.cwd()
    run = attach_run(project_root)
    owns_evidence_run = run is None
    if owns_evidence_run:
        run = open_run(project_root, operation="step")
    if run is not None:
        report_progress(f"Release evidence root: {run.root}")

    completed: subprocess.CompletedProcess[str] | None = None
    try:
        if run is None:
            completed = subprocess.run(list(argv), cwd=cwd, check=False)
        else:
            completed = record_subprocess(
                argv,
                cwd=cwd or project_root,
                run=run,
                parent=os.environ.get(EVIDENCE_PARENT_ENVIRONMENT),
                path=path or f"steps/{name}",
                operation="release.step",
                tee=True,
            )
        if completed.returncode < 0:
            return 128 - completed.returncode
        return completed.returncode
    finally:
        if run is not None and owns_evidence_run:
            run.close(
                "passed"
                if completed is not None and completed.returncode == 0
                else "failed"
            )


def main(arguments: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if arguments is None else arguments)
    try:
        separator = values.index("--")
    except ValueError as exc:
        raise SystemExit("litai_step requires '--' before the child command") from exc
    options = values[:separator]
    argv = values[separator + 1 :]
    if not argv:
        raise SystemExit("litai_step requires a child command")
    name: str | None = None
    path: str | None = None
    index = 0
    while index < len(options):
        option = options[index]
        if option == "--name" and index + 1 < len(options):
            name = options[index + 1]
            index += 2
        elif option == "--path" and index + 1 < len(options):
            path = options[index + 1]
            index += 2
        else:
            raise SystemExit(f"unrecognized option: {option}")
    if not name:
        raise SystemExit("litai_step requires --name")
    return run_step(argv, name=name, path=path)


if __name__ == "__main__":
    raise SystemExit(main())
