from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.cli.dispatch import main


class TargetMatrixCliTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        status = main(arguments, stdout=stdout, stderr=stderr)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_help_exposes_declaration_concurrency_and_host_acknowledgement(
        self,
    ) -> None:
        status, stdout, stderr = self.invoke("matrix", "help")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("--evidence-root", stdout)
        self.assertIn("--jobs", stdout)
        self.assertIn("--allow-host-execution", stdout)

    def test_invalid_target_is_rejected_before_lifecycle_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            declaration = root / "matrix.json"
            declaration.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/target-matrix@1",
                        "cells": [
                            {
                                "schema": "literate-ai/target-matrix-cell@1",
                                "component": "components/example",
                                "target": "../host",
                                "flavors": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            status, stdout, stderr = self.invoke(
                "--json",
                "matrix",
                str(declaration),
                "--project",
                str(root),
                "--evidence-root",
                str(root / "evidence"),
            )
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"]["code"], "matrix.target_unsafe")


if __name__ == "__main__":
    unittest.main()
