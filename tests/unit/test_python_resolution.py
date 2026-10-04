from __future__ import annotations

import json
import unittest

from literate_ai.adapters.dependencies import (
    CycloneDxLifecycleResolver,
    DependencyObservationError,
    build_cyclonedx_bom,
)
from literate_ai.adapters.lifecycle.standard_python import (
    StandardPythonDependencyObserver,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxLifecycle,
    canonical_identity,
)
from tests.support import fixtures_test_dependency_lifecycle as lifecycle_fixtures
from tests.support import fixtures_test_standard_python_evidence as retained_fixtures


class PythonResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        retained_fixtures.StandardPythonEvidenceTests.setUpClass()

    def setUp(self):
        self.fixture = retained_fixtures.StandardPythonEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.graph = lifecycle_fixtures._managed_graph()
        self.source = self.fixture.source
        self.evidence = None
        self.calls = 0
        self.components = [
            {
                "type": "library",
                "isExternal": True,
                "bom-ref": "source-" + name,
                "name": name,
                "versionRange": "vers:pypi/>=1.0|<2.0",
                "purl": "pkg:pypi/" + name,
                "properties": [
                    {"name": "literate-ai:dependency-kind", "value": "package"},
                    {"name": "literate-ai:dependency-scope", "value": "runtime"},
                    {"name": "literate-ai:python-top-level-import", "value": name},
                ],
            }
            for name in ("example", "helper")
        ]
        self.edges = (
            (self.graph.root_ref, "source-example"),
            ("source-example", "source-helper"),
        )
        self.path = self.fixture.root / "resolved.json"

    def artifact(self):
        bom, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=self.graph,
            additional_components=self.components,
            additional_edges=self.edges,
            composition_aggregate="complete",
        )
        files = {
            "requirements.txt": "example==1.0\n",
            "python-wheel-lock.json": json.dumps(self.fixture.fixture.fixture.value),
            "main.py": "import example\n",
            CYCLONEDX_SOURCE_SBOM_PATH: bom.decode(),
        }
        return {
            "files": files,
            "effective_revision_digest": canonical_identity({"revision": 1}).uri,
            "source_bundle_digest": canonical_identity(files).uri,
        }

    def observe(self):
        self.calls += 1
        if self.evidence is None:
            self.evidence = self.fixture.retain()
        return StandardPythonDependencyObserver(
            self.fixture.root,
            self.evidence.identity,
            self.fixture.binding,
            self.source,
            self.fixture.fixture.toolchain,
        )()

    def resolver(self, **overrides):
        arguments = {
            "managed_graph": self.graph,
            "observer": lifecycle_fixtures._EmptyObserver(),
            "evidence_path": self.path,
            "python_source_authority": self.source,
            "python_dependency_observer": self.observe,
        }
        return CycloneDxLifecycleResolver(**{**arguments, **overrides})

    def resolve(self, resolver=None):
        artifact = self.artifact()
        return (resolver or self.resolver()).resolve(
            artifact,
            {
                "source_bundle_digest": artifact["source_bundle_digest"],
                "artifact_digest": canonical_identity({"artifact": 1}).uri,
            },
        )

    def test_payload_drift_rejected_on_second_resolution(self):
        self.resolve()
        previous = self.path.read_bytes()
        (self.evidence.payload_root / "site/example/__init__.py").write_bytes(
            b"tampered"
        )
        with self.assertRaises(DependencyObservationError):
            self.resolve()
        self.assertEqual(self.path.read_bytes(), previous)

    def test_ambient_python_observations_cannot_supplement_verified_payloads(self):
        evidence = self.observe()
        with self.assertRaisesRegex(DependencyObservationError, "only from"):
            self.resolve(
                self.resolver(additional_components=evidence.observation.components)
            )
        self.assertFalse(self.path.exists())
