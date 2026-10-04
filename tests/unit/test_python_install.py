from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.dependencies.python_install import install_python_wheels
from literate_ai.adapters.dependencies.python_lock import parse_python_wheel_lock
from literate_ai.adapters.dependencies.python_target import observe_python_wheel_target
from literate_ai.adapters.dependencies.types import DependencyObservationError
from tests.support import fixtures_test_python_installed as fixtures


class PythonInstallerIntegrationTests(unittest.TestCase):
    """Requires the exact external installer wheel, not network during tests."""

    @classmethod
    def setUpClass(cls):
        value = os.environ.get("LITAI_TEST_PIP_WHEEL")
        if not value:
            raise unittest.SkipTest("Set LITAI_TEST_PIP_WHEEL to the pinned pip wheel")
        cls.installer_path = Path(value).resolve(strict=True)
        cls.toolchain = discover_python_toolchain(pinned_command=sys.executable)
        cls.target = observe_python_wheel_target(cls.toolchain)

    def setUp(self):
        self.fixture = fixtures.PythonInstalledTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.value.update(
            environment=dict(self.target.environment), tags=list(self.target.tags)
        )

    @contextmanager
    def install(self, installer_source=None):
        streams = {
            name: io.BytesIO(content) for name, content in self.fixture.sources.items()
        }
        with self.installer_path.open("rb") as installer:
            with install_python_wheels(
                self.toolchain,
                parse_python_wheel_lock(json.dumps(self.fixture.value)),
                streams,
                installer_source=installer_source or installer,
                temporary_root=self.fixture.root,
            ) as result:
                yield result

    def test_actual_offline_install_and_cleanup(self):
        with self.install() as result:
            path = result.directory
            self.assertEqual(len(result.observation.graph.components), 2)
            self.assertEqual(result.target.identity, self.target.identity)
            self.assertTrue(result.installer_identity.uri.startswith("sha256:"))
            result.revalidate()
        self.assertFalse(path.exists())

    def test_installed_wrapper_tampering_is_rejected(self):
        self.fixture.replace_wheel_files(
            "example",
            {
                "example-1.0.dist-info/entry_points.txt": (
                    b"[console_scripts]\nexample-cli = example:main\n"
                )
            },
        )
        with self.install() as result:
            wrapper = result.changes["example"].generated[0]
            (result.directory / wrapper.path).write_bytes(b"altered wrapper")
            with self.assertRaises(DependencyObservationError):
                result.revalidate()

    def test_exact_installer_hash_is_required(self):
        with (
            self.assertRaises(DependencyObservationError),
            self.install(io.BytesIO(b"not the pinned pip wheel")),
        ):
            self.fail("Wrong installer must not run")
        self.assertEqual(
            sorted(p.name for p in self.fixture.root.iterdir()), ["installed"]
        )


if __name__ == "__main__":
    unittest.main()
