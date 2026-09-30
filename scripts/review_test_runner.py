#!/usr/bin/env python3
"""Review and optionally record the sample test-runner source-closure identity."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from literate_ai.projects import ProjectConfigurationStore  # noqa: E402
from literate_ai.test_runner_authority import (  # noqa: E402
    SAMPLE_TEST_RUNNER_SOURCE_PATHS,
    sample_test_runner_source_closure_identity,
)

PROJECT_FILE = "literate.project.json"


def changed_paths(root: Path) -> list[str]:
    pinned = subprocess.run(
        ("git", "-C", str(root), "log", "-1", "--format=%H", "--", PROJECT_FILE),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not pinned:
        return []
    result = subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "diff",
            "--name-only",
            pinned,
            "--",
            *SAMPLE_TEST_RUNNER_SOURCE_PATHS,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--json", type=Path)
    arguments = parser.parse_args(argv)

    snapshot = ProjectConfigurationStore.discover(arguments.project)
    if snapshot is None or snapshot.definition.test_receipt_policy is None:
        print("project declares no test_receipt_policy", file=sys.stderr)
        return 2
    store = ProjectConfigurationStore(snapshot.root)
    policy = snapshot.definition.test_receipt_policy
    computed = sample_test_runner_source_closure_identity(snapshot.root)
    current = computed == policy.runner_identity
    drift = [] if current else changed_paths(snapshot.root)
    report = {
        "schema": "literate-ai/test-runner-review@1",
        "suite_id": policy.suite_id,
        "state": "current" if current else "stale",
        "pinned_identity": policy.runner_identity.uri,
        "computed_identity": computed.uri,
        "source_paths": list(SAMPLE_TEST_RUNNER_SOURCE_PATHS),
        "changed_since_pin": drift,
        "recorded": False,
    }
    if not current and arguments.record:
        store.update(
            snapshot,
            replace(
                snapshot.definition,
                test_receipt_policy=replace(policy, runner_identity=computed),
            ),
        )
        report["state"] = "current"
        report["recorded"] = True
    if arguments.json:
        arguments.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"sample test runner: {report['state']}")
    print(f"pinned            : {policy.runner_identity.uri}")
    print(f"computed          : {computed.uri}")
    for path in drift:
        print(f"    {path}")
    if report["recorded"]:
        print(f"re-pinned {PROJECT_FILE}; refresh documentation authority")
        return 0
    if not current:
        print("review the source closure, then re-run with --record")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
