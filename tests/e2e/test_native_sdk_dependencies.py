"""Real relocated SDK products preserve repository and native CycloneDX evidence."""

from __future__ import annotations

import json
import unittest

from literate_ai.adapters.dependencies import (
    CycloneDxLifecycleResolver,
    build_cyclonedx_bom,
    validate_resolved_cyclonedx_bom,
)
from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxLifecycle,
    canonical_identity,
)
from literate_ai.contracts.sbom import (
    project_component_lock_managed_graph,
)
from tests.support import (
    fixtures_test_native_sdk_source_build as test_native_sdk_source_build,
)


class _NoConsumerBuildObservation:
    """The test supplies real SDK observations, without claiming a consumer build."""

    def observe(self, build, *, root_ref):
        return HostDependencyObservation((), ())


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
