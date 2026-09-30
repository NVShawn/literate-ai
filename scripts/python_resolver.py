"""Validate the interpreter executing this bootstrap probe."""

from __future__ import annotations

import json
import sys


def main() -> int:
    if sys.argv[1:] != ["--check"]:
        raise SystemExit("usage: python_resolver.py --check")
    compatible = sys.version_info >= (3, 11)
    print(
        json.dumps(
            {
                "compatible": compatible,
                "executable": sys.executable,
                "version": list(sys.version_info[:3]),
            },
            sort_keys=True,
        )
    )
    return 0 if compatible else 1


if __name__ == "__main__":
    raise SystemExit(main())
