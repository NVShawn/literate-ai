from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_operator_adoption``."""

import io


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True
