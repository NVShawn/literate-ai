"""Fail-closed port contract tests for builder-produced Bzlmod evidence."""

from __future__ import annotations

import copy
import unittest

from literate_ai.contracts import canonical_identity
from literate_ai.ports.contracts import (
    BuildDependencyEvidenceArtifact,
    BuildDependencyObservation,
    BuildInputConsumption,
    BzlmodModule,
    BzlmodRootModule,
    PortContractError,
    require_build_result,
)


def identity(label: str) -> str:
    return canonical_identity({"fixture": label}).uri


def observation() -> BuildDependencyObservation:
    return BuildDependencyObservation(
        resolver_id="bazel/bzlmod@1",
        source_bundle_digest=identity("source"),
        build_toolchain_identity=identity("python-toolchain"),
        resolver_toolchain_identity=identity("bazel-toolchain"),
        root_module=BzlmodRootModule(
            name="fixture",
            version="1.0.0",
            dependencies=("rules_python@2.2.0",),
        ),
        modules=(
            BzlmodModule(
                key="bazel_skylib@1.8.2",
                name="bazel_skylib",
                version="1.8.2",
                dependencies=("rules_python@2.2.0",),
            ),
            BzlmodModule(
                key="rules_python@2.2.0",
                name="rules_python",
                version="2.2.0",
                dependencies=("bazel_skylib@1.8.2",),
            ),
        ),
        evidence_artifacts=(
            BuildDependencyEvidenceArtifact(
                "bazel-module-lock",
                ".literate/bazel/MODULE.bazel.lock",
                identity("module-lock"),
            ),
            BuildDependencyEvidenceArtifact(
                "bazel-module-graph",
                ".literate/bazel/module-graph.json",
                identity("graph-json"),
            ),
            BuildDependencyEvidenceArtifact(
                "bazel-repository-definitions",
                ".literate/bazel/repositories.ndjson",
                identity("repository-definitions"),
            ),
        ),
    )


