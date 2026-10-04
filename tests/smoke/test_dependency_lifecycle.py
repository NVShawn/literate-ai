from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from literate_ai.adapters.dependencies import (
    CycloneDxLifecycleResolver,
    DependencyObservationError,
    HostDependencyObservation,
    build_cyclonedx_bom,
    observe_installed_python_distributions,
)
from literate_ai.adapters.dependencies.acquisition import _generated_lock_projection
from literate_ai.adapters.dependencies.observation import (
    _npm_package_graph,
)
from literate_ai.adapters.dependencies.resolution import (
    _resolved_source_projection,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    canonical_identity,
    canonical_json_bytes,
    component_bom_ref,
)
from literate_ai.ports.contracts import (
    BuildDependencyEvidenceArtifact,
    BuildDependencyObservation,
    BuildInputConsumption,
    BzlmodModule,
    BzlmodRootModule,
)


def _managed_graph() -> CycloneDxManagedGraph:
    identity = canonical_identity({"fixture": "dependency-lifecycle"})
    root_ref = component_bom_ref(identity)
    return CycloneDxManagedGraph(
        root_ref,
        (
            CycloneDxManagedComponent(
                root_ref,
                ManagedComponentKind.ROOT,
                identity,
                "urn:literate-ai:component:fixture/application",
                "1.0.0",
                (),
            ),
        ),
        (),
        canonical_identity({"fixture": "dependency-lifecycle-composition"}),
    )


def _bazel_fixture_source(
    graph: CycloneDxManagedGraph,
    *,
    aggregate: str = "incomplete_third_party_only",
    exact: bool = False,
) -> bytes:
    dependency: dict[str, object] = {
        "type": "library",
        "bom-ref": "pkg:generic/rules_cc",
        "name": "rules_cc",
        "purl": "pkg:generic/rules_cc",
        "isExternal": True,
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "build"},
            {"name": "literate-ai:dependency-scope", "value": "build"},
            {
                "name": "literate-ai:bzlmod-requested-version",
                "value": "0.1.0",
            },
        ],
    }
    if exact:
        dependency["version"] = "0.1.0"
    else:
        dependency["versionRange"] = "vers:generic/>=0.1.0"
    source, _ = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=graph,
        additional_components=(dependency,),
        additional_edges=((graph.root_ref, "pkg:generic/rules_cc"),),
        composition_aggregate=aggregate,
    )
    return source


def _bzlmod_evidence_contents() -> dict[str, bytes]:
    rules_reference = {
        "key": "rules_cc@0.2.0",
        "name": "rules_cc",
        "version": "0.2.0",
        "apparentName": "rules_cc",
        "unexpanded": True,
    }
    platforms = {
        "key": "platforms@0.0.10",
        "name": "platforms",
        "version": "0.0.10",
        "apparentName": "platforms",
        "dependencies": [],
        "indirectDependencies": [],
        "cycles": [rules_reference],
    }
    rules = {
        "key": "rules_cc@0.2.0",
        "name": "rules_cc",
        "version": "0.2.0",
        "apparentName": "rules_cc",
        "dependencies": [platforms],
        "indirectDependencies": [],
        "cycles": [],
    }
    graph = json.dumps(
        {
            "key": "<root>",
            "name": "fixture_app",
            "version": "1.0.0",
            "apparentName": "fixture_app",
            "root": True,
            "dependencies": [rules],
            "indirectDependencies": [],
            "cycles": [],
        },
        indent=2,
    ).encode()
    registry_hashes: dict[str, str] = {}
    for name, version in (("platforms", "0.0.10"), ("rules_cc", "0.2.0")):
        prefix = f"https://bcr.example/modules/{name}/{version}"
        registry_hashes[f"{prefix}/MODULE.bazel"] = "a" * 64
        registry_hashes[f"{prefix}/source.json"] = "b" * 64
    lock = json.dumps(
        {
            "lockFileVersion": 11,
            "registryFileHashes": registry_hashes,
            "selectedYankedVersions": {},
            "moduleExtensions": {"fixture-extension": {"general": {}}},
            "facts": {},
            "factsVersions": {},
        },
        indent=2,
    ).encode()
    integrity = "sha256-" + base64.b64encode(b"i" * 32).decode()
    repository_records = []
    for name, version in (("platforms", "0.0.10"), ("rules_cc", "0.2.0")):
        repository_records.append(
            {
                "canonicalName": name if name == "platforms" else f"{name}+",
                "repoRuleName": "http_archive",
                "repoRuleBzlLabel": "@@bazel_tools//tools/build_defs/repo:http.bzl",
                "moduleKey": f"{name}@{version}",
                "attribute": [
                    {
                        "name": "integrity",
                        "type": "STRING",
                        "stringValue": integrity,
                    },
                    {
                        "name": "remote_module_file_integrity",
                        "type": "STRING",
                        "stringValue": integrity,
                    },
                    {
                        "name": "remote_module_file_urls",
                        "type": "STRING_LIST",
                        "stringListValue": [
                            "https://bcr.bazel.build/modules/"
                            f"{name}/{version}/MODULE.bazel"
                        ],
                    },
                    {
                        "name": "urls",
                        "type": "STRING_LIST",
                        "stringListValue": [
                            f"https://archives.example/{name}-{version}.tar.gz"
                        ],
                    },
                ],
            }
        )
    repositories = b"".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for record in repository_records
    )
    return {
        ".literate/bazel/MODULE.bazel.lock": lock,
        ".literate/bazel/module-graph.json": graph,
        ".literate/bazel/repositories.ndjson": repositories,
    }


