"""Run the spec-to-host conformance ladder over the sample catalog."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_TEST_PACKAGE_ENVIRONMENT = ("LITAI_CONFIG_DIR", "LITAI_MCP_TRANSPORT")


def _sample_main():
    """Import test-hosted implementation without retaining its test isolation.

    The reusable conformance implementation still lives below ``tests/``. Importing
    that package intentionally isolates operator configuration for unittest/pytest,
    but this file is also the production lifecycle-driver entrypoint. Restore the
    caller's exact environment after the import so normal host path resolution remains
    authoritative for the production process.
    """

    present = {name: name in os.environ for name in _TEST_PACKAGE_ENVIRONMENT}
    previous = {name: os.environ.get(name) for name in _TEST_PACKAGE_ENVIRONMENT}
    try:
        from tests.conformance.support.sample_runner import main
    finally:
        for name in _TEST_PACKAGE_ENVIRONMENT:
            if present[name]:
                assert previous[name] is not None
                os.environ[name] = previous[name]
            else:
                os.environ.pop(name, None)
    return main


def run() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(root))
    return _sample_main()(require_live_selection=True)


if __name__ == "__main__":
    raise SystemExit(run())
