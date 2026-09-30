#!/usr/bin/env python3
"""Remove validated project caches without depending on the cache-resident venv."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from literate_ai.cache_directories import clean_cache_directories


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=".")
    parser.add_argument("--really-clean", action="store_true")
    parser.add_argument(
        "--preserve-python-environments",
        action="store_true",
        help="retain OBJ_DIR/python-envs while removing other object state",
    )
    args = parser.parse_args()
    removed = clean_cache_directories(
        Path(args.project),
        environment=os.environ,
        remove_generated_sources=args.really_clean,
        preserve_object_subdirectories=(
            (Path("python-envs"),) if args.preserve_python_environments else ()
        ),
    )
    for path in removed:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
