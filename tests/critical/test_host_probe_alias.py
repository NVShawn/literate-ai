"""Host capability probes retain the selected multicall invocation name."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters import host_install
from literate_ai.bootstrap.host_install_requirements import (
    HostInstallTarget,
    UniversalCapabilityRequirement,
)


@unittest.skipIf(os.name == "nt", "POSIX executable/symlink multicall fixture")
class HostProbeAliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        launcher = self.root / "rustup"
        launcher.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "name = os.path.basename(sys.argv[0])\n"
            "versions = {'rustc': 'rustc 1.97.1', 'cargo': 'cargo 1.97.1',\n"
            "            'old-rustc': 'rustc 1.74.0', 'failed-rustc': 'rustc 1.97.1'}\n"
            "print(versions.get(name, 'rustup 1.29.1'))\n"
            "sys.exit(7 if name == 'failed-rustc' else 0)\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        for name in ("rustc", "cargo", "old-rustc", "failed-rustc"):
            (self.root / name).symlink_to(launcher.name)
        self.environment = {**os.environ, "PATH": str(self.root)}

    def requirement(self, candidate):
        return UniversalCapabilityRequirement(
            "urn:test:rust-probe",
            "rustc",
            "Rust compiler",
            ">=1.75,<2",
            "command",
            (candidate,),
            ("--version",),
            "native Rust capability",
        )

    def probe(self, candidate, *, locator=None):
        return host_install._probe_capability(
            self.requirement(candidate),
            environment=self.environment,
            runner=subprocess.run,
            locator=locator or (lambda command, path: shutil.which(command, path=path)),
        )

    def test_real_multicall_aliases_probe_selected_tools_not_rustup(self):
        for tool in ("rustc", "cargo"):
            with self.subTest(tool=tool):
                satisfied, version, executable = self.probe(tool)
                self.assertTrue(satisfied)
                self.assertEqual(version, f"{tool} 1.97.1")
                self.assertEqual(executable, self.root / tool)
                self.assertNotEqual(executable, executable.resolve())
        # A direct manager probe is not valid compiler-version evidence.
        self.assertFalse(self.probe("rustup")[0])

    def test_absolute_fallback_and_relative_locator_preserve_alias(self):
        path = self.root / "rustc"
        satisfied, version, executable = self.probe(str(path), locator=lambda *_: None)
        self.assertTrue(satisfied)
        self.assertEqual(executable, path)
        self.assertEqual(version, "rustc 1.97.1")
        relative = os.path.relpath(path)
        satisfied, _, executable = self.probe("rustc", locator=lambda *_: relative)
        self.assertTrue(satisfied)
        self.assertEqual(executable.name, "rustc")
        self.assertTrue(executable.is_absolute())

    def test_missing_failed_and_incompatible_tools_remain_unsatisfied(self):
        for tool in ("missing-tool", "old-rustc", "failed-rustc"):
            with self.subTest(tool=tool):
                self.assertFalse(self.probe(tool)[0])

    def test_version_success_does_not_fabricate_native_manager_provenance(self):
        _, sbom = host_install.load_host_install_sbom(
            Path(__file__).resolve().parents[2] / "flavors",
            HostInstallTarget("macos", "arm64"),
            flavor_names=("lang-rust", "build-cargo"),
        )
        requirement = next(item for item in sbom.packages if item.capability == "rustc")

        def runner(argv, **kwargs):
            if argv[0] == "/fixture/package-query":
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.run(argv, **kwargs)

        observation = host_install._observe_native_package(
            requirement,
            sbom=sbom,
            query_path="/fixture/package-query",
            environment=self.environment,
            runner=runner,
            locator=lambda command, path: shutil.which(command, path=path),
        )
        self.assertTrue(observation.probe_satisfied)
        self.assertFalse(observation.manager_installed)
        self.assertFalse(observation.ready)


if __name__ == "__main__":
    unittest.main()
