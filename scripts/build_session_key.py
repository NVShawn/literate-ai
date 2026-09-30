#!/usr/bin/env python3
"""Return one filesystem-safe identity for a managed Make environment."""

from __future__ import annotations

import os
import re
import sys

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def main() -> int:
    configured = os.environ.get("LITAI_SESSION_ID")
    key = configured if configured is not None else f"make-{os.getppid()}"
    if not _SAFE_KEY.fullmatch(key):
        print(
            "LITAI_SESSION_ID must be 1-64 ASCII letters, digits, dots, "
            "underscores, or hyphens and start with a letter or digit",
            file=sys.stderr,
        )
        return 2
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
