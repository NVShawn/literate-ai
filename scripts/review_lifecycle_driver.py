#!/usr/bin/env python3
"""Review the project lifecycle driver's trusted computing base and re-pin its identity.

``lifecycle_driver.implementation_identity`` in ``literate.project.json`` is the exact
project TCB that ``litai rebuild`` verifies before and after the host lifecycle. It goes
stale on every change under the declared implementation paths, which is the control
working: the pin asserts that the code trusted to drive the lifecycle was reviewed. An
independent agent reviewer that did not author the drift satisfies this; no human is
required.

This reports what changed since the pin so review is possible, and re-pins only when
asked. Re-pinning asserts review, so it is never the default.

Usage:
    review_lifecycle_driver.py                 # report drift, exit 1 when stale
    review_lifecycle_driver.py --record        # re-pin after reviewing the drift
    review_lifecycle_driver.py --json <path>   # also write a machine-readable report
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from literate_ai.adapters.project_lifecycle_driver import (  # noqa: E402
    lifecycle_driver_implementation_identity,
)
from literate_ai.projects import LoadedProject, ProjectConfigurationStore  # noqa: E402

PROJECT_FILE = "literate.project.json"


def changed_paths(root: Path, paths: tuple[str, ...]) -> list[str]:
    """Name the files under the declared TCB that git reports as changed."""

    pinned = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%H", "--", PROJECT_FILE],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = pinned.stdout.strip()
    if not commit:
        return []
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", commit, "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in diff.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument(
        "--record",
        action="store_true",
        help="re-pin the identity; assert that the drift below has been reviewed",
    )
    parser.add_argument("--json", type=Path, help="write the report here")
    args = parser.parse_args(argv)

    snapshot = ProjectConfigurationStore.discover(args.project)
    if snapshot is None:
        print(f"no {PROJECT_FILE} found", file=sys.stderr)
        return 2
    store = ProjectConfigurationStore(snapshot.root)
    project = LoadedProject(snapshot.root, snapshot.definition)
    driver = project.definition.lifecycle_driver
    if driver is None:
        print("project declares no lifecycle_driver", file=sys.stderr)
        return 2

    computed = lifecycle_driver_implementation_identity(project, driver)
    pinned = driver.implementation_identity
    current = computed == pinned
    drift = [] if current else changed_paths(project.root, driver.implementation_paths)

    report = {
        "schema": "literate-ai/lifecycle-driver-review@1",
        "driver_id": driver.driver_id,
        "state": "current" if current else "stale",
        "pinned_identity": pinned.uri,
        "computed_identity": computed.uri,
        "implementation_paths": list(driver.implementation_paths),
        "changed_since_pin": drift,
        "recorded": False,
    }

    if not current and args.record:
        store.update(
            snapshot,
            replace(
                snapshot.definition,
                lifecycle_driver=replace(driver, implementation_identity=computed),
            ),
        )
        report["recorded"] = True
        report["state"] = "current"

    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"lifecycle driver : {report['driver_id']}")
    print(f"state            : {report['state']}")
    print(f"pinned           : {pinned.uri}")
    print(f"computed         : {computed.uri}")
    if drift:
        print(f"changed since pin: {len(drift)} file(s) under the declared TCB")
        for path in drift[:20]:
            print(f"    {path}")
        if len(drift) > 20:
            print(f"    ... and {len(drift) - 20} more")
    if report["recorded"]:
        print(f"\nre-pinned {PROJECT_FILE}; refresh the documentation authority review")
        return 0
    if not current:
        print("\nreview the files above, then re-run with --record to re-pin")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
