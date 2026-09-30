"""StandardCommandProjectionError must reach the typed CLI envelope (#33).

Every one of standard_project.py's ~10 raise sites for this error -- not
just the one for a Component declaring more than one entrypoint -- reached
no except clause anywhere in the codebase, so it propagated out of every
CLI command as an uncaught Python traceback instead of the
{"command":..., "error": {"code":..., "message":...}, "ok": false, ...}
envelope every other lifecycle failure produces.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from literate_ai.adapters.cache import SourceCacheError
from literate_ai.adapters.standard_project import StandardCommandProjectionError
from literate_ai.cli import main


def invoke(*arguments: str) -> tuple[int, str, str]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    return status, output.getvalue(), errors.getvalue()


class StandardCommandProjectionErrorEnvelopeTests(unittest.TestCase):
    def test_main_converts_it_to_the_typed_json_envelope(self) -> None:
        with patch(
            "literate_ai.cli.dispatch._handle",
            side_effect=StandardCommandProjectionError(
                "standard_command.multiple_entrypoints",
                "each Standard executable Component must declare exactly one "
                "entrypoint",
            ),
        ):
            status, output, errors = invoke("version", "check", "--json")

        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        envelope = json.loads(errors)
        self.assertFalse(envelope["ok"])
        self.assertEqual(
            envelope["error"]["code"], "standard_command.multiple_entrypoints"
        )
        self.assertEqual(
            envelope["error"]["message"],
            "each Standard executable Component must declare exactly one entrypoint",
        )


class SourceCacheErrorEnvelopeTests(unittest.TestCase):
    """Same class of bug (#54): SourceCacheError reached no CLI-level catch."""

    def test_main_converts_it_to_the_typed_json_envelope(self) -> None:
        with patch(
            "literate_ai.cli.dispatch._handle",
            side_effect=SourceCacheError(
                "source-cache.ambiguous",
                "exact cache key has multiple accepted entries; select one identity",
            ),
        ):
            status, output, errors = invoke("version", "check", "--json")

        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        envelope = json.loads(errors)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "source-cache.ambiguous")


if __name__ == "__main__":
    unittest.main()
