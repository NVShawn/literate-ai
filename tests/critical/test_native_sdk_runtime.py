"""Counterexamples for native SDK observation; real images are tested by the builder."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.native_sdk_custody import capture_native_sdk
from literate_ai.adapters.native_sdk_runtime import (
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
