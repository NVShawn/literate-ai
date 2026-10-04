from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import fields, replace

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


if __name__ == "__main__":
    unittest.main()
