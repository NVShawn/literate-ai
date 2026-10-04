"""Locate the pip wheel used by real Python installation tests."""

from __future__ import annotations

import ensurepip
import os
import unittest
from pathlib import Path


def pinned_pip_wheel() -> Path:
    """Return LITAI_TEST_PIP_WHEEL, else the interpreter's bundled ensurepip wheel.

    Skips the calling test class when neither exists, for example on a distribution
    that strips ensurepip.
    """

    value = os.environ.get("LITAI_TEST_PIP_WHEEL")
    if value:
        return Path(value).resolve(strict=True)
    bundled = sorted((Path(ensurepip.__file__).parent / "_bundled").glob("pip-*.whl"))
    if not bundled:
        raise unittest.SkipTest(
            "Set LITAI_TEST_PIP_WHEEL; this interpreter bundles no pip wheel"
        )
    return bundled[-1].resolve(strict=True)
