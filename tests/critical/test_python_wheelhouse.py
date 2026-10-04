from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from packaging.markers import default_environment

from literate_ai.adapters.dependencies.python_lock import parse_python_wheel_lock
from literate_ai.adapters.dependencies.python_wheelhouse import stage_python_wheels
from literate_ai.adapters.dependencies.types import DependencyObservationError
from tests.support.fixtures_test_python_wheel_lock import document, wheel_record


class PythonWheelhouseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lock = parse_python_wheel_lock(json.dumps(document()))
        self.sources = {
            "example": io.BytesIO(wheel_record(dependencies=("helper>=1",))[1]),
            "helper": io.BytesIO(wheel_record("helper")[1]),
        }

    def stage(self, **overrides):
        options = {
            "environment": default_environment(),
            "tags": ("py3-none-any",),
            "temporary_root": self.root,
        }
        return stage_python_wheels(self.lock, self.sources, **{**options, **overrides})

    def test_changed_or_extra_staged_bytes_rejected(self):
        for action in ("overwrite", "extra", "remove"):
            with self.subTest(action=action), self.stage() as staged:
                path = staged.directory / self.lock.packages[0].filename
                if action == "overwrite":
                    path.write_bytes(b"modified cache")
                elif action == "extra":
                    (staged.directory / "unlocked.whl").write_bytes(b"extra")
                else:
                    path.unlink()
                with self.assertRaises(DependencyObservationError):
                    staged.revalidate()

    def test_symlink_or_hardlink_alias_rejected(self):
        for alias_kind in ("symlink", "hardlink"):
            with self.subTest(alias=alias_kind), self.stage() as staged:
                path = staged.directory / self.lock.packages[0].filename
                external = self.root / f"{alias_kind}.whl"
                try:
                    if alias_kind == "symlink":
                        external.write_bytes(path.read_bytes())
                        path.unlink()
                        path.symlink_to(external)
                    else:
                        os.link(path, external)
                except OSError:
                    self.skipTest(
                        f"Host does not permit creation of test {alias_kind}s"
                    )
                with self.assertRaises(DependencyObservationError):
                    staged.revalidate()


if __name__ == "__main__":
    unittest.main()