def _fixture_bazel_toolchain() -> dict[str, object]:
    material: dict[str, object] = {
        "command": ["/opt/fixture/bin/bazel"],
        "launcher_digest": "sha256:" + "c" * 64,
        "launcher_executable": "/opt/fixture/bin/bazel",
        "version": "bazel 9.2.0",
    }
    return {**material, "identity": canonical_identity(material).uri}


def _bzlmod_observation(
    *, source_bundle_digest: str, toolchain_identity: str
) -> dict[str, object]:
    contents = _bzlmod_evidence_contents()
    return BuildDependencyObservation(
        resolver_id="bazel/bzlmod@1",
        source_bundle_digest=source_bundle_digest,
        build_toolchain_identity=toolchain_identity,
        resolver_toolchain_identity=str(_fixture_bazel_toolchain()["identity"]),
        root_module=BzlmodRootModule("fixture_app", "1.0.0", ("rules_cc@0.2.0",)),
        modules=(
            BzlmodModule(
                "platforms@0.0.10",
                "platforms",
                "0.0.10",
                ("rules_cc@0.2.0",),
            ),
            BzlmodModule(
                "rules_cc@0.2.0",
                "rules_cc",
                "0.2.0",
                ("platforms@0.0.10",),
            ),
        ),
        evidence_artifacts=(
            BuildDependencyEvidenceArtifact(
                "bazel-module-lock",
                ".literate/bazel/MODULE.bazel.lock",
                "sha256:"
                + hashlib.sha256(
                    contents[".literate/bazel/MODULE.bazel.lock"]
                ).hexdigest(),
            ),
            BuildDependencyEvidenceArtifact(
                "bazel-module-graph",
                ".literate/bazel/module-graph.json",
                "sha256:"
                + hashlib.sha256(
                    contents[".literate/bazel/module-graph.json"]
                ).hexdigest(),
            ),
            BuildDependencyEvidenceArtifact(
                "bazel-repository-definitions",
                ".literate/bazel/repositories.ndjson",
                "sha256:"
                + hashlib.sha256(
                    contents[".literate/bazel/repositories.ndjson"]
                ).hexdigest(),
            ),
        ),
    ).to_dict()


