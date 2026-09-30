"""Executable JSON report for the read-only OVA shadow comparator."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from .shadow import OvaShadowComparator, ShadowStatus


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare two OVA artifact trees without modifying either tree."
    )
    parser.add_argument("--baseline", required=True, help="baseline file or tree")
    parser.add_argument("--candidate", required=True, help="candidate file or tree")
    parser.add_argument("--baseline-label", default="baseline")
    parser.add_argument("--candidate-label", default="candidate")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = OvaShadowComparator().compare(
            arguments.baseline,
            arguments.candidate,
            baseline_label=arguments.baseline_label,
            candidate_label=arguments.candidate_label,
        )
    except (OSError, ValueError) as exc:
        _parser().error(str(exc))
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    statuses = {item.status for item in report.comparisons}
    if statuses & {ShadowStatus.INVALID, ShadowStatus.INTERRUPTED}:
        return 2
    return 0 if report.exact else 1


if __name__ == "__main__":
    raise SystemExit(main())
