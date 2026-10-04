"""CLI coverage for read-only SCXML structural and trace review."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from literate_ai.cli.dispatch import main


class ScxmlReviewCliTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--json", *arguments])
        content = stdout.getvalue() if status == 0 else stderr.getvalue()
        return status, json.loads(content)

    def test_review_validates_exact_chart_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "simple.scxml"
            chart.write_text(
                '<scxml xmlns="http://www.w3.org/2005/07/scxml" '
                'version="1.0" initial="ready"><state id="ready"/></scxml>',
                encoding="utf-8",
            )
            before = chart.read_bytes()

            status, envelope = self.invoke("spec", "scxml-review", str(chart))

            self.assertEqual(status, 0, envelope)
            result = envelope["result"]
            assert isinstance(result, dict)
            self.assertEqual(result["state"], "passed")
            self.assertEqual(result["provider"], "specification-provider:scxml@1")
            self.assertEqual(chart.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
