"""Repository adapter for the evidence-aware one-step harness."""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))


_bootstrap()

from literate_ai.step_harness import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
