"""Counterexamples for native SDK observation; real images are tested by the builder."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.native_sdk_custody import capture_native_sdk
from literate_ai.adapters.native_sdk_runtime import (
    _pe_architecture,
    observe_native_sdk_runtime,
)
from literate_ai.contracts.executable_components.commands import (
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.storage.cas import FileSystemCAS


def pe_header(machine=0x8664):
    data = bytearray(88)
    data[:2] = b"MZ"
    data[60:64] = (64).to_bytes(4, "little")
    data[64:68] = b"PE\0\0"
    data[68:70] = machine.to_bytes(2, "little")
    return bytes(data)


class NativeSdkRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sdk-runtime-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sdk = self.root / "sdk"
        (self.sdk / "python/vendor").mkdir(parents=True)
        self.library = self.sdk / "python/vendor/library.bin"
        self.library.write_bytes(pe_header())
        self.system = self.root / "system.bin"
        self.system.write_bytes(pe_header())
        self.store = FileSystemCAS(self.root / "cas")
        self.snapshot = capture_native_sdk(
            self.sdk,
            store=self.store,
            source_lock_identity=canonical_identity("source"),
            recipe_identity=canonical_identity("recipe"),
            target_identity=canonical_identity("target"),
            license_identity=canonical_identity("license"),
            import_surface=LibraryImportSurface(
                "python",
                "vendor",
                (
                    LibraryCapabilityImport(
                        "vendor.api",
                        canonical_identity("interface"),
                        "vendor",
                        ("call",),
                    ),
                ),
            ),
            import_root="python",
            native_libraries=("python/vendor/library.bin",),
        )

    def observation(self, operating_system="linux"):
        path_name = {"linux": "elf", "macos": "macho", "windows": "pe"}[
            operating_system
        ]
        components = []
        for ref, path in (("sdk", self.library), ("system", self.system)):
            properties = [{"name": f"literate-ai:{path_name}-path", "value": str(path)}]
            if operating_system == "linux":
                properties.append(
                    {
                        "name": "literate-ai:elf-architecture",
                        "value": "ELF64/Advanced Micro Devices X86-64",
                    }
                )
            if operating_system == "macos":
                properties.append(
                    {"name": "literate-ai:macho-uuid:x86_64", "value": "fixture-uuid"}
                )
            components.append(
                {
                    "type": "file",
                    "bom-ref": ref,
                    "hashes": [
                        {"alg": "SHA-256", "content": self.store.put_file(path).digest}
                    ],
                    "properties": properties,
                }
            )
        return HostDependencyObservation(
            tuple(components), ((self.snapshot.identity.uri, "sdk"), ("sdk", "system"))
        )

    def observe(self, observation=None, operating_system="linux", **overrides):
        arguments = dict(
            expected_identity=self.snapshot.identity,
            target_identity=self.snapshot.target_identity,
            operating_system=operating_system,
            architecture="x86_64",
            root=self.sdk,
            store=self.store,
        )
        arguments.update(overrides)
        host = {"linux": "linux", "macos": "darwin", "windows": "win32"}[
            operating_system
        ]
        with (
            mock.patch("literate_ai.adapters.native_sdk_runtime.sys.platform", host),
            mock.patch(
                "literate_ai.adapters.native_sdk_runtime.platform.machine",
                return_value="x86_64",
            ),
            mock.patch(
                "literate_ai.adapters.native_sdk_runtime.PortableHostDependencyObserver.observe",
                return_value=observation or self.observation(operating_system),
            ),
        ):
            return observe_native_sdk_runtime(self.snapshot, **arguments)

    def test_three_platform_observations_bind_exact_sdk_target_and_dependency_edges(
        self,
    ):
        for operating_system in ("linux", "macos", "windows"):
            with self.subTest(operating_system=operating_system):
                evidence = self.store.get_manifest(
                    self.observe(operating_system=operating_system)
                )
                self.assertEqual(
                    evidence["snapshot_identity"], self.snapshot.identity.to_dict()
                )
                self.assertEqual(
                    evidence["target_identity"], self.snapshot.target_identity.to_dict()
                )
                self.assertEqual(
                    evidence["native_libraries"],
                    [{"path": "python/vendor/library.bin", "component": "sdk"}],
                )
                self.assertIn(["sdk", "system"], evidence["edges"])

    def test_stale_snapshot_target_and_wrong_host_are_rejected(self):
        for changes in (
            {"expected_identity": canonical_identity("stale")},
            {"target_identity": canonical_identity("other target")},
            {"architecture": "arm64"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.observe(**changes)

    def test_missing_ambiguous_or_wrong_hash_native_images_are_rejected(self):
        original = self.observation()
        wrong_hash = copy.deepcopy(original.components)
        wrong_hash[0]["hashes"][0]["content"] = "0" * 64
        wrong_path = copy.deepcopy(original.components)
        wrong_path[0]["properties"][0]["value"] = str(self.root / "another")
        for components in (
            wrong_hash,
            wrong_path,
            (*original.components, original.components[0]),
        ):
            with self.subTest(components=components), self.assertRaises(ValueError):
                self.observe(
                    HostDependencyObservation(tuple(components), original.edges)
                )

    def test_dangling_and_disconnected_graphs_are_rejected(self):
        observed = self.observation()
        for edges in (
            ((self.snapshot.identity.uri, "sdk"), ("sdk", "missing")),
            ((self.snapshot.identity.uri, "sdk"),),
        ):
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                self.observe(HostDependencyObservation(observed.components, edges))

    def test_wrong_architecture_in_a_transitive_library_is_rejected(self):
        observed = self.observation()
        observed.components[1]["properties"][1]["value"] = "ELF64/AArch64"
        with self.assertRaisesRegex(ValueError, "architecture differs"):
            self.observe(observed)
        self.system.write_bytes(pe_header(0xAA64))
        with self.assertRaisesRegex(ValueError, "architecture differs"):
            self.observe(operating_system="windows")

    def test_unavailable_delayed_import_is_not_an_admitted_sdk_closure(self):
        observed = self.observation("windows")
        observed.components[0]["properties"].append(
            {"name": "literate-ai:pe-delay-import-unavailable", "value": "missing.dll"}
        )
        with self.assertRaisesRegex(ValueError, "unavailable delayed import"):
            self.observe(observed, operating_system="windows")

    def conditional_host_observation(self):
        observed = self.observation("windows")
        observed.components[1]["properties"].extend(
            [
                {
                    "name": "literate-ai:pe-delay-import-unavailable",
                    "value": "optional.dll",
                },
                {
                    "name": "literate-ai:pe-import-edge",
                    "value": json.dumps({"name": "optional.dll", "delay_load": True}),
                },
            ]
        )
        return observed

    def test_external_delayed_absence_is_retained_and_identity_bound(self):
        observed = self.conditional_host_observation()
        retained = self.observe(observed, operating_system="windows")
        record = self.store.get_manifest(retained)
        self.assertEqual(record["components"], list(observed.components))
        self.assertEqual(record["edges"], [list(edge) for edge in observed.edges])
        changed = copy.deepcopy(observed)
        changed.components[1]["properties"] = changed.components[1]["properties"][:-2]
        self.assertNotEqual(self.observe(changed, operating_system="windows"), retained)

    def test_external_absence_requires_delay_metadata_and_verified_importer_bytes(self):
        for defect in (
            "metadata-missing",
            "required",
            "unhashed",
            "changed",
            "invalid",
        ):
            observed = self.conditional_host_observation()
            component = observed.components[1]
            if defect == "metadata-missing":
                component["properties"].pop()
            elif defect == "required":
                component["properties"][-1]["value"] = json.dumps(
                    {"name": "optional.dll", "delay_load": False}
                )
            elif defect == "unhashed":
                component["hashes"] = []
            elif defect == "changed":
                component["hashes"][0]["content"] = "0" * 64
            else:
                component["properties"][-1]["value"] = json.dumps(
                    {"name": "optional.dll", "delay_load": "true"}
                )
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.observe(observed, operating_system="windows")

    def test_sdk_owned_delayed_absence_is_rejected_even_with_delay_metadata(self):
        observed = self.conditional_host_observation()
        observed.components[0]["properties"].extend(
            observed.components[1]["properties"][-2:]
        )
        with self.assertRaisesRegex(ValueError, "unavailable delayed import"):
            self.observe(observed, operating_system="windows")

    def test_unavailable_api_set_host_requires_external_delay_only_importers(self):
        contract = "api-ms-win-optional-example-l1-1-0.dll"
        for importer, delayed, metadata in (
            ("system", True, True),
            ("sdk", True, True),
            ("system", False, True),
            ("system", True, False),
        ):
            observed = self.observation("windows")
            component = observed.components[0 if importer == "sdk" else 1]
            if metadata:
                component["properties"].append(
                    {
                        "name": "literate-ai:pe-import-edge",
                        "value": json.dumps({"name": contract, "delay_load": delayed}),
                    }
                )
            virtual = {
                "type": "file",
                "bom-ref": "api-set",
                "properties": [
                    {"name": "literate-ai:pe-api-set-contract", "value": contract},
                    {"name": "literate-ai:pe-api-set-host-available", "value": "false"},
                    {
                        "name": "literate-ai:pe-delay-import-only",
                        "value": str(delayed).lower(),
                    },
                ],
            }
            observation = HostDependencyObservation(
                (*observed.components, virtual),
                (*observed.edges, (importer, "api-set")),
            )
            with self.subTest(importer=importer, delayed=delayed, metadata=metadata):
                if importer == "system" and delayed and metadata:
                    record = self.store.get_manifest(
                        self.observe(observation, operating_system="windows")
                    )
                    self.assertIn(virtual, record["components"])
                else:
                    with self.assertRaisesRegex(ValueError, "unavailable API-set host"):
                        self.observe(observation, operating_system="windows")

    def test_changed_sdk_bytes_are_rejected(self):
        observed = self.observation()
        self.library.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SDK bytes changed"):
            self.observe(observed)

    def test_changed_runtime_dependency_bytes_are_rejected(self):
        observed = self.observation()
        self.system.write_bytes(b"different runtime bytes")
        with self.assertRaisesRegex(ValueError, "runtime library bytes changed"):
            self.observe(observed)

    def test_pe_header_parser_rejects_truncation_bad_offsets_and_unknown_machine(self):
        for machine, architecture in ((0x8664, "x86_64"), (0xAA64, "arm64")):
            self.library.write_bytes(pe_header(machine))
            self.assertEqual(_pe_architecture(self.library), architecture)
        bad_offset = bytearray(pe_header())
        bad_offset[60:64] = (32).to_bytes(4, "little")
        for content in (b"MZ", pe_header()[:70], bytes(bad_offset), pe_header(0xA641)):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.library.write_bytes(content)
                _pe_architecture(self.library)
