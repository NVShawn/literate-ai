"""Locate the pip wheel used by real Python installation tests."""

from __future__ import annotations

import ensurepip
import os
import unittest
from pathlib import Path

# The Python installation fixtures lock this exact pip wheel's bytes.
PINNED_PIP_WHEEL = "pip-26.2.1-py3-none-any.whl"


def pinned_pip_wheel() -> Path:
    """Return LITAI_TEST_PIP_WHEEL, else the interpreter's bundled pinned pip wheel.

    Skips the calling test class when neither is available, for example when the
    interpreter bundles a different pip version or ensurepip is stripped.
    """

    value = os.environ.get("LITAI_TEST_PIP_WHEEL")
    if value:
        return Path(value).resolve(strict=True)
    bundled = Path(ensurepip.__file__).parent / "_bundled" / PINNED_PIP_WHEEL
    if not bundled.is_file():
        raise unittest.SkipTest(
            f"Set LITAI_TEST_PIP_WHEEL to {PINNED_PIP_WHEEL}; this interpreter "
            "does not bundle it"
        )
    return bundled.resolve(strict=True)
