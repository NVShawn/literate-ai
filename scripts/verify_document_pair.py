#!/usr/bin/env python3
"""Compatibility entry point for the packaged document-pair verifier."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from literate_ai.document_pair_verification import *  # noqa: E402, F403, I001


if __name__ == "__main__":
    raise SystemExit(main())  # noqa: F405
