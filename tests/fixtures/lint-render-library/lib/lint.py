"""Fail closed when the library surface cannot parse as Python."""

from __future__ import annotations

import ast
import pathlib
import sys

failed = False
for path in sorted(pathlib.Path(".").glob("*.py")):
    text = path.read_text(encoding="utf-8")
    try:
        ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        print(exc, file=sys.stderr)
        failed = True
raise SystemExit(1 if failed else 0)
