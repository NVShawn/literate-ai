"""Portable loader contexts retain exact facts, without merging conflicting files."""

import copy
import json
import unittest

from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.native_sdk_dependencies import merge_sdk_host_observations
from literate_ai.adapters.native_sdk_freebsd import project_freebsd_loader_context
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class NativeSdkFreeBsdContextTests(unittest.TestCase):
    def context(self, root):
        source_ref = "urn:image:" + canonical_identity(root).digest
        record = {
            "image": source_ref,
            "program": source_ref,
            "kind": "filter",
            "name": "libsys.so.7",
            "target": "system-image",
            "loading": "potential",
        }
        state = {
            "loader": {"files": []},
            "paths": {"LD_LIBRARY_PATH": root + "/lib"},
            "filtees": [record],
        }
        identity = canonical_identity(state)
        component = {
            "type": "data",
            "name": "freebsd-loader-configuration",
            "bom-ref": "urn:literate-ai:freebsd-loader-state:" + identity.digest,
            "version": identity.digest,
            "hashes": [{"alg": "SHA-256", "content": identity.digest}],
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "system"},
                {"name": "literate-ai:dependency-scope", "value": "runtime"},
                {
                    "name": "literate-ai:freebsd-loader-state",
                    "value": canonical_json_bytes(state).decode(),
                },
                {
                    "name": "literate-ai:freebsd-filtee",
                    "value": canonical_json_bytes(record).decode(),
                },
            ],
        }
        return component, {source_ref: "sdk-image", "system-image": "system-image"}

    def project(self, component, refs, root):
        return project_freebsd_loader_context(
            component, refs=refs, native_refs=frozenset(refs), root_prefix=root + "/"
        )

    def test_relocation_rebinds_context_hash_paths_and_every_native_reference(self):
        results = []
        for root in ("/tmp/first sdk", "/tmp/other-é-sdk"):
            component, refs = self.context(root)
            result = self.project(component, refs, root)
            self.assertNotEqual(component["bom-ref"], result["bom-ref"])
            state = json.loads(
                next(
                    p["value"]
                    for p in result["properties"]
                    if p["name"] == "literate-ai:freebsd-loader-state"
                )
            )
            self.assertEqual(state["paths"], {"LD_LIBRARY_PATH": "sdk:///lib"})
            self.assertEqual(state["filtees"][0]["image"], "sdk-image")
            self.assertEqual(state["filtees"][0]["program"], "sdk-image")
            self.assertEqual(state["filtees"][0]["target"], "system-image")
            self.assertEqual(
                result["hashes"][0]["content"], canonical_identity(state).digest
            )
            results.append(result)
        self.assertEqual(*results)

    def test_changed_context_or_missing_filter_fact_refuses(self):
        for field in ("version", "hashes", "filtee"):
            with self.subTest(field=field):
                component, refs = self.context("/tmp/sdk")
                if field == "filtee":
                    component["properties"] = component["properties"][:-1]
                else:
                    component[field] = "changed"
                with self.assertRaises(ValueError):
                    self.project(component, refs, "/tmp/sdk")

    def test_unresolved_native_reference_refuses(self):
        component, refs = self.context("/tmp/sdk")
        del refs["system-image"]
        with self.assertRaises(ValueError):
            self.project(component, refs, "/tmp/sdk")

    def test_duplicate_json_key_cannot_hide_in_a_hashed_context(self):
        component, refs = self.context("/tmp/sdk")
        property = component["properties"][-2]
        property["value"] = property["value"][:-1] + ',"filtees":[]}'
        with self.assertRaises(ValueError):
            self.project(component, refs, "/tmp/sdk")

    def test_actual_file_identity_conflicts_still_refuse(self):
        original = {
            "type": "file",
            "bom-ref": "same-image",
            "name": "lib",
            "hashes": [{"alg": "SHA-256", "content": "a" * 64}],
            "properties": [],
        }
        altered = copy.deepcopy(original)
        altered["hashes"][0]["content"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "observations conflict"):
            merge_sdk_host_observations(
                HostDependencyObservation((original,), ()),
                HostDependencyObservation((altered,), ()),
            )
