"""The reviewed @bazel/bazelisk npm launcher binds only its host binary.

The real npm package ships prebuilt bazelisk binaries for every platform. The
observer once refused every native file before the launcher could admit them, so
the documented `npm install --global @bazel/bazelisk` path always failed closed.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.dependencies import observation
from literate_ai.adapters.dependencies.types import DependencyObservationError

_FOREIGN_MAGIC = {
    "darwin": b"\xcf\xfa\xed\xfe" + b"\0" * 60,
    "linux": b"\x7fELF" + b"\0" * 60,
    "windows": b"MZ" + b"\0" * 62,
}


def _host_predicate():
    if sys.platform == "darwin":
        return observation._is_macho
    if sys.platform.startswith("linux"):
        return observation._is_elf
    return observation._is_pe


def _bazelisk_package(prefix: Path, node: Path) -> tuple[Path, Path]:
    package = prefix / "lib" / "node_modules" / "@bazel" / "bazelisk"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@bazel/bazelisk",
                "version": "1.28.1",
                "bin": {"bazel": "bazelisk.js", "bazelisk": "bazelisk.js"},
            }
        ),
        encoding="utf-8",
    )
    launcher = package / "bazelisk.js"
    launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    host = observation._bazelisk_host_binary_name(node)
    for system in ("darwin", "linux", "windows"):
        for arch in ("amd64", "arm64"):
            name = f"bazelisk-{system}_{arch}" + (".exe" if system == "windows" else "")
            if name == host:
                shutil.copy2(node, package / name)
            else:
                (package / name).write_bytes(_FOREIGN_MAGIC[system])
    return package, launcher


class BazeliskNpmLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        found = shutil.which("node")
        if found is None:
            self.skipTest("node is not on PATH")
        self.node = Path(found).resolve()
        if not _host_predicate()(self.node):
            self.skipTest("node is not a native binary this observer inspects")

    def test_only_the_host_binary_is_a_native_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package, launcher = _bazelisk_package(Path(raw).resolve(), self.node)
            observed = observation._npm_toolchain_launcher_dependencies(
                launcher,
                "bazel",
                root_ref="urn:test:root",
                native_predicate=_host_predicate(),
            )
        assert observed is not None
        _observation, owners, runtime_edges = observed
        host = package.resolve() / observation._bazelisk_host_binary_name(self.node)
        self.assertEqual(set(owners), {str(host)})
        self.assertEqual(len(runtime_edges), 1)

    def test_native_payloads_outside_the_platform_binaries_still_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package, launcher = _bazelisk_package(Path(raw).resolve(), self.node)
            (package / "addon.node").write_bytes(_FOREIGN_MAGIC["linux"])
            with self.assertRaises(DependencyObservationError) as refused:
                observation._npm_toolchain_launcher_dependencies(
                    launcher,
                    "bazel",
                    root_ref="urn:test:root",
                    native_predicate=_host_predicate(),
                )
        self.assertEqual(
            refused.exception.code, "dependencies.npm-native-payload-unsupported"
        )

    def test_a_host_without_a_published_binary_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package, launcher = _bazelisk_package(Path(raw).resolve(), self.node)
            (package / observation._bazelisk_host_binary_name(self.node)).unlink()
            with self.assertRaises(DependencyObservationError) as refused:
                observation._npm_toolchain_launcher_dependencies(
                    launcher,
                    "bazel",
                    root_ref="urn:test:root",
                    native_predicate=_host_predicate(),
                )
        self.assertEqual(
            refused.exception.code, "dependencies.toolchain-launcher-unsupported"
        )

    def test_the_binary_follows_the_node_architecture_not_the_cpu(self) -> None:
        """bazelisk.js selects by os.arch(): an emulated Node picks its own arch."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            system = observation._bazelisk_host_binary_name(self.node).split("_")[0]
            for arch, header in (
                ("amd64", b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\x00"),
                ("arm64", b"\x7fELF\x02\x01" + b"\0" * 12 + b"\xb7\x00"),
            ):
                node = root / f"node-{arch}"
                node.write_bytes(header + b"\0" * 64)
                self.assertEqual(observation._executable_architecture(node), arch)
                suffix = ".exe" if system.endswith("windows") else ""
                self.assertEqual(
                    observation._bazelisk_host_binary_name(node),
                    f"{system}_{arch}{suffix}",
                )
            # A universal Mach-O or unknown header cannot say which binary runs.
            unknown = root / "node-fat"
            unknown.write_bytes(b"\xca\xfe\xba\xbe" + b"\0" * 64)
            with self.assertRaises(DependencyObservationError) as refused:
                observation._bazelisk_host_binary_name(unknown)
            self.assertEqual(
                refused.exception.code, "dependencies.toolchain-launcher-unsupported"
            )


if __name__ == "__main__":
    unittest.main()
