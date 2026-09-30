"""The multi-output build connector is portable outside the framework runtime."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from literate_ai.adapters.multi_entrypoint_build import standalone_driver_source


def _encoded(value: object) -> str:
    return base64.urlsafe_b64encode(
        zlib.compress(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    ).decode("ascii")


class MultiEntrypointBuildTests(unittest.TestCase):
    def test_standalone_driver_runs_under_isolated_host_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "candidate"
            source.joinpath("source").mkdir(parents=True)
            source.joinpath("source", "main.py").write_text(
                "print('primary')\n", encoding="utf-8"
            )
            source.joinpath("source", "inspect.py").write_text(
                "print('inspect')\n", encoding="utf-8"
            )
            objects = root / "objects"
            artifact = root / "artifact"
            artifact.mkdir()
            primary = artifact / "primary"
            completed = subprocess.run(
                (
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    standalone_driver_source(),
                    "python-tree",
                    json.dumps([sys.executable]),
                    _encoded([]),
                    str(source),
                    str(objects),
                    str(artifact),
                    str(primary),
                    _encoded(
                        [
                            {"source": "source/main.py", "export_id": "primary"},
                            {
                                "source": "source/inspect.py",
                                "export_id": "inspector",
                            },
                        ]
                    ),
                ),
                cwd=artifact,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                primary.joinpath("source", "main.py").read_text(encoding="utf-8"),
                "print('primary')\n",
            )
            self.assertEqual(
                artifact.joinpath("inspector", "source", "inspect.py").read_text(
                    encoding="utf-8"
                ),
                "print('inspect')\n",
            )


if __name__ == "__main__":
    unittest.main()
