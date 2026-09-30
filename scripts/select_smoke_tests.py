"""Select a bounded-duration smoke subset from pytest-split's recorded durations.

Greedy fastest-first selection biases toward a handful of trivial tests and can
leave whole modules (whole slices of core functionality) uncovered. This
selects breadth-first across test modules instead: each round takes every
module's cheapest remaining test that still fits the budget, so coverage
spreads across the full test tree before any one module gets a second test.
Tests with no recorded duration (new since the last `--store-durations` run)
are estimated at the median known duration rather than skipped.

Usage:
    python scripts/select_smoke_tests.py [--budget-seconds N] [--durations-path PATH]

Prints selected pytest node ids to stdout, one per line, so a caller can run:
    python -m pytest $(python scripts/select_smoke_tests.py)
A coverage/budget summary is written to stderr.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from statistics import median

_ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict[str, object]:
    document = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return document.get("tool", {}).get("smoke_tests", {})


def _collect_node_ids(testpaths: list[str]) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *testpaths],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    node_ids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "::" not in line:
            continue
        node_ids.append(line)
    return node_ids


def _module_of(node_id: str) -> str:
    return node_id.split("::", 1)[0]


def select_smoke_tests(
    budget_seconds: float,
    durations_path: Path,
    testpaths: list[str],
) -> tuple[list[str], dict[str, object]]:
    durations: dict[str, float] = {}
    if durations_path.exists():
        durations = json.loads(durations_path.read_text())

    node_ids = _collect_node_ids(testpaths)
    known = [durations[node_id] for node_id in node_ids if node_id in durations]
    fallback_duration = median(known) if known else 1.0

    by_module: dict[str, list[str]] = {}
    for node_id in node_ids:
        by_module.setdefault(_module_of(node_id), []).append(node_id)
    for module_node_ids in by_module.values():
        module_node_ids.sort(key=lambda n: durations.get(n, fallback_duration))

    modules = sorted(by_module)
    cursor = dict.fromkeys(modules, 0)
    selected: list[str] = []
    covered_modules: set[str] = set()
    spent = 0.0

    progressed = True
    while progressed and spent < budget_seconds:
        progressed = False
        for module in modules:
            remaining = by_module[module]
            index = cursor[module]
            if index >= len(remaining):
                continue
            node_id = remaining[index]
            cost = durations.get(node_id, fallback_duration)
            if spent + cost > budget_seconds:
                continue
            cursor[module] = index + 1
            selected.append(node_id)
            covered_modules.add(module)
            spent += cost
            progressed = True

    summary = {
        "budget_seconds": budget_seconds,
        "spent_seconds": round(spent, 3),
        "selected_tests": len(selected),
        "total_tests": len(node_ids),
        "covered_modules": len(covered_modules),
        "total_modules": len(modules),
        "tests_with_unknown_duration": sum(
            1 for node_id in selected if node_id not in durations
        ),
    }
    return selected, summary


def main(argv: list[str]) -> int:
    config = _config()
    budget_seconds = float(config.get("budget_seconds", 600))
    durations_path = _ROOT / str(config.get("durations_path", ".test_durations"))
    testpaths = list(config.get("testpaths", ["tests/unit"]))

    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--budget-seconds":
            budget_seconds = float(args.pop(0))
        elif arg == "--durations-path":
            durations_path = _ROOT / args.pop(0)
        elif arg == "--testpath":
            testpaths.append(args.pop(0))
        else:
            raise SystemExit(f"unrecognized argument: {arg}")

    if budget_seconds <= 0:
        print(
            "smoke-test budget is 0 or negative -- this selects NO tests, which "
            "means merging with no smoke coverage at all. Pass --budget-seconds "
            "explicitly if that's really intended.",
            file=sys.stderr,
        )
        return 1

    selected, summary = select_smoke_tests(budget_seconds, durations_path, testpaths)
    for node_id in selected:
        print(node_id)
    print(json.dumps(summary), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
