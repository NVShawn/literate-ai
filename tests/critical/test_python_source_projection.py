"""A target projection preserves declarations without inventing SDK payloads."""

import copy
import hashlib
import json
import unittest

from packaging.markers import default_environment

from literate_ai.adapters.dependencies.admission import reconcile_generated_dependencies
from literate_ai.adapters.dependencies.python_source import (
    prepare_python_source_authority,
)
from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.contracts import StandardPythonWheelCommandProfile


class PythonSourceProjectionTests(unittest.TestCase):
    def setUp(self):
        environment = default_environment()
        environment.update(
            sys_platform="linux", platform_system="Linux", os_name="posix"
        )
        self.lock = {
            "schema": "literate-ai/python-wheel-lock@1",
            "environment": environment,
            "tags": ["py3-none-any"],
            "requirements": ['darwin-sdk==1.0; sys_platform == "darwin"'],
            "packages": [],
        }
        self.backend = "source/backends/darwin.py"
        self.projection_path = "source/python-source-projection.json"
        self.files = {
            "source/requirements.txt": self.lock["requirements"][0] + "\n",
            "source/python-wheel-lock.json": json.dumps(self.lock),
            "source/main.py": (
                "import sys\nif sys.platform == 'darwin':\n"
                " from backends.darwin import run\nelse:\n"
                " from backends.linux import run\n"
            ),
            self.backend: "import darwin_sdk\ndef run(): return darwin_sdk.run()\r\n",
            "source/backends/linux.py": "def run(): return 'linux'\n",
        }
        self.document = {
            "schema": "literate-ai/python-source-projection@1",
            "environment": environment,
            "tags": self.lock["tags"],
            "excluded": [
                {
                    "path": self.backend,
                    "sha256": hashlib.sha256(
                        self.files[self.backend].encode()
                    ).hexdigest(),
                    "reason": "Darwin-only SDK backend is absent from this Linux build",
                }
            ],
        }
        self.write_projection()

    def write_projection(self):
        self.files[self.projection_path] = json.dumps(self.document)

    def authority(self, **overrides):
        return prepare_python_source_authority(
            self.files,
            **{
                "manifest_path": "source/requirements.txt",
                "lock_path": "source/python-wheel-lock.json",
                "projection_path": self.projection_path,
                **overrides,
            },
        )

    def test_linux_projection_preserves_full_declaration_and_exact_backend_bytes(self):
        before = copy.deepcopy(self.files)
        source = self.authority()
        reconcile_generated_dependencies(
            self.files,
            b'{"components": []}',
            python_source_authority=source,
            observed_python_imports=frozenset(),
        )
        self.assertEqual(self.files, before)
        self.assertEqual(source.lock.requirements, tuple(self.lock["requirements"]))
        self.assertEqual(source.projection.excluded_paths, (self.backend,))

    def test_active_missing_import_is_not_authorized_by_an_inactive_requirement(self):
        self.files["source/main.py"] += "import darwin_sdk\n"
        with self.assertRaisesRegex(
            DependencyObservationError, "installed payload evidence"
        ):
            reconcile_generated_dependencies(
                self.files,
                b'{"components": []}',
                python_source_authority=self.authority(),
                observed_python_imports=frozenset(),
            )

    def test_projected_static_pyproject_and_requirements_in_preserve_markers(self):
        original = self.files.pop("source/requirements.txt")
        for name in ("requirements.in", "pyproject.toml"):
            with self.subTest(manifest=name):
                path = "source/" + name
                self.files[path] = (
                    "[project]\nname='projected'\nrequires-python='>=3.11'\n"
                    + "dependencies="
                    + json.dumps(self.lock["requirements"])
                    + "\n"
                    if name == "pyproject.toml"
                    else original
                )
                try:
                    source = self.authority(manifest_path=path)
                    reconcile_generated_dependencies(
                        self.files,
                        b'{"components": []}',
                        python_source_authority=source,
                        observed_python_imports=frozenset(),
                    )
                    self.assertIn("darwin-sdk", self.files[path])
                finally:
                    self.files.pop(path)

    def test_duplicate_json_fields_and_oversized_projection_refuse(self):
        for content in (
            self.files[self.projection_path].replace(
                '"excluded":', '"excluded": [], "excluded":', 1
            ),
            " " * (1024 * 1024 + 1),
        ):
            with self.subTest(size=len(content)):
                self.files[self.projection_path] = content
                with self.assertRaises(DependencyObservationError):
                    self.authority()

    def test_projection_requires_explicit_profile_selection(self):
        with self.assertRaisesRegex(DependencyObservationError, "unselected"):
            self.authority(projection_path=None)
        self.files.pop(self.projection_path)
        source = self.authority(projection_path=None)
        with self.assertRaisesRegex(DependencyObservationError, "absent from"):
            reconcile_generated_dependencies(
                self.files,
                b'{"components": []}',
                python_source_authority=source,
            )

    def test_target_environment_or_tags_drift_cannot_reuse_the_projection(self):
        for field, value in (
            ("platform_machine", "another-architecture"),
            ("tags", ["py2-none-any", "py3-none-any"]),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.lock)
                if field == "tags":
                    changed["tags"] = value
                else:
                    changed["environment"][field] = value
                self.files["source/python-wheel-lock.json"] = json.dumps(changed)
                with self.assertRaisesRegex(
                    DependencyObservationError, "exact wheel-lock"
                ):
                    self.authority()

    def test_stale_backend_bytes_or_unknown_manifest_fields_refuse(self):
        original = self.files[self.backend]
        self.files[self.backend] = original.replace("\r\n", "\n")
        with self.assertRaisesRegex(DependencyObservationError, "reviewed bytes"):
            self.authority()
        self.files[self.backend] = original
        self.document["override"] = True
        self.write_projection()
        with self.assertRaises(DependencyObservationError):
            self.authority()

    def test_unsafe_duplicate_missing_and_non_python_exclusions_refuse(self):
        original = copy.deepcopy(self.document["excluded"])
        for filename in (
            "../escape.py",
            "/absolute.py",
            "source/../escape.py",
            "source\\escape.py",
            "elsewhere/backend.py",
            "source/requirements.txt",
            "source/missing.py",
        ):
            with self.subTest(filename=filename):
                self.document["excluded"] = [{**original[0], "path": filename}]
                self.write_projection()
                with self.assertRaises(DependencyObservationError):
                    self.authority()
        self.document["excluded"] = original * 2
        self.write_projection()
        with self.assertRaisesRegex(DependencyObservationError, "unique"):
            self.authority()

    def test_locked_profile_binds_selection_and_preserves_legacy_identity(self):
        ordinary = StandardPythonWheelCommandProfile(
            "pip", "python", "source/requirements.txt", "source/python-wheel-lock.json"
        )
        projected = StandardPythonWheelCommandProfile.from_dict(
            {**ordinary.to_dict(), "source_projection": self.projection_path}
        )
        self.assertNotEqual(ordinary.identity, projected.identity)
        self.assertNotIn("source_projection", ordinary.to_dict())
        self.assertEqual(projected.source_projection, self.projection_path)
        for path in (
            "other/python-source-projection.json",
            "source/other.json",
            "../python-source-projection.json",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                StandardPythonWheelCommandProfile.from_dict(
                    {**ordinary.to_dict(), "source_projection": path}
                )
