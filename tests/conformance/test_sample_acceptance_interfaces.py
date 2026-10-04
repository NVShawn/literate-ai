from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from literate_ai.adapters.models import GenerationRecipe, RecipeDocument
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    canonical_json_bytes,
)
from literate_ai.projects import project_boundary
from tests.conformance.support.sample_runner import (
    _coding_skills,
    _execution_contract,
    _load_sample,
    sample_harness_manifest,
    sample_harness_oracle,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPO_ROOT / "samples"
EXECUTION_INTERFACE_SCHEMA = "literate-ai/sample-execution-interface@3"
EXECUTION_ORACLE_SCHEMA = "literate-ai/sample-execution-oracle@1"
SAMPLE_SCHEMA = "literate-ai/conformance-sample@5"
TEST_COMPONENT_LOCK_IDENTITY = ContentIdentity.parse_uri("sha256:" + "b" * 64)

# Canonical SHA-256 of the original @1 arguments and expected result. These locks
# prove that the primary case survived both the multi-case and authority-boundary
# migrations unchanged.
PRIMARY_CASE_IDENTITIES = {
    "critical-path-scheduler": (
        "75b8874b1958dbb9a683e23a1b3d33c9c3dcc9a5581b4dca4a35084577dc1642"
    ),
    "dependency-planner": (
        "74a0d9baae351cbb82f6ccc1b42838fd4fb8c645836d4b0d9b30b3e6be20ad33"
    ),
    "empty-cache-restart": (
        "b2355639cdcd64d1378d585b41ec88009058090ccacc6a4f1e6504ee6c744c23"
    ),
    "flavor-matrix": "a0a6c597788edcee8f0f3e833faf0847a8e627eef7d9b1f86f23849ce94c7868",
    "full-stack-rust-js": (
        "7aa398f58662b80447a524f2d7ac4bb76bbfd95412218ecc54e81eeb358c49e2"
    ),
    "generated-library": (
        "ffc541292f39ea435b3b12039b3f777556f965dc8989908179e58c75e6aea2c9"
    ),
    "hello-component": (
        "5972008de95c1d917c9c5e1f0fcbb9cab33d4eaafb0de4359b481c3e9483f79b"
    ),
    "javascript-ledger-workbench": (
        "8e1d3d84849b4d53ced702df439728ebdf39794ab56050b9162f949097406269"
    ),
    "model-routing": "98aa714f57bfd856853d95202e7d0a364def64f26a8626fe78752fa8610d5740",
    "multi-repository-component": (
        "d79d9885ddce55c56f7800a4817868094a02f76b810842967a71a8585e9bdff2"
    ),
    "publication-import": (
        "4af00b33db44fc68f4f0c8e51793b5c2d0f84c6e2f3d2fd0455c42d429146832"
    ),
    "regenerative-roundtrip": (
        "4d4c7a0e1a62a457b7d26fa9cdd50d50f90800132d6d212360ec199ccc7031b2"
    ),
    "security-policies": (
        "1745caf3f5f3ca44015954d4a80547131b3ef6e616befa9d014f757178cfec57"
    ),
    "self-hosting": "9de294ae9cdb625fd9c4df34d0f8f5e759eb155aa2926f445c1afabaf91d1204",
    "service-stack": "87a17b97b04a8ae40e4413cb5e5d54c631374de7b30e902fcd26a6edc90f6b93",
}


def _sample_roots() -> tuple[Path, ...]:
    return tuple(
        sorted(
            SAMPLES / path.parent.name
            for path in (SAMPLES / "_harness").glob("*/sample.json")
        )
    )


class SampleAcceptanceInterfaceTests(unittest.TestCase):
    def test_interfaces_and_verifier_oracles_have_disjoint_exact_authority(self):
        roots = _sample_roots()
        discovered_names = {root.name for root in roots}
        self.assertTrue(
            set(PRIMARY_CASE_IDENTITIES).issubset(discovered_names),
            "every migration-locked sample must remain discoverable",
        )
        for sample_root in roots:
            with self.subTest(sample=sample_root.name):
                component_bytes = (sample_root / "component.md").read_bytes()
                _metadata, definition, loaded, _closures = _load_sample(sample_root)
                interface, interface_document = _execution_contract(
                    sample_root, definition, loaded
                )
                interface_identity = ContentIdentity.parse_uri(
                    interface_document.identity
                )
                interface_path = (
                    sample_harness_manifest(sample_root).parent
                    / "acceptance"
                    / "execution.json"
                )
                interface_bytes = interface_path.read_bytes()
                self.assertEqual(
                    set(interface),
                    {
                        "schema",
                        "requirement_id",
                        "scenario_id",
                        "entrypoint",
                        "invocations",
                        "result_shape",
                        "target",
                        "dependencies",
                    },
                )
                self.assertEqual(interface["schema"], EXECUTION_INTERFACE_SCHEMA)
                self.assertEqual(
                    hashlib.sha256(interface_bytes).hexdigest(),
                    interface_identity.digest,
                )
                self.assertNotIn(b"oracle", interface_bytes.lower())
                self.assertNotIn(b"expected_result", interface_bytes)

                metadata = json.loads(sample_harness_manifest(sample_root).read_bytes())
                self.assertEqual(metadata["schema"], SAMPLE_SCHEMA)
                oracle_reference = ContentReference.from_dict(
                    metadata["acceptance_oracle"]
                )
                self.assertEqual(oracle_reference.kind, "acceptance-oracle")
                self.assertEqual(oracle_reference.uri, "acceptance/oracle.json")
                oracle_bytes = sample_harness_oracle(
                    sample_root, oracle_reference
                ).read_bytes()
                self.assertEqual(
                    hashlib.sha256(oracle_bytes).hexdigest(),
                    oracle_reference.identity.digest,
                )
                oracle = json.loads(oracle_bytes)
                self.assertEqual(
                    set(oracle),
                    {"schema", "execution_interface_identity", "oracle_results"},
                )
                self.assertEqual(oracle["schema"], EXECUTION_ORACLE_SCHEMA)
                self.assertEqual(
                    oracle["execution_interface_identity"],
                    interface_identity.uri,
                )
                self.assertNotIn(oracle_reference.uri.encode(), component_bytes)
                self.assertNotIn(
                    oracle_reference.identity.uri.encode(), component_bytes
                )
                self.assertNotIn(
                    oracle_reference.identity.uri.encode(), interface_bytes
                )

                invocations = interface["invocations"]
                oracles = oracle["oracle_results"]
                self.assertGreaterEqual(len(invocations), 2)
                self.assertEqual(
                    [item["case_id"] for item in invocations],
                    [item["case_id"] for item in oracles],
                )
                self.assertNotEqual(
                    invocations[0]["arguments"], invocations[1]["arguments"]
                )
                self.assertNotEqual(
                    oracles[0]["expected_result"], oracles[1]["expected_result"]
                )
                primary = {
                    "arguments": invocations[0]["arguments"],
                    "expected_result": oracles[0]["expected_result"],
                }
                locked_identity = PRIMARY_CASE_IDENTITIES.get(sample_root.name)
                if locked_identity is not None:
                    self.assertEqual(invocations[0]["case_id"], "primary")
                    self.assertEqual(invocations[1]["case_id"], "generalization")
                    self.assertEqual(
                        hashlib.sha256(canonical_json_bytes(primary)).hexdigest(),
                        locked_identity,
                    )

    def test_public_generation_recipe_can_discover_only_the_safe_interface(self):
        for sample_root in _sample_roots():
            with self.subTest(sample=sample_root.name):
                _metadata, definition, _loaded, closures = _load_sample(sample_root)
                documents = tuple(
                    RecipeDocument.create(path, content.decode("utf-8"))
                    for path, content in _loaded.contents
                )
                _execution, interface_document = _execution_contract(
                    sample_root, definition, _loaded
                )
                interface_bytes = (
                    sample_harness_manifest(sample_root).parent
                    / "acceptance"
                    / "execution.json"
                ).read_bytes()
                self.assertEqual(
                    interface_document.content.encode("utf-8"), interface_bytes
                )
                self.assertEqual(
                    interface_document.identity,
                    "sha256:" + hashlib.sha256(interface_bytes).hexdigest(),
                )
                self.assertNotIn(
                    "acceptance/oracle.json", {item.path for item in documents}
                )
                metadata = json.loads(sample_harness_manifest(sample_root).read_bytes())
                oracle_reference = ContentReference.from_dict(
                    metadata["acceptance_oracle"]
                )
                prompt = GenerationRecipe(
                    recipe_id=f"public-generation-{sample_root.name}",
                    application_id=sample_root.name,
                    documents=(*documents, interface_document),
                    component_lock_identity=TEST_COMPONENT_LOCK_IDENTITY,
                    skills=_coding_skills(
                        sample_root,
                        definition,
                        boundary=project_boundary(
                            sample_root, legacy=sample_root.parent
                        ),
                        source=f"Component {definition.coordinate.uri}",
                    ),
                ).prompt()
                self.assertIsNot(closures.generation, closures.verifier)
                self.assertNotIn("oracle_results", prompt)
                self.assertIn("`expected_result`", prompt)
                self.assertIn("generated implementation-test suite", prompt)
                self.assertIn("Generation-safe invocation contract", prompt)
                self.assertIn('"invocation_signatures"', prompt)
                self.assertIn('"result_shape"', prompt)
                self.assertIn('"arity":1', prompt)
                self.assertNotIn(interface_document.content, prompt)
                self.assertNotIn(oracle_reference.uri, prompt)
                self.assertNotIn(oracle_reference.identity.uri, prompt)


if __name__ == "__main__":
    unittest.main()
