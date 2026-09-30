"""Execute every deterministic source-to-specification test fixture."""

from __future__ import annotations

import io
import json
from pathlib import Path

from literate_ai.cli import main

ROOT = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "source_to_specification"
)
CASES = (
    ROOT / "state-machine" / "case.json",
    ROOT / "library-consumer" / "case.json",
    ROOT / "two-revision-refresh" / "revision-2" / "case.json",
    ROOT / "contradictory-tests" / "case.json",
    ROOT / "prompt-injection" / "case.json",
    ROOT / "flavor-split" / "case.json",
)


def run() -> int:
    reports = []
    failed = False
    for case in CASES:
        output = io.StringIO()
        errors = io.StringIO()
        status = main(["spec", "conformance", str(case)], stdout=output, stderr=errors)
        payload = json.loads(output.getvalue() or errors.getvalue())
        reports.append(payload)
        failed = failed or status != 0
    print(json.dumps(reports, sort_keys=True, separators=(",", ":")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
