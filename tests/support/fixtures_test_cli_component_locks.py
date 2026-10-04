from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_cli_component_locks``."""

import io
import json

from literate_ai.cli import main


def _run(arguments: list[str]) -> tuple[int, dict[str, object], str]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    document = json.loads(output.getvalue() or errors.getvalue())
    return status, document, errors.getvalue()
