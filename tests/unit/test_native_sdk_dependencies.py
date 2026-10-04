"""Real relocated SDK products preserve repository and native CycloneDX evidence."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace

from literate_ai.adapters.dependencies import (
    CycloneDxLifecycleResolver,
    build_cyclonedx_bom,
    validate_resolved_cyclonedx_bom,
)
from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_dependencies import (
    NativeSdkDependencyEvidence,
    merge_native_sdk_dependencies,
    merge_sdk_host_observations,
    project_native_sdk_dependencies,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxLifecycle,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.sbom import (
    CycloneDxRepositorySourceResolution,
    project_component_lock_managed_graph,
)
from tests.support import fixtures_test_native_sdk_source_build as test_native_sdk_source_build


class _NoConsumerBuildObservation:
    """The test supplies real SDK observations, without claiming a consumer build."""

    def observe(self, build, *, root_ref):
        return HostDependencyObservation((), ())


class NativeSdkDependencyMergeTests(unittest.TestCase):
    def test_shared_host_image_combines_scopes_but_rejects_changed_facts(self):
        sdk_component = {
            "bom-ref": "native-image",
            "name": "libshared",
            "version": "exact",
            "hashes": [{"alg": "SHA-256", "content": "a" * 64}],
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "system"},
                {"name": "literate-ai:dependency-scope", "value": "runtime"},
            ],
        }
        host_component = copy.deepcopy(sdk_component)
        host_component["properties"][-1]["value"] = "build"
        sdk = HostDependencyObservation((sdk_component,), (("sdk", "native-image"),))
        host = HostDependencyObservation(
            (host_component,), (("consumer", "native-image"),)
        )
        merged = merge_sdk_host_observations(sdk, host)
        self.assertEqual(merged, merge_sdk_host_observations(host, sdk))
        self.assertEqual(
            set(merged.edges), {("sdk", "native-image"), ("consumer", "native-image")}
        )
        self.assertEqual(
            {
                item["value"]
                for item in merged.components[0]["properties"]
                if item["name"] == "literate-ai:dependency-scope"
            },
            {"runtime", "build"},
        )
        host_component["hashes"][0]["content"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "observations conflict"):
            merge_sdk_host_observations(sdk, host)

    def evidence(self, name):
        resolution = CycloneDxRepositorySourceResolution(
            dependency_identity=canonical_identity("repository"),
            resolved_commit="a" * 40,
            source_lock_identity=canonical_identity("lock"),
            source_snapshot_identity=canonical_identity("snapshot"),
            source_tree_identity=canonical_identity("tree"),
            resolver_identity=canonical_identity("resolver"),
            index_identity=canonical_identity("index"),
            admission_identity=canonical_identity(name + "-admission"),
            cache_record_identity=canonical_identity(name + "-cache"),
        )
        return NativeSdkDependencyEvidence(
            canonical_identity(name),
            resolution,
            canonical_json_bytes(
                [
                    {
                        "bom-ref": name,
                        "name": name,
                        "type": "library",
                        "version": "1",
                        "properties": [
                            {
                                "name": "literate-ai:native-sdk-source-admission",
                                "value": resolution.admission_identity.uri,
                            }
                        ],
                    }
                ]
            ),
            (("consumer", name),),
        )

    def test_shared_source_uses_canonical_proof_and_retains_each_sdk_admission(self):
        first, second = self.evidence("one"), self.evidence("two")
        observed, resolutions = merge_native_sdk_dependencies((first, second))
        self.assertEqual(
            merge_native_sdk_dependencies((second, first)), (observed, resolutions)
        )
        self.assertEqual(len(resolutions), 1)
        self.assertEqual(len(observed.components), 2)
        self.assertEqual(
            {item["properties"][0]["value"] for item in observed.components},
            {
                first.repository_resolution.admission_identity.uri,
                second.repository_resolution.admission_identity.uri,
            },
        )

    def test_conflicting_source_observations_and_duplicate_inputs_refuse(self):
        first, second = self.evidence("one"), self.evidence("two")
        conflict = replace(
            second,
            repository_resolution=replace(
                second.repository_resolution,
                source_tree_identity=canonical_identity("drift"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "source proofs conflict"):
            merge_native_sdk_dependencies((first, conflict))
        with self.assertRaisesRegex(ValueError, "duplicate inputs"):
            merge_native_sdk_dependencies((first, first))
        changed = json.loads(first._components_json)
        changed[0]["name"] = "conflicting observed image"
        conflict = replace(second, _components_json=canonical_json_bytes(changed))
        with self.assertRaisesRegex(ValueError, "observations conflict"):
            merge_native_sdk_dependencies((first, conflict))


class NativeSdkDependencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_sdk_source_build.NativeSdkSourceBuildTests(
            "test_real_policy_build_cache_and_relocated_native_execution"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.service = self.fixture.service()
        self.built = self.service.build()
        self.inputs = NativeSdkConsumerInputs(
            snapshot=self.fixture.snapshot, services=(self.service,)
        )
        self.revision = self.built.selection.component_revision
        self.target = self.built.selection.target_identity
        self.parent = self.fixture.fixture.root / "consumer SDKs"
        self.parent.mkdir()

    def materialize(self):
        return self.inputs.materialize(
            self.revision, target_identity=self.target, parent=self.parent
        )

    def test_real_sdk_graph_resolves_and_survives_relocation_without_origin(self):
        managed = project_component_lock_managed_graph(
            self.fixture.snapshot.authority.lock, self.revision
        )
        source_content, source_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )
        self.fixture.fixture.remove_source()
        projections = []
        for index in range(2):
            with self.materialize() as (sdk,):
                evidence = sdk.dependencies
                projections.append(evidence.identity)
                observation = evidence.observation
                wire = evidence.to_dict()
                self.assertNotIn(str(self.parent), json.dumps(wire))
                self.assertEqual(self.fixture.fixture.manifest(evidence.identity), wire)
                resolution = evidence.repository_resolution
                self.assertEqual(
                    resolution.source_lock_identity, self.built.resolution.lock.identity
                )
                self.assertEqual(
                    resolution.admission_identity,
                    self.built.resolution.admission.identity,
                )
                self.assertEqual(
                    resolution.resolved_commit,
                    self.built.resolution.lock.resolved_commit,
                )
                package_ref = (
                    f"urn:literate-ai:native-sdk:{sdk.binding.identity.digest}"
                )
                self.assertIn((managed.root_ref, package_ref), observation.edges)
                native = [
                    item
                    for item in observation.components
                    if any(
                        prop["name"] == "literate-ai:native-sdk-relative-path"
                        for prop in item.get("properties", [])
                    )
                ]
                self.assertGreaterEqual(
                    len(native), len(self.built.product.snapshot.native_libraries)
                )
                self.assertTrue(
                    any(
                        item["bom-ref"] != package_ref and item not in native
                        for item in observation.components
                    ),
                    "system and inspector dependencies must remain present",
                )
                destination = self.parent / f"resolved-{index}.json"
                resolver = CycloneDxLifecycleResolver(
                    managed_graph=managed,
                    observer=_NoConsumerBuildObservation(),
                    evidence_path=destination,
                    additional_components=observation.components,
                    additional_edges=observation.edges,
                    repository_resolutions=(resolution,),
                )
                files = {CYCLONEDX_SOURCE_SBOM_PATH: source_content.decode()}
                bundle = canonical_identity(files).uri
                result = resolver.resolve(
                    {
                        "effective_revision_digest": self.revision.uri,
                        "source_bundle_digest": bundle,
                        "files": files,
                    },
                    {
                        "artifact_path": str(self.parent),
                        "artifact_digest": canonical_identity("fixture consumer").uri,
                        "source_bundle_digest": bundle,
                    },
                )
                before, after = validate_resolved_cyclonedx_bom(
                    destination.read_bytes(),
                    source_content=source_content,
                    source_managed_graph=managed,
                    repository_resolutions=(resolution,),
                )
                self.assertEqual(before, source_binding)
                self.assertEqual(after.to_dict(), result["resolved_bom"])
                # Caller edits to returned JSON cannot mutate retained evidence.
                wire["components"][0]["name"] = "caller changed the view"
                self.assertEqual(evidence.identity, projections[-1])
            self.assertFalse(sdk.root.exists())
        self.assertEqual(projections[0], projections[1])

    def test_changed_or_incomplete_loader_evidence_is_rejected(self):
        with self.materialize() as (sdk,):
            runtime = self.service.store.get_manifest(sdk.runtime_observation)
            changed = []
            for key in ("snapshot_identity", "target_identity"):
                value = copy.deepcopy(runtime)
                value[key] = canonical_identity("foreign product").to_dict()
                changed.append(value)
            value = copy.deepcopy(runtime)
            value["native_libraries"] = []
            changed.append(value)
            value = copy.deepcopy(runtime)
            value["edges"] = []
            changed.append(value)
            value = copy.deepcopy(runtime)
            value["edges"].append([self.built.product.snapshot.identity.uri, "absent"])
            changed.append(value)
            value = copy.deepcopy(runtime)
            value["components"].append(value["components"][0])
            changed.append(value)
            value = copy.deepcopy(runtime)
            ref = value["native_libraries"][0]["component"]
            next(item for item in value["components"] if item["bom-ref"] == ref)[
                "hashes"
            ] = []
            changed.append(value)
            for value in changed:
                with self.subTest(changed=value.keys()), self.assertRaises(ValueError):
                    project_native_sdk_dependencies(
                        sdk.binding, root=sdk.root, runtime=value
                    )
