from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_standard_python_evidence``."""

import hashlib
import json
import os
import unittest
from dataclasses import fields, replace
from unittest.mock import patch

from literate_ai.adapters.dependencies.python_source import (
    prepare_python_source_authority,
)
from literate_ai.adapters.dependencies.python_target import observe_python_wheel_target
from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.adapters.lifecycle.standard_python import (
    StandardPythonBuildBinding,
    retain_standard_python_dependencies,
    verify_standard_python_dependencies,
)
from literate_ai.contracts import ContentIdentity, canonical_identity
from tests.support import fixtures_test_python_install as install_fixtures


class StandardPythonEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_fixtures.PythonInstallerIntegrationTests.setUpClass()

    def setUp(self):
        self.fixture = install_fixtures.PythonInstallerIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.fixture.root / "artifact"
        self.root.mkdir()
        self.binding = StandardPythonBuildBinding(
            **{
                field.name: canonical_identity({"field": field.name})
                for field in fields(StandardPythonBuildBinding)
            }
        )
        self.binding = replace(
            self.binding,
            python_toolchain_identity=ContentIdentity.parse_uri(
                self.fixture.toolchain.identity
            ),
        )
        self.source = prepare_python_source_authority(
            {
                "requirements.txt": "example==1.0\n",
                "python-wheel-lock.json": json.dumps(self.fixture.fixture.value),
            },
            manifest_path="requirements.txt",
            lock_path="python-wheel-lock.json",
        )

    def retain(self):
        with self.fixture.install() as installed:
            evidence = retain_standard_python_dependencies(
                self.root, binding=self.binding, source=self.source, installed=installed
            )
            self.installation_path = installed.directory
        self.assertFalse(self.installation_path.exists())
        return evidence

    def verify(self, evidence, **overrides):
        arguments = {
            "expected_identity": evidence.identity,
            "binding": self.binding,
            "source": self.source,
            "target": observe_python_wheel_target(self.fixture.toolchain),
        }
        return verify_standard_python_dependencies(
            self.root, **{**arguments, **overrides}
        )

    def test_retained_cache_survives_installer_context_cleanup(self):
        evidence = self.retain()
        restored = self.verify(evidence)
        self.assertEqual(evidence, restored)
        self.assertEqual(len(restored.observation.components), 2)
        self.assertEqual(restored.payload_root, self.root / "python-runtime")

    def test_changed_cached_payload_rejected(self):
        evidence = self.retain()
        (evidence.payload_root / "site/example/__init__.py").write_bytes(b"changed\n")
        with self.assertRaises(DependencyObservationError):
            self.verify(evidence)

    def test_manifest_cannot_reauthorize_its_own_changed_payload(self):
        evidence = self.retain()
        path = evidence.payload_root / "site/example/__init__.py"
        content = b"changed and rehashed\n"
        path.write_bytes(content)
        manifest = self.root / "python-dependencies.json"
        document = json.loads(manifest.read_bytes())
        for record in document["files"]:
            if record[0] == "site/example/__init__.py":
                record[1:] = [len(content), hashlib.sha256(content).hexdigest()]
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(
            DependencyObservationError, "sealed artifact identity"
        ):
            self.verify(evidence)

    def test_authorization_build_and_source_binding_drift_rejected(self):
        evidence = self.retain()
        for field in fields(self.binding):
            with (
                self.subTest(field=field.name),
                self.assertRaises(DependencyObservationError),
            ):
                self.verify(
                    evidence,
                    binding=replace(
                        self.binding,
                        **{field.name: canonical_identity({"changed": field.name})},
                    ),
                )
        with self.assertRaises(DependencyObservationError):
            self.verify(evidence, source=replace(self.source, manifest_sha256="0" * 64))

    def test_target_identity_drift_rejected(self):
        evidence = self.retain()
        target = replace(
            self.fixture.target, probe_identity=canonical_identity({"changed": "probe"})
        )
        with self.assertRaises(DependencyObservationError):
            self.verify(evidence, target=target)

    def test_additional_runtime_file_rejected(self):
        evidence = self.retain()
        (evidence.payload_root / "site/undeclared.py").write_bytes(b"pass\n")
        with self.assertRaises(DependencyObservationError):
            self.verify(evidence)

    def test_symlink_substitution_rejected(self):
        evidence = self.retain()
        path = evidence.payload_root / "site/example/__init__.py"
        outside = self.fixture.fixture.root / "outside.py"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest("Host does not permit test symlinks")
        with self.assertRaises(DependencyObservationError):
            self.verify(evidence)

    @unittest.skipIf(os.name == "nt", "POSIX execute permissions do not apply")
    def test_executable_mode_drift_rejected(self):
        evidence = self.retain()
        path = evidence.payload_root / "site/example/__init__.py"
        path.chmod(path.stat().st_mode | 0o111)
        with self.assertRaises(DependencyObservationError):
            self.verify(evidence)

    def test_existing_reserved_paths_are_not_replaced(self):
        reserved = self.root / "python-runtime"
        reserved.mkdir()
        marker = reserved / "keep.txt"
        marker.write_bytes(b"existing data")
        with (
            self.fixture.install() as installed,
            self.assertRaises(DependencyObservationError),
        ):
            retain_standard_python_dependencies(
                self.root, binding=self.binding, source=self.source, installed=installed
            )
        self.assertEqual(marker.read_bytes(), b"existing data")

    def test_failed_copy_cleans_only_new_dependency_paths(self):
        keep = self.root / "application.py"
        keep.write_bytes(b"owned by caller")
        with (
            self.fixture.install() as installed,
            patch(
                "literate_ai.adapters.lifecycle.standard_python.shutil.copytree",
                side_effect=OSError("copy failed"),
            ),
            self.assertRaises(OSError),
        ):
            retain_standard_python_dependencies(
                self.root, binding=self.binding, source=self.source, installed=installed
            )
        self.assertEqual(list(self.root.iterdir()), [keep])

    def test_manifest_bound_and_missing_expected_identity(self):
        evidence = self.retain()
        with (
            patch("literate_ai.adapters.lifecycle.standard_python._MAX_MANIFEST", 8),
            self.assertRaises(DependencyObservationError),
        ):
            self.verify(evidence)
        with self.assertRaises(TypeError):
            self.verify(evidence, expected_identity=None)


if __name__ == "__main__":
    unittest.main()
