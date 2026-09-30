"""Fail-closed pytest-testmon runner. Durations files never authorize skip.

Usage:
    python scripts/run_impact_pytest.py [--root PATH] [--python PATH]
        [--refresh] [--timeout SECONDS] [--decision-json PATH] -- [pytest args]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))


_bootstrap()

from literate_ai.ci_impact_run import (  # noqa: E402
    CiImpactRunError,
    resolve_pytest_impact_run,
    run_pytest_impact,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_impact_pytest.py",
        description=(
            "Run pytest with pytest-testmon only when the impact map is trusted. "
            "A stale or missing map, a durations file, or a missing plugin keeps "
            "the full suite."
        ),
    )
    parser.add_argument("--root", default=".", help="project tree that owns the map")
    parser.add_argument("--python", help="interpreter that should import testmon")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="run the full suite, collect a new map, and write the identity sidecar",
    )
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument(
        "--decision-json",
        help="write the skip/full-suite decision as JSON before pytest starts",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    pytest_args = list(args.pytest_args)
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]
    root = Path(args.root)
    try:
        decision = resolve_pytest_impact_run(
            root,
            pytest_args,
            python=args.python,
            refresh=args.refresh,
        )
        if args.decision_json:
            Path(args.decision_json).write_text(
                json.dumps(decision, indent=2) + "\n",
                encoding="utf-8",
            )
        completed, decision = run_pytest_impact(
            root,
            pytest_args,
            python=args.python,
            refresh=args.refresh,
            timeout=args.timeout,
            testmon_available=decision["testmon_available"],
        )
    except CiImpactRunError as exc:
        print(json.dumps({"code": exc.code, "message": exc.message}), file=sys.stderr)
        return 2
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if args.decision_json:
        Path(args.decision_json).write_text(
            json.dumps(decision, indent=2) + "\n",
            encoding="utf-8",
        )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
