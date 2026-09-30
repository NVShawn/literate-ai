from __future__ import annotations

import hashlib
import json
import unittest

from literate_ai.adapters.dependencies.acquisition import _generated_lock_projection
from literate_ai.adapters.dependencies.python_source import (
    prepare_python_source_authority,
)
from literate_ai.adapters.dependencies.types import DependencyObservationError
from tests.unit.test_python_wheel_lock import document


class PythonSourceAuthorityTests(unittest.TestCase):
    def prepare(
        self,
        content="example==1.0\n",
        *,
        name="requirements.txt",
        extra=None,
        lock=None,
    ):
        files = {
            name: content,
            "python-wheel-lock.json": json.dumps(document() if lock is None else lock),
            "main.py": "import example\n",
            **(extra or {}),
        }
        return prepare_python_source_authority(
            files, manifest_path=name, lock_path="python-wheel-lock.json"
        )

    def test_requirements_match_with_comments_and_normalized_names(self):
        content = "# runtime\n\nEXAMPLE == 1.0 # ordinary comment\n"
        authority = self.prepare(content)
        self.assertEqual(
            authority.manifest_sha256, hashlib.sha256(content.encode()).hexdigest()
        )
        self.assertEqual(
            authority.lock.edges, (("@root", "example"), ("example", "helper"))
        )
        self.assertEqual(
            authority.lock_sha256,
            hashlib.sha256(json.dumps(document()).encode()).hexdigest(),
        )

    def test_pyproject_matches_and_checks_interpreter(self):
        self.prepare(
            '[project]\nname="application"\nrequires-python=">=3.11"\ndependencies=["example==1.0"]\n',
            name="pyproject.toml",
        )
        with self.assertRaises(DependencyObservationError):
            self.prepare(
                '[project]\nrequires-python=">=99"\ndependencies=["example==1.0"]',
                name="pyproject.toml",
            )

    def test_extra_or_missing_root_is_not_justified_by_transitive_membership(self):
        for content in (
            "",
            "helper==1.0\n",
            "example==1.0\nhelper==1.0\n",
            "example>=1.0\n",
        ):
            with (
                self.subTest(content=content),
                self.assertRaises(DependencyObservationError) as caught,
            ):
                self.prepare(content)
            self.assertEqual(
                caught.exception.code, "dependencies.python-source-lock-mismatch"
            )

    def test_inactive_root_marker_still_must_be_declared(self):
        lock = document()
        lock["requirements"] = ['absent; sys_platform == "never"', "example==1.0"]
        with self.assertRaises(DependencyObservationError):
            self.prepare(lock=lock)
        self.prepare('absent; sys_platform == "never"\nexample==1.0', lock=lock)

    def test_pip_configuration_and_includes_are_not_silently_ignored(self):
        for line in (
            "-r other.txt",
            "-c constraints.txt",
            "--index-url https://example.invalid",
            "-e .",
            "example @ https://example.invalid/example.whl",
            "example==1.0 --hash=sha256:123",
            "example==1.0 \\",
        ):
            with self.subTest(line=line), self.assertRaises(DependencyObservationError):
                self.prepare(line)

    def test_extra_source_authorities_are_rejected_even_if_empty(self):
        for name in (
            "nested/requirements.txt",
            "requirements.in",
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "uv.lock",
            "poetry.lock",
            "Pipfile",
            "Pipfile.lock",
            "pylock.toml",
            "pip.conf",
            "pip.ini",
            "nested/python-wheel-lock.json",
            "REQUIREMENTS.TXT",
            "nested\\requirements.txt",
            "pylock.windows.toml",
        ):
            with self.subTest(name=name), self.assertRaises(DependencyObservationError):
                self.prepare(extra={name: ""})

    def test_pyproject_unsupported_authority_is_rejected(self):
        for content in (
            '[project]\ndependencies=["example==1.0"]\n[build-system]\nrequires=["setuptools"]',
            '[project]\ndynamic=["dependencies"]',
            '[project]\ndependencies=["example==1.0"]\n[project.optional-dependencies]\ntest=["helper"]',
            '[project]\ndependencies=["example==1.0"]\n[dependency-groups]\ntest=["helper"]',
            '[project]\ndependencies=["example==1.0"]\n[tool.uv]',
            '[project]\ndependencies=["example==1.0"]\n[tool.poetry]',
            '[project]\ndependencies=["example==1.0"]\n[tool.pdm]',
        ):
            with (
                self.subTest(content=content),
                self.assertRaises(DependencyObservationError),
            ):
                self.prepare(content, name="pyproject.toml")

    def test_pyproject_malformed_shapes_are_rejected(self):
        for content in (
            "not toml",
            "",
            "project=1",
            "[project]\ndependencies=1",
            "[project]\ndependencies=[1]",
            "[project]\nrequires-python=1",
            "[project]\nrequires-python='invalid'",
            "[project]\ndynamic=1",
            "[project]\ndynamic=[1]",
            "[project]\noptional-dependencies=[]",
        ):
            with (
                self.subTest(content=content),
                self.assertRaises(DependencyObservationError),
            ):
                self.prepare(content, name="pyproject.toml")

    def test_duplicate_normalized_roots_are_rejected(self):
        with self.assertRaises(DependencyObservationError):
            self.prepare("example==1.0\nEXAMPLE == 1.0\n")

    def test_nontext_authority_is_rejected(self):
        for content in (None, b"example==1.0", 3):
            with (
                self.subTest(content=content),
                self.assertRaises(DependencyObservationError),
            ):
                self.prepare(content)

    def test_canonical_paths_and_single_root_are_required(self):
        for manifest, lock in (
            ("../requirements.txt", "../python-wheel-lock.json"),
            ("/requirements.txt", "/python-wheel-lock.json"),
            ("./requirements.txt", "python-wheel-lock.json"),
            ("requirements.txt", "nested/python-wheel-lock.json"),
            ("a\\requirements.txt", "a\\python-wheel-lock.json"),
            ("C:/requirements.txt", "C:/python-wheel-lock.json"),
        ):
            with (
                self.subTest(manifest=manifest),
                self.assertRaises(DependencyObservationError),
            ):
                prepare_python_source_authority(
                    {}, manifest_path=manifest, lock_path=lock
                )

    def test_nested_selected_root_is_supported(self):
        authority = prepare_python_source_authority(
            {
                "app/requirements.txt": "example==1.0",
                "app/python-wheel-lock.json": json.dumps(document()),
            },
            manifest_path="app/requirements.txt",
            lock_path="app/python-wheel-lock.json",
        )
        self.assertEqual(authority.manifest_path, "app/requirements.txt")

    def test_standard_source_preflight_uses_manifest_reconciliation(self):
        files = {
            "requirements.txt": "helper==1.0",
            "python-wheel-lock.json": json.dumps(document()),
        }
        with self.assertRaises(DependencyObservationError) as caught:
            _generated_lock_projection(files)
        self.assertEqual(
            caught.exception.code, "dependencies.python-source-lock-mismatch"
        )
        files["requirements.txt"] = "example==1.0"
        with self.assertRaises(DependencyObservationError) as caught:
            _generated_lock_projection(files)
        self.assertEqual(
            caught.exception.code, "dependencies.python-acquisition-evidence-missing"
        )

    def test_standard_preflight_rejects_missing_or_ambiguous_authority(self):
        for extra in (
            {},
            {"requirements.txt": "example==1.0", "requirements.in": "example==1.0"},
            {"nested/python-wheel-lock.json": "{}"},
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaises(DependencyObservationError) as caught,
            ):
                _generated_lock_projection(
                    {"python-wheel-lock.json": json.dumps(document()), **extra}
                )
            self.assertEqual(
                caught.exception.code, "dependencies.python-source-invalid"
            )


if __name__ == "__main__":
    unittest.main()
