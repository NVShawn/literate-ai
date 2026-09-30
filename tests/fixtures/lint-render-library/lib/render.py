"""Emit the library's exact HTML snapshot on stdout."""

from __future__ import annotations

import sys

from surface import render

sys.stdout.buffer.write(render().encode("utf-8"))
