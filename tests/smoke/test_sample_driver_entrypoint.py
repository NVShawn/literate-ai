"""Production sample-driver import boundary regressions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


class SampleDriverEntrypointTests(unittest.TestCase):
    def test_test_support_import_restores_exact_production_environment(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        names = ("LITAI_CONFIG_DIR", "LITAI_MCP_TRANSPORT")
        probe = (
            "import json, os\n"
            "from scripts.run_samples import _sample_main\n"
            "_sample_main()\n"
            f"print(json.dumps({{name: os.environ.get(name) for name in {names!r}}}, "
            "sort_keys=True))\n"
        )
        cases = (
            {},
            {
                "LITAI_CONFIG_DIR": str(repository / "operator-config"),
                "LITAI_MCP_TRANSPORT": "configured-transport",
            },
        )
        for configured in cases:
            with self.subTest(configured=bool(configured)):
                environment = dict(os.environ)
                for name in names:
                    environment.pop(name, None)
                environment.update(configured)

                completed = subprocess.run(
                    [sys.executable, "-c", probe],
                    cwd=repository,
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    json.loads(completed.stdout),
                    {name: configured.get(name) for name in names},
                )