class BuildDependencyObservationContractTests(unittest.TestCase):
    def test_build_input_consumption_is_exact_immutable_and_content_addressed(
        self,
    ) -> None:
        expected = BuildInputConsumption(
            consumer_id="bazel/bzlmod@1",
            source_bundle_digest=identity("source"),
            files=("source/BUILD.bazel", "source/MODULE.bazel"),
        )

        self.assertEqual(BuildInputConsumption.from_dict(expected.to_dict()), expected)
        self.assertEqual(expected.identity, canonical_identity(expected.to_dict()).uri)
        with self.assertRaises(PortContractError):
            BuildInputConsumption(
                consumer_id="bazel/bzlmod@1",
                source_bundle_digest=identity("source"),
                files=("source/MODULE.bazel", "source/BUILD.bazel"),
            )
        with self.assertRaises(PortContractError):
            BuildInputConsumption(
                consumer_id="bazel/bzlmod@1",
                source_bundle_digest=identity("source"),
                files=("../BUILD.bazel",),
            )

    def test_canonical_observation_is_immutable_and_round_trips(self) -> None:
        expected = observation()
        wire = expected.to_dict()

        self.assertEqual(
            BuildDependencyObservation.from_dict(wire),
            expected,
        )
        self.assertEqual(wire["graph_identity"], expected.graph_identity)
        self.assertEqual(wire["evidence_identity"], expected.evidence_identity)
        self.assertEqual(wire["observation_identity"], expected.observation_identity)
        self.assertEqual(
            expected.modules[0].dependencies,
            ("rules_python@2.2.0",),
            "resolved Bzlmod cycle edges are exact dependency evidence",
        )
        self.assertEqual(
            tuple(
                (item.kind, item.logical_name) for item in expected.evidence_artifacts
            ),
            BuildDependencyObservation.REQUIRED_EVIDENCE_ARTIFACTS,
        )

        modules = wire["modules"]
        assert isinstance(modules, list)
        first = modules[0]
        assert isinstance(first, dict)
        first["version"] = "tampered"
        self.assertEqual(
            expected.modules[0].version,
            "1.8.2",
            "wire mutations must not change the immutable observation",
        )

    def test_build_result_validates_optional_observation_authorities(self) -> None:
        expected = observation()
        build = {
            "artifact_digest": identity("artifact"),
            "source_bundle_digest": identity("source"),
            "authorization_id": "authorization:fixture",
            "compiled_files": ["bin/run"],
            "toolchain_identity": identity("python-toolchain"),
            "dependency_observation": expected.to_dict(),
        }

        normalized = require_build_result(
            build,
            source_bundle_digest=identity("source"),
            authorization_id="authorization:fixture",
            toolchain_identity=identity("python-toolchain"),
        )
        self.assertEqual(normalized["dependency_observation"], expected.to_dict())

        legacy = dict(build)
        legacy.pop("dependency_observation")
        self.assertNotIn(
            "dependency_observation",
            require_build_result(
                legacy,
                source_bundle_digest=identity("source"),
                authorization_id="authorization:fixture",
            ),
        )

        mismatched_source = copy.deepcopy(build)
        mismatched_source["dependency_observation"]["source_bundle_digest"] = identity(
            "other-source"
        )
        mismatched_source["dependency_observation"] = _reidentify(
            mismatched_source["dependency_observation"]
        )
        with self.assertRaises(PortContractError) as source_error:
            require_build_result(
                mismatched_source,
                source_bundle_digest=identity("source"),
                authorization_id="authorization:fixture",
            )
        self.assertEqual(
            source_error.exception.code,
            "ports.build-dependencies.source-mismatch",
        )

        mismatched_toolchain = copy.deepcopy(build)
        mismatched_toolchain["dependency_observation"]["build_toolchain_identity"] = (
            identity("other-toolchain")
        )
        mismatched_toolchain["dependency_observation"] = _reidentify(
            mismatched_toolchain["dependency_observation"]
        )
        with self.assertRaises(PortContractError) as toolchain_error:
            require_build_result(
                mismatched_toolchain,
                source_bundle_digest=identity("source"),
                authorization_id="authorization:fixture",
            )
        self.assertEqual(
            toolchain_error.exception.code,
            "ports.build-dependencies.toolchain-mismatch",
        )

    def test_malformed_or_noncanonical_observations_fail_closed(self) -> None:
        valid = observation().to_dict()
        cases: tuple[tuple[str, dict[str, object], str], ...] = (
            (
                "observation-identity",
                {**valid, "observation_identity": identity("fabricated")},
                "ports.build-dependencies.identity-mismatch",
            ),
            (
                "graph-identity",
                {**valid, "graph_identity": identity("fabricated")},
                "ports.build-dependencies.graph-identity-mismatch",
            ),
            (
                "evidence-identity",
                {**valid, "evidence_identity": identity("fabricated")},
                "ports.build-dependencies.evidence-identity-mismatch",
            ),
            (
                "unknown-field",
                {**valid, "ambient_path": "/tmp/unsafe"},
                "ports.build-dependencies.fields-invalid",
            ),
        )
        for name, value, code in cases:
            with self.subTest(name=name):
                with self.assertRaises(PortContractError) as raised:
                    BuildDependencyObservation.from_dict(value)
                self.assertEqual(raised.exception.code, code)

        unsafe_artifact = copy.deepcopy(valid)
        unsafe_artifact["evidence_artifacts"][0]["logical_name"] = "../graph.json"
        with self.assertRaises(PortContractError) as unsafe:
            BuildDependencyObservation.from_dict(unsafe_artifact)
        self.assertEqual(
            unsafe.exception.code,
            "ports.build-dependencies.artifact-name-invalid",
        )

        dangling = copy.deepcopy(valid)
        dangling["modules"][1]["dependencies"] = ["missing@1.0.0"]
        dangling = _reidentify(dangling, graph=True)
        with self.assertRaises(PortContractError) as graph:
            BuildDependencyObservation.from_dict(dangling)
        self.assertEqual(
            graph.exception.code,
            "ports.build-dependencies.graph-reference-invalid",
        )

    def test_observation_requires_the_exact_canonical_evidence_set(self) -> None:
        valid = observation().to_dict()
        artifacts = valid["evidence_artifacts"]
        assert isinstance(artifacts, list)
        cases = {
            "missing-public-lock": artifacts[1:],
            "arbitrary-single-file": [
                {
                    "kind": "bazel-module-graph",
                    "logical_name": ".literate/bazel/module-graph.json",
                    "content_identity": identity("graph-json"),
                }
            ],
            "extra-artifact": [
                *artifacts,
                {
                    "kind": "bazel-module-extension-output",
                    "logical_name": ".literate/bazel/extensions.json",
                    "content_identity": identity("extensions"),
                },
            ],
            "wrong-kind-for-role": [
                {**artifacts[0], "kind": "bazel-module-graph"},
                *artifacts[1:],
            ],
            "wrong-logical-name": [
                artifacts[0],
                {
                    **artifacts[1],
                    "logical_name": ".literate/bazel/normalized-graph.json",
                },
                artifacts[2],
            ],
            "noncanonical-order": [artifacts[1], artifacts[0], artifacts[2]],
        }
        for name, candidate in cases.items():
            with self.subTest(name=name):
                wire = copy.deepcopy(valid)
                wire["evidence_artifacts"] = candidate
                with self.assertRaises(PortContractError) as raised:
                    BuildDependencyObservation.from_dict(wire)
                self.assertEqual(
                    raised.exception.code,
                    "ports.build-dependencies.artifact-set-invalid",
                )


def _reidentify(value: dict[str, object], *, graph: bool = False) -> dict[str, object]:
    result = copy.deepcopy(value)
    if graph:
        result["graph_identity"] = canonical_identity(
            {
                "schema": BuildDependencyObservation.GRAPH_SCHEMA,
                "root_module": result["root_module"],
                "modules": result["modules"],
            }
        ).uri
    material = dict(result)
    material.pop("observation_identity")
    result["observation_identity"] = canonical_identity(material).uri
    return result


if __name__ == "__main__":
    unittest.main()
