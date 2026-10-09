"""Typed Mix source and source-to-resolved BOM custody, without native execution."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from literate_ai.adapters.dependencies import (
    CycloneDxLifecycleResolver,
    build_cyclonedx_bom,
    validate_resolved_cyclonedx_bom,
)
from literate_ai.adapters.dependencies.acquisition import _generated_lock_projection
from literate_ai.adapters.dependencies.mix_lock import MixLock
from literate_ai.adapters.dependencies.mix_resolution import (
    MixDependencyEvidence,
    project_mix_evidence,
)
from literate_ai.adapters.dependencies.mix_source import prepare_mix_source_authority
from literate_ai.adapters.dependencies.types import (
    DependencyObservationError,
    HostDependencyObservation,
)
from literate_ai.contracts import CycloneDxLifecycle, canonical_identity
from tests.unit.test_dependency_lifecycle import _managed_graph
from tests.unit.test_elixir_mix_dependencies import _entry, _lock, _project


def _fixture():
    graph = _managed_graph()
    component = {
        "type": "library",
        "bom-ref": "hex-decimal",
        "name": "decimal",
        "purl": "pkg:hex/decimal",
        "isExternal": True,
        "versionRange": "vers:hex/>=2.0.0|<3.0.0",
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "package"},
            {"name": "literate-ai:dependency-scope", "value": "runtime"},
        ],
    }
    bom, _ = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=graph,
        additional_components=(component,),
        additional_edges=((graph.root_ref, component["bom-ref"]),),
    )
    files = {
        "source/mix-project.json": json.dumps(_project().to_dict()),
        "source/main.exs": "IO.puts(1)\n",
        "source/.literate/sbom.cdx.json": bom.decode(),
    }
    authority = prepare_mix_source_authority(files)
    lock = MixLock.from_bytes(_lock(_entry()), project=authority.project)
    observed = {
        "type": "library",
        "bom-ref": "pkg:hex/decimal@2.3.0",
        "name": "decimal",
        "purl": "pkg:hex/decimal@2.3.0",
        "version": "2.3.0",
        "hashes": [{"alg": "SHA-256", "content": "b" * 64}],
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "package"},
            {
                "name": "literate-ai:mix-runtime-tree-identity",
                "value": canonical_identity({"payload": 1}).uri,
            },
        ],
    }
    evidence = MixDependencyEvidence(
        authority,
        canonical_identity({"fixture": "fresh-mix-evidence"}),
        lock,
        HostDependencyObservation((observed,), (("@root", observed["bom-ref"]),)),
    )
    artifact = {
        "files": files,
        "effective_revision_digest": canonical_identity({"revision": 1}).uri,
        "source_bundle_digest": canonical_identity(files).uri,
    }
    build = {
        "source_bundle_digest": artifact["source_bundle_digest"],
        "artifact_digest": canonical_identity({"artifact": 1}).uri,
    }
    return graph, bom, authority, evidence, artifact, build


class MixSourceAuthorityTests(unittest.TestCase):
    def test_no_process_and_all_source_bytes_bind_authority(self):
        _, _, authority, _, artifact, _ = _fixture()
        files = dict(artifact["files"])
        files["source/main.exs"] += "# changed\n"
        self.assertNotEqual(prepare_mix_source_authority(files), authority)
        self.assertEqual(
            _generated_lock_projection(
                artifact["files"], mix_source_authority=authority
            ).packages,
            (),
        )

    def test_default_resolution_remains_fail_closed(self):
        _, _, _, _, artifact, _ = _fixture()
        with self.assertRaisesRegex(DependencyObservationError, "lifecycle-owned"):
            _generated_lock_projection(artifact["files"])

    def test_raw_locks_manifests_nested_intent_and_path_aliases_fail(self):
        _, _, _, _, artifact, _ = _fixture()
        for path in (
            "source/mix.lock",
            "source/mix.exs",
            "source/.iex.exs",
            "source/nested/mix-project.json",
            "source/../mix-project.json",
            "source/MIX.EXS",
        ):
            files = {**artifact["files"], path: "%{}"}
            with self.subTest(path=path), self.assertRaises(DependencyObservationError):
                prepare_mix_source_authority(files)

    def test_changed_authority_rejected_before_acquisition(self):
        _, _, authority, _, artifact, _ = _fixture()
        files = {**artifact["files"], "source/main.exs": "changed"}
        with self.assertRaises(DependencyObservationError):
            _generated_lock_projection(files, mix_source_authority=authority)


class MixResolutionTests(unittest.TestCase):
    def project(self, bom, authority, evidence):
        return project_mix_evidence(
            bom, root_ref=_managed_graph().root_ref, source=authority, evidence=evidence
        )

    def test_exact_graph_remapped_to_source_refs(self):
        graph, bom, authority, evidence, _, _ = _fixture()
        result = self.project(bom, authority, evidence)
        self.assertEqual(result.edges, ((graph.root_ref, "hex-decimal"),))
        self.assertEqual(result.components[0]["version"], "2.3.0")
        self.assertEqual(
            result.components[0]["hashes"], evidence.observation.components[0]["hashes"]
        )

    def test_observed_coordinate_version_hash_inventory_and_graph_fail(self):
        _, bom, authority, evidence, _, _ = _fixture()
        for changes in (
            {"purl": "pkg:pypi/decimal@2.3.0"},
            {"version": "9.0.0"},
            {"hashes": []},
        ):
            component = {**evidence.observation.components[0], **changes}
            bad = replace(
                evidence,
                observation=replace(evidence.observation, components=(component,)),
            )
            with (
                self.subTest(changes=changes),
                self.assertRaises(DependencyObservationError),
            ):
                self.project(bom, authority, bad)
        for observation in (
            HostDependencyObservation((), ()),
            replace(evidence.observation, edges=()),
            replace(evidence.observation, edges=(("@root", "unknown"),)),
        ):
            with self.assertRaises(DependencyObservationError):
                self.project(bom, authority, replace(evidence, observation=observation))

    def test_source_range_hash_inventory_and_graph_fail(self):
        _, bom, authority, evidence, _, _ = _fixture()
        for key, value in (
            ("versionRange", "vers:hex/>=3.0.0"),
            ("hashes", [{"alg": "SHA-256", "content": "c" * 64}]),
            ("purl", "pkg:hex/missing"),
        ):
            document = json.loads(bom)
            document["components"][-1][key] = value
            with self.subTest(key=key), self.assertRaises(DependencyObservationError):
                self.project(json.dumps(document).encode(), authority, evidence)
        document = json.loads(bom)
        document["dependencies"] = []
        with self.assertRaises(DependencyObservationError):
            self.project(json.dumps(document).encode(), authority, evidence)

    def test_source_runtime_claims_and_foreign_purls_fail(self):
        _, bom, authority, evidence, _, _ = _fixture()
        for changes in (
            {"purl": "pkg:hex/decimal@9.0.0"},
            {"purl": "pkg:hex/decimal?repository_url=https://private.invalid"},
            {
                "properties": [
                    {"name": "literate-ai:elixir-module", "value": "Elixir.Absent"}
                ]
            },
            {
                "properties": [
                    {
                        "name": "literate-ai:mix-runtime-tree-identity",
                        "value": canonical_identity({"foreign": True}).uri,
                    }
                ]
            },
        ):
            document = json.loads(bom)
            document["components"][-1].update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaises(DependencyObservationError),
            ):
                self.project(json.dumps(document).encode(), authority, evidence)

    def test_serialized_build_metadata_cannot_replace_callback(self):
        graph, _, authority, _, artifact, build = _fixture()
        with tempfile.TemporaryDirectory() as directory:
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=Mock(
                    observe=Mock(return_value=HostDependencyObservation((), ()))
                ),
                evidence_path=Path(directory) / "resolved.json",
                mix_source_authority=authority,
            )
            resolver.validate_source(artifact)
            with self.assertRaisesRegex(DependencyObservationError, "fresh"):
                resolver.resolve(
                    artifact,
                    {
                        **build,
                        "mix_lifecycle_profile": True,
                        "mix_dependency_evidence": {"verified": True},
                    },
                )
            self.assertFalse(resolver.evidence_path.exists())

    def test_fresh_callback_on_every_resolution_and_bom_continuity(self):
        graph, bom, authority, evidence, artifact, build = _fixture()
        callback = Mock(return_value=evidence)
        with tempfile.TemporaryDirectory() as directory:
            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=Mock(
                    observe=Mock(return_value=HostDependencyObservation((), ()))
                ),
                evidence_path=Path(directory) / "resolved.json",
                mix_source_authority=authority,
                mix_dependency_observer=callback,
            )
            first = resolver.resolve(artifact, build)
            second = resolver.resolve(artifact, build)
            self.assertEqual(callback.call_count, 2)
            self.assertEqual(first, second)
            self.assertEqual(
                first["mix_dependency_evidence_identity"], evidence.identity.uri
            )
            validate_resolved_cyclonedx_bom(
                resolver.evidence_path.read_bytes(),
                source_content=bom,
                source_managed_graph=graph,
            )
            resolved = json.loads(resolver.evidence_path.read_bytes())
            package = next(
                c for c in resolved["components"] if c["bom-ref"] == "hex-decimal"
            )
            self.assertEqual(package["version"], "2.3.0")
            self.assertNotIn("versionRange", package)
            callback.side_effect = DependencyObservationError(
                "fixture.tampered", "payload changed"
            )
            with self.assertRaisesRegex(DependencyObservationError, "payload changed"):
                resolver.resolve(artifact, build)

    def test_foreign_host_or_additional_hex_evidence_rejected(self):
        graph, _, authority, evidence, artifact, build = _fixture()
        for additional in (False, True):
            host = () if additional else evidence.observation.components
            with tempfile.TemporaryDirectory() as directory:
                resolver = CycloneDxLifecycleResolver(
                    managed_graph=graph,
                    observer=Mock(
                        observe=Mock(return_value=HostDependencyObservation(host, ()))
                    ),
                    evidence_path=Path(directory) / "resolved.json",
                    additional_components=evidence.observation.components
                    if additional
                    else (),
                    mix_source_authority=authority,
                    mix_dependency_observer=lambda: evidence,
                )
                with self.assertRaisesRegex(DependencyObservationError, "only"):
                    resolver.resolve(artifact, build)

    def test_wrong_source_evidence_and_duplicate_packages_rejected(self):
        _, bom, authority, evidence, artifact, _ = _fixture()
        changed = prepare_mix_source_authority(
            {**artifact["files"], "source/main.exs": "changed"}
        )
        with self.assertRaises(DependencyObservationError):
            self.project(bom, authority, replace(evidence, source=changed))
        document = json.loads(bom)
        document["components"].append(copy.deepcopy(document["components"][-1]))
        with self.assertRaises(DependencyObservationError):
            self.project(json.dumps(document).encode(), authority, evidence)


if __name__ == "__main__":
    unittest.main()
