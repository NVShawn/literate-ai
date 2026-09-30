"""Remove only the manifest-bound Literate AI private runtime and launcher."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from literate_ai.adapters.host_uninstall import (  # noqa: E402
    HostUninstallError,
    plan_host_uninstall,
    uninstall_host,
)
from literate_ai.adapters.user_paths import resolve_host_paths  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prefix", type=Path, default=Path(resolve_host_paths().install_root)
    )
    arguments = parser.parse_args()
    try:
        result = uninstall_host(plan_host_uninstall(arguments.prefix))
    except (HostUninstallError, OSError) as exc:
        print(f"uninstall_litai: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
