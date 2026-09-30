"""Command-boundary regression tests for the Standard service-stack sample."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.conformance.support.standard_service_stack import (
    _ARCHIVE_RUNNER,
    _SOURCE_RUNNER,
)


def _encoded(value: object) -> str:
    document = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "litai-b64:" + base64.urlsafe_b64encode(document).decode("ascii")


class StandardServiceStackCommandTests(unittest.TestCase):
    def test_packaged_acceptance_argument_overrides_default_invocation(self) -> None:
        default = [{"source": "default"}]
        verifier = [{"source": "independent-verifier"}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            application = root / "application.py"
            application.write_text(
                "import json,sys\n"
                "print(json.dumps(json.loads(sys.argv[1]),sort_keys=True))\n",
                encoding="utf-8",
            )
            for runner, prefix in (
                (_ARCHIVE_RUNNER, (str(root), application.name)),
                (_SOURCE_RUNNER, (str(root), ".", application.name)),
            ):
                with self.subTest(runner=runner):
                    ordinary = subprocess.run(
                        (sys.executable, "-c", runner, *prefix, _encoded(default)),
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    independent = subprocess.run(
                        (
                            sys.executable,
                            "-c",
                            runner,
                            *prefix,
                            _encoded(default),
                            json.dumps(verifier, separators=(",", ":")),
                        ),
                        check=True,
                        capture_output=True,
                        text=True,
                    )

                    self.assertEqual(json.loads(ordinary.stdout), default)
                    self.assertEqual(json.loads(independent.stdout), verifier)


if __name__ == "__main__":
    unittest.main()
