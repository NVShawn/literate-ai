"""Skip symlink-refusal fixtures on hosts that cannot follow symbolic links."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

# ERROR_SYMLINK_CLASS_DISABLED: Windows refuses to follow the link because the
# host's symbolic-link evaluation policy disables that link class. Seen for plain
# local links in Windows OpenSSH sessions even when local-to-local evaluation is on.
_WINDOWS_SYMLINK_CLASS_DISABLED = 1463


def symlinks_followable(directory: Path) -> bool:
    """Whether a fresh file symlink created in ``directory`` can be followed."""

    with tempfile.TemporaryDirectory(dir=directory) as raw:
        probe = Path(raw)
        target = probe / "target"
        target.write_bytes(b"")
        link = probe / "link"
        try:
            link.symlink_to(target)
        except OSError:
            return False
        try:
            link.stat()
        except OSError as exc:
            if getattr(exc, "winerror", None) == _WINDOWS_SYMLINK_CLASS_DISABLED:
                return False
            raise
        return True


def skip_unless_symlinks_followable(test: unittest.TestCase, directory: Path) -> None:
    """Skip ``test`` when symlinks under ``directory`` cannot be created or followed."""

    if not symlinks_followable(directory):
        test.skipTest("host cannot create or follow symbolic links")
