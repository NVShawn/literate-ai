"""Run pip without exporting the parent Make invocation into dependency builds."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    # Cargo creates its own jobserver. Inherited Make command-line overrides can
    # otherwise put `--` before Cargo's flags, turning those flags into targets.
    # Preserve package-source policy and explicit compiler concurrency settings.
    make_variables = {"makeflags", "mflags", "makeoverrides", "gnumakeflags"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() not in make_variables
    }
    return subprocess.run(
        [sys.executable, "-m", "pip", *sys.argv[1:]],
        env=environment,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
