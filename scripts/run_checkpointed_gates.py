"""Repository adapter for installed fail-fast gate checkpointing."""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))


_bootstrap()

from literate_ai.application.pinned_test_dispatch import (  # noqa: E402
    PinDispatchService,
)
from literate_ai.test_checkpointing import (  # noqa: E402
    load_completed as _load_completed,
)
from literate_ai.test_checkpointing import main as _main  # noqa: E402
from literate_ai.test_checkpointing import (  # noqa: E402
    run_gates,
)
from literate_ai.test_checkpointing import (  # noqa: E402
    store_completed as _store_completed,
)

__all__ = ["PinDispatchService", "_load_completed", "_store_completed", "run_gates"]


def main() -> int:
    return _main(["gates", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
