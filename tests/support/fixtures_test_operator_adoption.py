"""Shared test fixtures extracted from test_operator_adoption."""

from __future__ import annotations

import io


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True