def _write_bzlmod_composite(
    root: Path, *, build: dict[str, object], observation: dict[str, object]
) -> None:
    contents = _bzlmod_evidence_contents()
    consumption = BuildInputConsumption(
        consumer_id="bazel/bzlmod@1",
        source_bundle_digest=str(build["source_bundle_digest"]),
        files=("source/MODULE.bazel", "source/main.cc"),
    )
    contents = {
        **contents,
        ".literate/bazel/buildfiles.txt": b"",
        ".literate/bazel/source-inputs.txt": b"//:main.cc\n",
        ".literate/bazel/build-input-consumption.json": canonical_json_bytes(
            consumption.to_dict()
        ),
    }
    for relative, content in contents.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    files = [
        {
            "path": relative,
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
        for relative, content in sorted(contents.items())
    ]
    manifest = {
        "schema": "urn:literate-ai:schema:v1:bazel-conformance-artifact",
        "authorization_id": build["authorization_id"],
        "builder_id": "builder:fixture@1",
        "delegate_artifact_digest": canonical_identity(
            {"fixture": "delegate-artifact"}
        ).uri,
        "source_bundle_digest": build["source_bundle_digest"],
        "build_toolchain_identity": observation["build_toolchain_identity"],
        "resolver_toolchain_identity": observation["resolver_toolchain_identity"],
        "graph_identity": observation["graph_identity"],
        "evidence_identity": observation["evidence_identity"],
        "observation_identity": observation["observation_identity"],
        "build_input_consumption": consumption.to_dict(),
        "build_input_consumption_identity": consumption.identity,
        "files": files,
    }
    manifest_content = canonical_json_bytes(manifest)
    (root / "build-manifest.json").write_bytes(manifest_content)
    build["artifact_digest"] = "sha256:" + hashlib.sha256(manifest_content).hexdigest()
    build["artifact_path"] = str(root)
    build["build_input_consumption"] = consumption.to_dict()
    build["build_input_consumption_identity"] = consumption.identity


class _ObservedRuntime:
    def observe(self, build, *, root_ref: str) -> HostDependencyObservation:
        del build
        runtime_ref = "pkg:generic/observed-runtime@1.2.3"
        return HostDependencyObservation(
            (
                {
                    "type": "library",
                    "bom-ref": runtime_ref,
                    "name": "observed-runtime",
                    "version": "1.2.3",
                    "properties": [
                        {
                            "name": "literate-ai:dependency-kind",
                            "value": "system",
                        },
                        {
                            "name": "literate-ai:dependency-scope",
                            "value": "runtime",
                        },
                    ],
                },
            ),
            ((root_ref, runtime_ref),),
        )


class _EmptyObserver:
    def observe(self, build, *, root_ref: str) -> HostDependencyObservation:
        del build, root_ref
        return HostDependencyObservation((), ())


class DependencyLifecycleTests(unittest.TestCase):
    def test_python_distribution_observation_includes_transitive_file_closure(
        self,
    ) -> None:
        root_ref = "urn:fixture:python-root"
        observation = observe_installed_python_distributions(
            (
                "cyclonedx-python-lib[json-validation]==11.11.0",
                "univers==32.0.1",
            ),
            root_ref=root_ref,
        )
        inventory = {item["bom-ref"]: item for item in observation.components}

        self.assertIn("pkg:pypi/univers", inventory)
        self.assertIn("pkg:pypi/packaging", inventory)
        self.assertIn("pkg:pypi/jsonschema", inventory)
        self.assertIn("pkg:pypi/fqdn", inventory)
        self.assertIn((root_ref, "pkg:pypi/univers"), observation.edges)
        self.assertIn(
            ("pkg:pypi/cyclonedx-python-lib", "pkg:pypi/jsonschema"),
            observation.edges,
        )
        self.assertIn(("pkg:pypi/jsonschema", "pkg:pypi/fqdn"), observation.edges)
        self.assertIn(("pkg:pypi/univers", "pkg:pypi/packaging"), observation.edges)
        self.assertRegex(
            inventory["pkg:pypi/univers"]["hashes"][0]["content"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            observe_installed_python_distributions(
                ("packaging; python_version < '0'",), root_ref=root_ref
            ).components,
            (),
        )

    def test_npm_observation_rejects_name_escape_links_and_manifest_aliases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            boundary = Path(temporary)
            root = boundary / "root"
            outside = boundary / "outside"

            def manifest(path: Path, value: dict[str, object]) -> None:
                path.mkdir(parents=True, exist_ok=True)
                (path / "package.json").write_text(json.dumps(value), encoding="utf-8")

            manifest(outside, {"name": "outside", "version": "1.0.0"})
            manifest(
                root,
                {
                    "name": "launcher",
                    "version": "1.0.0",
                    "dependencies": {"../../../outside": "1.0.0"},
                },
            )
            with self.assertRaises(DependencyObservationError) as traversal:
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                traversal.exception.code, "dependencies.npm-declaration-invalid"
            )

            manifest(
                root,
                {
                    "name": "launcher",
                    "version": "1.0.0",
                    "dependencies": {"child": "1.0.0"},
                },
            )
            child = root / "node_modules" / "child"
            child.parent.mkdir(parents=True, exist_ok=True)
            try:
                child.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("host cannot create an adversarial directory link")
            with self.assertRaises(DependencyObservationError) as linked:
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                linked.exception.code, "dependencies.npm-dependency-path-unsafe"
            )
            child.unlink()

            manifest(child, {"name": "different-child", "version": "1.0.0"})
            with self.assertRaises(DependencyObservationError) as alias:
                _npm_package_graph(root, native_predicate=lambda _path: False)
            self.assertEqual(
                alias.exception.code, "dependencies.npm-dependency-name-mismatch"
            )

    def test_bzlmod_observation_resolves_full_reachable_graph(self) -> None:
        graph = _managed_graph()
        source_bundle = canonical_identity({"fixture": "bazel-source"}).uri
        toolchain = canonical_identity({"fixture": "native-toolchain"}).uri
        source_document = json.loads(_bazel_fixture_source(graph))
        rules_source = next(
            item for item in source_document["components"] if item["name"] == "rules_cc"
        )
        rules_source["versionRange"] = "vers:generic/>=999.0.0"
        requested_property = next(
            item
            for item in rules_source["properties"]
            if item["name"] == "literate-ai:bzlmod-requested-version"
        )
        requested_property["value"] = "999.0.0"
        source = canonical_json_bytes(source_document)
        module = (
            'module(name = "fixture_app", version = "1.0.0")\n'
            'bazel_dep(name = "rules_cc", version = "999.0.0")\n'
        )
        artifact = {
            "effective_revision_digest": canonical_identity(
                {"fixture": "bazel-revision"}
            ).uri,
            "source_bundle_digest": source_bundle,
            "files": {
                CYCLONEDX_SOURCE_SBOM_PATH: source.decode(),
                "source/MODULE.bazel": module,
                "source/main.cc": "int main() { return 0; }\n",
            },
        }
        observation = _bzlmod_observation(
            source_bundle_digest=source_bundle,
            toolchain_identity=toolchain,
        )
        build: dict[str, object] = {
            "source_bundle_digest": source_bundle,
            "authorization_id": "authorization:fixture",
            "toolchain_identity": toolchain,
            "dependency_observation": observation,
        }
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            artifact_path = temporary_root / "artifact"
            artifact_path.mkdir()
            _write_bzlmod_composite(
                artifact_path,
                build=build,
                observation=observation,
            )
            evidence_artifact = (
                artifact_path / ".literate" / "bazel" / "module-graph.json"
            )
            evidence_path = temporary_root / "resolved.cdx.json"
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_EmptyObserver(),
                evidence_path=evidence_path,
            )
            path_type = type(artifact_path)
            with mock.patch.object(
                path_type,
                "__lt__",
                lambda left, right: str(left).casefold() < str(right).casefold(),
            ):
                resolver.resolve(artifact, build)
            document = json.loads(evidence_path.read_bytes())
            inventory = {item["bom-ref"]: item for item in document["components"]}
            self.assertEqual(inventory["pkg:generic/rules_cc"]["version"], "0.2.0")
            rules_properties = {
                item["name"]: item["value"]
                for item in inventory["pkg:generic/rules_cc"]["properties"]
            }
            self.assertEqual(
                rules_properties["literate-ai:bzlmod-selected-version"], "0.2.0"
            )
            self.assertEqual(
                inventory["pkg:generic/platforms@0.0.10"]["version"], "0.0.10"
            )
            edges = {
                (item["ref"], target)
                for item in document["dependencies"]
                for target in item["dependsOn"]
            }
            self.assertIn((graph.root_ref, "pkg:generic/rules_cc"), edges)
            self.assertIn(
                ("pkg:generic/rules_cc", "pkg:generic/platforms@0.0.10"), edges
            )
            self.assertIn(
                ("pkg:generic/platforms@0.0.10", "pkg:generic/rules_cc"), edges
            )

            buildfiles_path = artifact_path / ".literate" / "bazel" / "buildfiles.txt"
            buildfiles_path.write_bytes(b"//:BUILD.bazel\n")
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-build-inputs-mismatch"
            )
            buildfiles_path.write_bytes(b"")

            consumption_path = (
                artifact_path / ".literate" / "bazel" / "build-input-consumption.json"
            )
            original_consumption = consumption_path.read_bytes()
            consumption_path.write_bytes(b"{}")
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-build-inputs-content-mismatch",
            )
            consumption_path.write_bytes(original_consumption)

            mismatched_consumption = copy.deepcopy(build)
            mismatched_consumption["build_input_consumption_identity"] = (
                canonical_identity({"fixture": "wrong-consumption"}).uri
            )
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, mismatched_consumption)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-build-inputs-authority-mismatch",
            )

            missing_observation = dict(build)
            missing_observation.pop("dependency_observation")
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, missing_observation)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-observation-missing"
            )

            tampered = copy.deepcopy(build)
            tampered["dependency_observation"]["modules"][0]["version"] = "0.0.11"
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, tampered)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-observation-invalid",
            )

            manifest_path = artifact_path / "build-manifest.json"
            original_manifest = manifest_path.read_bytes()
            incomplete_manifest = json.loads(original_manifest)
            incomplete_manifest["files"].pop()
            incomplete_manifest_bytes = canonical_json_bytes(incomplete_manifest)
            manifest_path.write_bytes(incomplete_manifest_bytes)
            build["artifact_digest"] = (
                "sha256:" + hashlib.sha256(incomplete_manifest_bytes).hexdigest()
            )
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-manifest-files-mismatch",
            )
            manifest_path.write_bytes(original_manifest)
            build["artifact_digest"] = (
                "sha256:" + hashlib.sha256(original_manifest).hexdigest()
            )

            evidence_artifact.write_bytes(b"changed graph\n")
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code,
                "dependencies.bzlmod-evidence-content-mismatch",
            )

            evidence_artifact.unlink()
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-evidence-missing"
            )

            outside = temporary_root / "outside.json"
            outside.write_bytes(b"fixture graph\n")
            evidence_artifact.symlink_to(outside)
            with self.assertRaises(DependencyObservationError) as caught:
                resolver.resolve(artifact, build)
            self.assertEqual(
                caught.exception.code, "dependencies.bzlmod-evidence-unsafe"
            )

            evidence_artifact.unlink()
            evidence_artifact.mkdir()
            with self.assertRaises(DependencyObservationError):
                resolver.resolve(artifact, build)

    def test_npm_lock_projects_integrity_and_parent_edges_exactly(self) -> None:
        graph = _managed_graph()
        alpha_ref = "pkg:npm/alpha"
        beta_ref = "pkg:npm/beta"

        def package(ref: str, name: str) -> dict[str, object]:
            return {
                "type": "library",
                "bom-ref": ref,
                "name": name,
                "purl": f"pkg:npm/{name}",
                "isExternal": True,
                "versionRange": "vers:npm/>=1.0.0|<2.0.0",
                "properties": [
                    {"name": "literate-ai:dependency-kind", "value": "package"},
                    {"name": "literate-ai:dependency-scope", "value": "runtime"},
                ],
            }

        source_content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=(
                package(alpha_ref, "alpha"),
                package(beta_ref, "beta"),
            ),
            additional_edges=(
                (graph.root_ref, alpha_ref),
                (alpha_ref, beta_ref),
            ),
        )
        integrity = "sha256-" + base64.b64encode(b"a" * 32).decode()
        package_lock = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "fixture-app",
                        "version": "1.0.0",
                        "dependencies": {"alpha": "1.1.0"},
                    },
                    "node_modules/alpha": {
                        "version": "1.1.0",
                        "integrity": integrity,
                        "dependencies": {"beta": "1.2.0"},
                        "peerDependencies": {"missing-peer": ">=3.0.0"},
                        "peerDependenciesMeta": {"missing-peer": {"optional": True}},
                    },
                    "node_modules/beta": {
                        "version": "1.2.0",
                        "integrity": integrity,
                    },
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_path = root / "artifact"
            artifact_path.mkdir()
            evidence_path = root / "resolved.cdx.json"
            CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_ObservedRuntime(),
                evidence_path=evidence_path,
            ).resolve(
                {
                    "effective_revision_digest": canonical_identity(
                        {"fixture": "npm-revision"}
                    ).uri,
                    "source_bundle_digest": canonical_identity(
                        {"fixture": "npm-source"}
                    ).uri,
                    "files": {
                        CYCLONEDX_SOURCE_SBOM_PATH: source_content.decode(),
                        "source/package.json": json.dumps(
                            {
                                "name": "fixture-app",
                                "dependencies": {"alpha": "1.1.0"},
                            }
                        ),
                        "source/package-lock.json": package_lock,
                        "source/main.js": "import alpha from 'alpha';\n",
                    },
                },
                {
                    "source_bundle_digest": canonical_identity(
                        {"fixture": "npm-source"}
                    ).uri,
                    "artifact_digest": canonical_identity(
                        {"fixture": "npm-artifact"}
                    ).uri,
                    "artifact_path": str(artifact_path),
                },
            )
            inventory = {
                item["bom-ref"]: item
                for item in json.loads(evidence_path.read_bytes())["components"]
            }
            self.assertEqual(inventory[alpha_ref]["version"], "1.1.0")
            self.assertEqual(inventory[beta_ref]["version"], "1.2.0")
            self.assertEqual(inventory[beta_ref]["hashes"][0]["alg"], "SHA-256")

        lock_projection = _generated_lock_projection(
            {
                "source/package.json": json.dumps(
                    {
                        "name": "fixture-app",
                        "dependencies": {"alpha": "1.1.0"},
                    }
                ),
                "source/package-lock.json": package_lock,
            }
        )
        missing_edge_source, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=(
                package(alpha_ref, "alpha"),
                package(beta_ref, "beta"),
            ),
            additional_edges=(
                (graph.root_ref, alpha_ref),
                (graph.root_ref, beta_ref),
            ),
        )
        with self.assertRaises(DependencyObservationError) as caught:
            _resolved_source_projection(
                missing_edge_source,
                managed_graph=graph,
                resolution_candidates=(),
                lock_projection=lock_projection,
            )
        self.assertEqual(caught.exception.code, "dependencies.lock-edge-missing")

        wrong_ecosystem = package(beta_ref, "beta")
        wrong_ecosystem["purl"] = "pkg:cargo/beta"
        wrong_ecosystem["versionRange"] = "vers:cargo/>=1.0.0|<2.0.0"
        wrong_ecosystem_source, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
            additional_components=(package(alpha_ref, "alpha"), wrong_ecosystem),
            additional_edges=(
                (graph.root_ref, alpha_ref),
                (alpha_ref, beta_ref),
            ),
        )
        with self.assertRaises(DependencyObservationError) as caught:
            _resolved_source_projection(
                wrong_ecosystem_source,
                managed_graph=graph,
                resolution_candidates=(),
                lock_projection=lock_projection,
            )
        self.assertEqual(caught.exception.code, "dependencies.source-range-unresolved")


if __name__ == "__main__":
    unittest.main()
