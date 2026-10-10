"""Locate the pip wheel used by real Python installation tests."""

from __future__ import annotations

import ensurepip
import hashlib
import os
import unittest
from pathlib import Path

from literate_ai.adapters.dependencies.python_install import PIP_INSTALLER

# The Python installation fixtures lock this exact pip wheel's bytes.
PINNED_PIP_WHEEL = PIP_INSTALLER.filename


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pinned_pip_wheel() -> Path:
    """Return LITAI_TEST_PIP_WHEEL, else the interpreter's bundled pinned pip wheel.

    Skips the calling test class when neither is available, for example when the
    interpreter bundles a different pip version, ensurepip is stripped, or a
    distributor repacked the bundled wheel so its bytes differ from the PyPI
    digest the installer lock pins. An explicit LITAI_TEST_PIP_WHEEL is operator
    configuration, so a missing or mismatched explicit wheel fails here. The
    release gate sets LITAI_REQUIRE_PINNED_PIP_WHEEL so these tests cannot pass
    it by skipping.
    """

    value = os.environ.get("LITAI_TEST_PIP_WHEEL")
    if value:
        explicit = Path(value).resolve(strict=True)
        if _sha256(explicit) != PIP_INSTALLER.sha256:
            raise AssertionError(
                f"LITAI_TEST_PIP_WHEEL is not the PyPI {PINNED_PIP_WHEEL} "
                f"(sha256 {PIP_INSTALLER.sha256})"
            )
        return explicit
    unavailable = (
        AssertionError
        if os.environ.get("LITAI_REQUIRE_PINNED_PIP_WHEEL") == "1"
        else unittest.SkipTest
    )
    bundled = Path(ensurepip.__file__).parent / "_bundled" / PINNED_PIP_WHEEL
    if not bundled.is_file():
        raise unavailable(
            f"Set LITAI_TEST_PIP_WHEEL to {PINNED_PIP_WHEEL}; this interpreter "
            "does not bundle it"
        )
    if _sha256(bundled) != PIP_INSTALLER.sha256:
        raise unavailable(
            f"Set LITAI_TEST_PIP_WHEEL to the PyPI {PINNED_PIP_WHEEL} "
            f"(sha256 {PIP_INSTALLER.sha256}); this interpreter bundles "
            "different bytes under that name"
        )
    return bundled.resolve(strict=True)
