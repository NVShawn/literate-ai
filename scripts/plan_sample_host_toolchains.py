#!/usr/bin/env python3
"""Project selected conformance samples to their host-toolchain Flavor mix-ins."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform", required=True, choices=("linux", "macos", "windows")
    )
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--flavor", action="append", default=[])
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(root))

    isolated_names = ("LITAI_CONFIG_DIR", "LITAI_MCP_TRANSPORT")
    present = {name: name in os.environ for name in isolated_names}
    previous = {name: os.environ.get(name) for name in isolated_names}
    try:
        from tests.conformance.support.sample_runner import (
            required_host_flavor_names,
        )
    finally:
        for name in isolated_names:
            if present[name]:
                assert previous[name] is not None
                os.environ[name] = previous[name]
            else:
                os.environ.pop(name, None)

    names = required_host_flavor_names(
        root / "samples",
        sample_patterns=tuple(arguments.sample or ("regenerative-roundtrip",)),
        flavor_selectors=tuple(arguments.flavor),
        platform=arguments.platform,
    )
    print(
        json.dumps(
            {
                "schema": "literate-ai/sample-host-toolchain-plan@1",
                "flavors": list(names),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
