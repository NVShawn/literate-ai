"""Target projections pass real offline Standard execution and artifact custody."""

import hashlib
import json
import shutil
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from tests.smoke import test_standard_python_lifecycle as fixtures


class StandardPythonSourceProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.StandardPythonLifecycleTests.setUpClass()

    def fixture(self, *, library=True):
        base = (
            fixtures.StandardPythonLibraryLifecycleTests
            if library
            else fixtures.StandardPythonLifecycleTests
        )

        class Fixture(base):
            source_projection = True

            def _admit_source(self):
                if not getattr(self, "projection_prepared", False):
                    self.projection_prepared = True
                    _prepare_source(self)
                return super()._admit_source()

        fixture = Fixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_application_build_test_execution_and_cache_reuse_use_projection(self):
        fixture = self.fixture(library=False)
        before = fixture.source.joinpath("source/backends/foreign.py").read_bytes()
        fixture.test_authorized_offline_build_test_launch_and_cache_reuse()
        self.assertEqual(
            fixture.source.joinpath("source/backends/foreign.py").read_bytes(), before
        )
        export = fixture.ports.artifact_path(fixture.build().exports[0])
        self.assertFalse((export / "source/backends/foreign.py").exists())
        self.assertIn("platform-sdk", (export / "source/requirements.txt").read_text())

    def test_library_independent_acceptance_reopens_sealed_projection_after_relocation(
        self,
    ):
        fixture = self.fixture()
        output = fixture.build()
        fixture.ports.test(fixture.plan, output.exports)
        fixture.ports.execute(fixture.plan, output.exports)
        plan, result, custody = fixture.package(output)
        self.assertIsNotNone(fixture.accept(plan, result))
        old_root = custody.root
        relocated = old_root.with_name("relocated target projection")
        old_root.rename(relocated)
        custody = replace(
            custody,
            root=relocated,
            artifact_paths={
                key: relocated / path.relative_to(old_root)
                for key, path in custody.artifact_paths.items()
            },
        )
        fixture.ports._project_packages[result.identity.uri] = custody
        export = custody.artifact_paths[output.exports[0].identity.uri]
        self.assertFalse((export / "source/backends/foreign.py").exists())
        self.assertIn("platform-sdk", (export / "source/requirements.txt").read_text())
        evidence = json.loads((export.parent / "python-dependencies.json").read_bytes())
        self.assertEqual(
            evidence["source"]["projection_path"],
            "source/python-source-projection.json",
        )
        projection = export / "source/python-source-projection.json"
        self.assertEqual(
            hashlib.sha256(projection.read_bytes()).hexdigest(),
            evidence["source"]["projection_sha256"],
        )
        shutil.rmtree(fixture.artifact)
        shutil.rmtree(fixture.source)
        shutil.rmtree(fixture.wheelhouse)
        self.assertIsNotNone(fixture.accept(plan, result))
        original = projection.read_bytes()
        projection.write_bytes(original + b" ")
        with self.assertRaises(LocalStandardLifecycleError):
            fixture.accept(plan, result)

    def test_changed_excluded_source_refuses_before_build(self):
        fixture = self.fixture()
        with (fixture.source / "source/backends/foreign.py").open("ab") as stream:
            stream.write(b"# changed\n")
        with self.assertRaisesRegex(
            (DependencyObservationError, LocalStandardLifecycleError), "reviewed bytes"
        ):
            fixture._admit_source()
        self.assertEqual(fixture.ports.build_cache_misses, 0)

    def test_missing_active_import_still_refuses_before_build(self):
        fixture = self.fixture()
        with (fixture.source / "source/main.py").open("a") as stream:
            stream.write("import platform_sdk\n")
        with self.assertRaisesRegex(
            (DependencyObservationError, LocalStandardLifecycleError), "absent from"
        ):
            fixture._admit_source()
        self.assertEqual(fixture.ports.build_cache_misses, 0)


def _prepare_source(fixture):
    foreign = "linux" if sys.platform == "darwin" else "darwin"
    requirement = f'platform-sdk==1.0; sys_platform == "{foreign}"'
    lock = fixture.wheels.fixture.value
    lock["requirements"].append(requirement)
    (fixture.source / "source/python-wheel-lock.json").write_text(json.dumps(lock))
    (fixture.source / "source/requirements.txt").write_text(
        "\n".join(lock["requirements"]) + "\n"
    )
    backends = fixture.source / "source/backends"
    backends.mkdir()
    (backends / "generic.py").write_text(
        "import helper\ndef value(): return helper.VALUE\n"
    )
    excluded = backends / "foreign.py"
    excluded.write_bytes(
        b"import platform_sdk\r\ndef value(): return platform_sdk.VALUE\r\n"
    )
    prefix = (
        f"import sys\nif sys.platform == {foreign!r}:\n"
        " from backends.foreign import value as backend_value\nelse:\n"
        " from backends.generic import value as backend_value\n"
        "assert backend_value() == 1\n"
    )
    active = [fixture.source / "source/main.py"]
    if fixture.library:
        capability = fixture.closure.contracts[0].library_import_surface.capabilities[0]
        active.append(
            (
                fixture.source / "source" / Path(*capability.module.split("."))
            ).with_suffix(".py")
        )
    for path in active:
        path.write_text(prefix + path.read_text())
    (fixture.source / "source/python-source-projection.json").write_text(
        json.dumps(
            {
                "schema": "literate-ai/python-source-projection@1",
                "environment": lock["environment"],
                "tags": lock["tags"],
                "excluded": [
                    {
                        "path": "source/backends/foreign.py",
                        "sha256": hashlib.sha256(excluded.read_bytes()).hexdigest(),
                        "reason": "Backend requires the foreign-platform SDK",
                    }
                ],
            }
        )
    )
