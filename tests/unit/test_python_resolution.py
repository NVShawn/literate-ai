from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace

from literate_ai.adapters.dependencies import (
    CycloneDxLifecycleResolver,
    DependencyObservationError,
    HostDependencyObservation,
    build_cyclonedx_bom,
)
from literate_ai.adapters.lifecycle.standard_python import (
    StandardPythonDependencyObserver,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    canonical_identity,
)
from literate_ai.ports.contracts import (
    PortContractError,
    require_dependency_resolution_result,
    require_dependency_source_validation_result,
)
from tests.unit import test_dependency_lifecycle as lifecycle_fixtures
from tests.unit import test_standard_python_evidence as retained_fixtures


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

    def test_retained_installation_resolves_source_refs_and_exact_versions(self):
        result = self.resolve()
        self.assertEqual(self.calls, 1)
        self.assertEqual(
            result["python_dependency_evidence_identity"], self.evidence.identity.uri
        )
        document = json.loads(self.path.read_bytes())
        packages = [
            item
            for item in document["components"]
            if item.get("purl", "").startswith("pkg:pypi/")
        ]
        self.assertEqual(
            {item["bom-ref"] for item in packages}, {"source-example", "source-helper"}
        )
        self.assertTrue(all(item["version"] == "1.0" for item in packages))
        self.assertTrue(all("versionRange" not in item for item in packages))
        for item in packages:
            self.assertIn(
                "literate-ai:python-installed-tree-identity",
                {prop["name"] for prop in item["properties"]},
            )
        self.assertFalse(self.fixture.installation_path.exists())
        self.resolve()
        self.assertEqual(self.calls, 2)

    def test_default_resolver_remains_closed(self):
        with self.assertRaises(DependencyObservationError):
            self.resolve(
                self.resolver(
                    python_source_authority=None, python_dependency_observer=None
                )
            )
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.path.exists())

    def test_source_preflight_cannot_resolve_without_payload_observer(self):
        resolver = self.resolver(python_dependency_observer=None)
        resolver.validate_source(self.artifact())
        with self.assertRaises(DependencyObservationError):
            self.resolve(resolver)
        self.assertFalse(self.path.exists())

    def test_lock_source_and_source_graph_drift_rejected_before_installation(self):
        artifact = self.artifact()
        artifact["files"]["requirements.txt"] = "example>=1\n"
        with self.assertRaises(DependencyObservationError):
            self.resolver().validate_source(artifact)
        self.edges = (
            (self.graph.root_ref, "source-example"),
            (self.graph.root_ref, "source-helper"),
        )
        with self.assertRaises(DependencyObservationError):
            self.resolver().validate_source(self.artifact())
        self.assertEqual(self.calls, 0)

    def test_unproved_import_alias_rejected(self):
        self.components[0]["properties"].append(
            {"name": "literate-ai:python-top-level-import", "value": "invented"}
        )
        with self.assertRaisesRegex(DependencyObservationError, "imports absent"):
            self.resolve()
        self.assertFalse(self.path.exists())

    def test_payload_drift_rejected_on_second_resolution(self):
        self.resolve()
        previous = self.path.read_bytes()
        (self.evidence.payload_root / "site/example/__init__.py").write_bytes(
            b"tampered"
        )
        with self.assertRaises(DependencyObservationError):
            self.resolve()
        self.assertEqual(self.path.read_bytes(), previous)

    def test_wrong_source_or_incomplete_observation_rejected(self):
        evidence = self.observe()
        observations = (
            replace(evidence, source=replace(self.source, manifest_sha256="0" * 64)),
            replace(evidence, observation=replace(evidence.observation, edges=())),
            replace(
                evidence,
                observation=replace(
                    evidence.observation, components=evidence.observation.components[:1]
                ),
            ),
        )
        for index, observation in enumerate(observations):
            with (
                self.subTest(case=index),
                self.assertRaises(DependencyObservationError),
            ):
                self.resolve(
                    self.resolver(
                        python_dependency_observer=lambda item=observation: item
                    )
                )
        self.assertFalse(self.path.exists())

    def test_wrong_package_version_rejected(self):
        evidence = self.observe()
        components = copy.deepcopy(evidence.observation.components)
        components[0]["version"] = "9.0"
        changed = replace(
            evidence,
            observation=HostDependencyObservation(
                components, evidence.observation.edges
            ),
        )
        with self.assertRaises(DependencyObservationError):
            self.resolve(self.resolver(python_dependency_observer=lambda: changed))

    def test_build_json_cannot_substitute_for_trusted_observer(self):
        artifact = self.artifact()
        with self.assertRaises(DependencyObservationError):
            self.resolver(python_dependency_observer=None).resolve(
                artifact,
                {
                    "python_lifecycle_profile": True,
                    "python_dependency_evidence": {"verified": True},
                },
            )
        self.assertFalse(self.path.exists())

    def test_public_ports_accept_evidence_and_reject_identity_drift(self):
        artifact = self.artifact()
        resolver = self.resolver()
        source_validation = resolver.validate_source(artifact)
        common = {
            "resolver_id": resolver.resolver_id,
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "source_bom_identity": CycloneDxBomBinding.from_dict(
                source_validation["source_bom"]
            ).bom_identity.uri,
            "managed_graph_identity": self.graph.identity.uri,
            "composition_identity": self.graph.resolved_graph_identity.uri,
            "root_ref": self.graph.root_ref,
        }
        validated = require_dependency_source_validation_result(
            source_validation, **common
        )
        result = self.resolve(resolver)
        arguments = {
            **common,
            "source_validation": validated,
            "artifact_digest": result["artifact_digest"],
        }
        require_dependency_resolution_result(result, **arguments)
        for value in (None, canonical_identity({"changed": True}).uri):
            changed = {**result, "python_dependency_evidence_identity": value}
            with self.subTest(value=value), self.assertRaises(PortContractError):
                require_dependency_resolution_result(changed, **arguments)
        stripped = dict(result)
        stripped.pop("python_dependency_evidence_identity")
        with self.assertRaises(PortContractError):
            require_dependency_resolution_result(stripped, **arguments)

    def test_ambient_python_observations_cannot_supplement_verified_payloads(self):
        evidence = self.observe()
        with self.assertRaisesRegex(DependencyObservationError, "only from"):
            self.resolve(
                self.resolver(additional_components=evidence.observation.components)
            )
        self.assertFalse(self.path.exists())

    def test_distribution_name_is_not_proof_of_an_importable_module(self):
        evidence = self.observe()
        components = copy.deepcopy(evidence.observation.components)
        for component in (*components, *self.components):
            component["properties"] = [
                prop
                for prop in component["properties"]
                if prop["name"] != "literate-ai:python-top-level-import"
            ]
        changed = replace(
            evidence,
            observation=HostDependencyObservation(
                components, evidence.observation.edges
            ),
        )
        with self.assertRaisesRegex(
            DependencyObservationError, "installed payload evidence"
        ):
            self.resolve(self.resolver(python_dependency_observer=lambda: changed))
        self.assertFalse(self.path.exists())
