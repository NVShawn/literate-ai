"""Contracts for composed Flavor host-toolchain installation."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import host_install
from literate_ai.adapters.host_install import (
    HostInstallError,
)
from literate_ai.bootstrap.host_install_requirements import (
    HostInstallTarget,
    HostManagedArtifact,
    detect_host_install_target,
)


def _artifact(archive: Path, *, digest: str | None = None) -> HostManagedArtifact:
    return HostManagedArtifact(
        "urn:test:artifact",
        "fixture agent",
        "1.0.0",
        ("codex",),
        "https://example.invalid/fixture.tar.gz",
        "sha256",
        digest or hashlib.sha256(archive.read_bytes()).hexdigest(),
        "tar.gz",
        "bundle",
        (".",),
        "agent-source",
        "codex",
    )


class HostInstallContractTests(unittest.TestCase):
    def test_arm64_images_and_companions_cannot_be_proven_by_versions(self):
        """Foreign images never become native proof or trigger silent replacement.

        PE files and the OS command runner are deliberate counterexample fixtures,
        not an executable Windows program or native platform qualification.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "flavors"
            repository = Path(__file__).resolve().parents[2]
            shutil.copytree(repository / "flavors", catalog)
            target = HostInstallTarget("windows", "arm64")
            path, sbom = host_install.load_host_install_sbom(catalog, target)
            artifact = next(a for a in sbom.artifacts if "codex" in a.capabilities)
            bundle = root / "bundle"
            executable = bundle / artifact.executable_path
            required = [bundle / name for name in artifact.required_runtime_paths]

            def image(path, machine):
                content = bytearray(88)
                content[:2] = b"MZ"
                content[60:64] = (64).to_bytes(4, "little")
                content[64:68] = b"PE\0\0"
                content[68:70] = machine.to_bytes(2, "little")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            installs = []

            def runner(arguments, **kwargs):
                if "install" in arguments:
                    installs.append(arguments)
                    self.fail("architecture refusal must not replace an existing tool")
                output = (
                    "\n".join(p.manager_package for p in sbom.packages)
                    if "list" in arguments
                    else "codex 0.153.0"
                )
                return subprocess.CompletedProcess(arguments, 0, output, "")

            def locator(command, _path):
                if command in {"codex", "codex.exe", "codex.cmd"}:
                    return str(selected_executable)
                if command == "winget.exe":
                    return str(root / "winget.exe")
                return None

            environment = {"CODING_CLI": "codex", "LITAI_INSTALL_DEPENDENCIES": "yes"}
            for variant in (
                "native",
                "foreign-main",
                "foreign-helper",
                "missing-helper",
                "script-launcher",
            ):
                with self.subTest(variant=variant):
                    selected_executable = executable
                    image(executable, 0xAA64)
                    for companion in required:
                        companion.parent.mkdir(parents=True, exist_ok=True)
                        if companion.suffix == ".json":
                            companion.write_text("{}")
                        else:
                            image(companion, 0xAA64)
                    helper = next(p for p in required if p.suffix == ".exe")
                    if variant == "foreign-main":
                        image(executable, 0x8664)
                    elif variant == "foreign-helper":
                        image(helper, 0x8664)
                    elif variant == "missing-helper":
                        helper.unlink()
                    elif variant == "script-launcher":
                        selected_executable = bundle / "codex.cmd"
                        selected_executable.write_text(
                            "@echo off\necho codex 0.153.0\n"
                        )
                    before = {
                        p.relative_to(bundle): p.read_bytes()
                        for p in bundle.rglob("*")
                        if p.is_file()
                    }
                    report = host_install.observe_host_install_dependencies(
                        sbom_path=path,
                        sbom=sbom,
                        environment=environment,
                        runner=runner,
                        locator=locator,
                        managed_tool_root=root / "managed",
                    )
                    agent = next(
                        item
                        for item in report.observations
                        if item.requirement.capability == "codex"
                    )
                    self.assertTrue(agent.probe_satisfied)
                    self.assertEqual(agent.ready, variant == "native")
                    wire = agent.to_dict()["image_architecture"]
                    self.assertEqual(wire["verified"], variant == "native")
                    if variant != "script-launcher":
                        self.assertEqual(len(wire["images"]), len(required))
                    if variant != "native":
                        with self.assertRaises(HostInstallError) as refused:
                            host_install.ensure_host_install_dependencies(
                                sbom_root=catalog,
                                target=target,
                                environment=environment,
                                runner=runner,
                                locator=locator,
                                interactive=False,
                                managed_tool_root=root / "managed",
                            )
                        self.assertEqual(
                            refused.exception.code,
                            "host-install.native-image-unverified",
                        )
                        self.assertIn("codex", refused.exception.message)
                    self.assertEqual(installs, [])
                    self.assertEqual(
                        before,
                        {
                            p.relative_to(bundle): p.read_bytes()
                            for p in bundle.rglob("*")
                            if p.is_file()
                        },
                    )

    def test_inaccessible_native_search_path_reports_typed_error(self) -> None:
        # Native host readiness must fail closed when declared tool paths cannot
        # be inspected, with a typed error the installer and doctor can report.
        root = Path(__file__).resolve().parents[2]
        sbom_path, sbom = host_install.load_host_install_sbom(
            root / "flavors", HostInstallTarget("windows", "x86_64")
        )
        with tempfile.TemporaryDirectory() as temporary:
            environment = {"PATH": "existing-tools", "LOCALAPPDATA": temporary}
            for failure in (PermissionError("access denied"), OSError("unavailable")):
                with (
                    self.subTest(failure=type(failure).__name__),
                    mock.patch.object(Path, "is_dir", side_effect=failure),
                    self.assertRaises(HostInstallError) as raised,
                ):
                    host_install.observe_host_install_dependencies(
                        sbom_path=sbom_path,
                        sbom=sbom,
                        environment=environment,
                        managed_tool_root=Path(temporary) / "tools",
                    )
                self.assertEqual(raised.exception.code, "host-install.path-unreadable")
                self.assertIn("account with access", raised.exception.message)
                self.assertEqual(environment["PATH"], "existing-tools")

    def test_managed_artifact_verifies_digest_and_normalizes_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "fixture.tar.gz"
            content = b"fixture executable"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("bundle/agent-source")
                info.size = len(content)
                info.mode = 0o755
                bundle.addfile(info, io.BytesIO(content))
            artifact = _artifact(archive)

            def download(_url: str, destination: Path) -> None:
                shutil.copy2(archive, destination)

            installed = host_install._install_managed_artifact(
                artifact, tool_root=root / "tools", downloader=download
            )
            self.assertEqual((installed / "codex").read_bytes(), content)
            self.assertTrue(
                host_install._artifact_manifest_matches(installed, artifact)
            )

            manifest_path = installed / ".literate-ai-artifact.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("required_runtime_paths")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertTrue(
                host_install._artifact_manifest_matches(installed, artifact)
            )

    def test_managed_artifact_rejects_digest_mismatch_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe.tar.gz"
            with tarfile.open(safe, "w:gz") as bundle:
                info = tarfile.TarInfo("bundle/agent-source")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))

            def download_safe(_url: str, destination: Path) -> None:
                shutil.copy2(safe, destination)

            with self.assertRaises(HostInstallError) as digest_error:
                host_install._install_managed_artifact(
                    _artifact(safe, digest="0" * 64),
                    tool_root=root / "digest-tools",
                    downloader=download_safe,
                )
            self.assertEqual(
                digest_error.exception.code, "host-install.artifact-digest-mismatch"
            )

            unsafe = root / "unsafe.tar.gz"
            with tarfile.open(unsafe, "w:gz") as bundle:
                info = tarfile.TarInfo("../escape")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))

            def download_unsafe(_url: str, destination: Path) -> None:
                shutil.copy2(unsafe, destination)

            with self.assertRaises(HostInstallError) as traversal_error:
                host_install._install_managed_artifact(
                    _artifact(unsafe),
                    tool_root=root / "traversal-tools",
                    downloader=download_unsafe,
                )
            self.assertEqual(
                traversal_error.exception.code, "host-install.archive-traversal"
            )


class WindowsArm64HostInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.managed_root = Path(temporary.name) / "managed"
        self.root = Path(__file__).resolve().parents[2]
        self.target = detect_host_install_target(system="Windows", machine="ARM64")
        self.flavors = (
            "lang-python",
            "lang-cpp",
            "lang-rust",
            "build-cargo",
            "lang-typescript",
            "build-cmake",
        )

    def test_exact_target_template_and_managed_runtime_closure(self):
        path, sbom = host_install.load_host_install_sbom(
            self.root / "flavors", self.target, flavor_names=self.flavors
        )
        self.assertEqual(sbom.target, HostInstallTarget("windows", "arm64"))
        self.assertEqual(
            path.read_bytes(),
            (
                self.root
                / "src/literate_ai/project_template"
                / path.relative_to(self.root)
            ).read_bytes(),
        )
        x64_path, x64 = host_install.load_host_install_sbom(
            self.root / "flavors", HostInstallTarget("windows", "x86_64")
        )
        self.assertNotEqual(path, x64_path)
        self.assertEqual(x64.target.architecture, "x86_64")
        self.assertNotIn("--architecture", x64.package_manager.install_arguments)
        self.assertEqual(len(sbom.artifacts), 1)
        codex = sbom.artifacts[0]
        self.assertEqual(codex.capabilities, ("codex",))
        self.assertIn("aarch64-pc-windows-msvc", codex.url)
        self.assertEqual(codex.executable_path, "bin/codex.exe")
        self.assertEqual(len(codex.required_runtime_paths), 5)
        for requirement in sbom.packages:
            args = (
                requirement.install_arguments or sbom.package_manager.install_arguments
            )
            if requirement.manager_package == "Microsoft.VisualStudio.2022.BuildTools":
                self.assertNotIn("--architecture", args)
                self.assertIn(
                    "Microsoft.VisualStudio.Component.VC.Tools.ARM64",
                    args[args.index("--override") + 1],
                )
            else:
                self.assertEqual(args[args.index("--architecture") + 1], "arm64")
        self.assertEqual(
            {
                p.manager_package
                for p in sbom.packages
                if p.capability in {"git", "git-lfs", "bash"}
            },
            {"Git.Git"},
        )
        self.assertEqual(
            next(
                p.manager_package for p in sbom.packages if p.capability == "vcruntime"
            ),
            "Microsoft.VCRedist.2015+.arm64",
        )

    def test_unrealized_capabilities_and_providers_refuse_without_dispatch(self):
        for flavor, capability in (
            ("build-make", "make"),
            ("build-bazel", "bazel"),
            ("lang-elixir", "elixir"),
            ("package-npm", "npm"),
        ):
            with (
                self.subTest(flavor=flavor),
                self.assertRaises(host_install.HostInstallError) as raised,
            ):
                host_install.load_host_install_sbom(
                    self.root / "flavors", self.target, flavor_names=(flavor,)
                )
            self.assertEqual(
                raised.exception.code, "host-install.capability-closure-invalid"
            )
            self.assertIn(capability, raised.exception.message)
        _, sbom = host_install.load_host_install_sbom(
            self.root / "flavors", self.target
        )

        def forbidden(*args, **kwargs):
            self.fail(
                "unsupported provider must refuse before discovery or acquisition"
            )

        for provider in ("claude", "cursor-agent", "opencode"):
            with (
                self.subTest(provider=provider),
                self.assertRaises(host_install.HostInstallError) as raised,
            ):
                host_install.ensure_host_install_dependencies(
                    sbom_root=self.root / "flavors",
                    target=self.target,
                    environment={"CODING_CLI": provider},
                    runner=forbidden,
                    locator=forbidden,
                    interactive=False,
                    managed_tool_root=self.managed_root,
                )
            self.assertEqual(
                raised.exception.code, "host-install.coding-agent-unsupported"
            )

    def ports(self, directory, *, missing_compiler=False):
        root = Path(directory).resolve()
        system = root / "System32"
        system.mkdir()

        def image(path):
            # Controlled OS counterexamples, never native Windows qualification.
            content = bytearray(88)
            content[:2] = b"MZ"
            content[60:64] = (64).to_bytes(4, "little")
            content[64:68] = b"PE\0\0"
            content[68:70] = (0xAA64).to_bytes(2, "little")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        for name in ("MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll"):
            image(system / name)
        tools = root / "tools"
        for name in (
            "git",
            "git-lfs",
            "bash",
            "node",
            "rustc",
            "cargo",
            "cmake",
            "winget",
        ):
            image(tools / name)
        if not missing_compiler:
            image(tools / "cl")
        python = tools / "python.exe"
        runtime = tools / "python-runtime.dll"
        image(python)
        image(runtime)
        self.enterContext(
            mock.patch.object(host_install.sys, "executable", str(python))
        )
        self.enterContext(
            mock.patch.object(
                host_install, "_python_runtime_image", return_value=runtime
            )
        )
        _, sbom = host_install.load_host_install_sbom(
            self.root / "flavors", self.target, flavor_names=self.flavors
        )
        artifact = next(item for item in sbom.artifacts if "codex" in item.capabilities)
        bundle = root / "bundle"
        executable = bundle / artifact.executable_path
        image(executable)
        for relative in artifact.required_runtime_paths:
            path = bundle / relative
            if path.suffix == ".json":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            else:
                image(path)
        environment = {
            "CODING_CLI": "codex",
            "LITAI_INSTALL_DEPENDENCIES": "no",
            "PATH": str(root / "tools"),
            "SystemRoot": str(root),
            "ProgramFiles": str(root / "programs"),
            "ProgramW6432": str(root / "programs"),
        }
        versions = {
            "git": "git version 2.55.0",
            "git-lfs": "git-lfs/3.7.1",
            "bash": "GNU bash 5.2",
            "node": "v24.13.1",
            "rustc": "rustc 1.94.0",
            "cargo": "cargo 1.94.0",
            "cmake": "cmake version 4.2.3",
            "cl": "Microsoft C/C++ 19.44",
            "codex": "codex-cli 0.153.0",
        }
        installs = []

        def name(command):
            return Path(str(command).replace("\\", "/")).name.removesuffix(".exe")

        def locator(command, path):
            key = name(command)
            if key == "codex":
                return str(executable)
            if key == "cl" and missing_compiler:
                return None
            return str(root / "tools" / key) if key in {*versions, "winget"} else None

        def runner(argv, **kwargs):
            if len(argv) > 1 and argv[1] == "list":
                package = argv[argv.index("--id") + 1]
                missing = (
                    missing_compiler
                    and package == "Microsoft.VisualStudio.2022.BuildTools"
                )
                return subprocess.CompletedProcess(
                    argv, 1 if missing else 0, "" if missing else package, ""
                )
            if len(argv) > 1 and argv[1] == "install":
                installs.append(argv)
                return subprocess.CompletedProcess(
                    argv, 1, "", "No applicable ARM64 installer found"
                )
            if str(argv[0]) == sys.executable:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, versions[name(argv[0])], "")

        return environment, locator, runner, installs

    def test_ready_tools_are_preserved_without_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, locator, runner, installs = self.ports(directory)
            # The native ARM64 baseline coexists with this unrelated x64 DLL.
            system = Path(directory) / "System32"
            unrelated = system / "VCRUNTIME140_1.dll"
            content = bytearray(unrelated.read_bytes())
            content[68:70] = (0x8664).to_bytes(2, "little")
            unrelated.write_bytes(content)
            report = host_install.ensure_host_install_dependencies(
                sbom_root=self.root / "flavors",
                target=self.target,
                flavor_names=self.flavors,
                environment=environment,
                locator=locator,
                runner=runner,
                interactive=False,
                managed_tool_root=Path(directory) / "managed",
                downloader=lambda *a: self.fail("ready tools must not download"),
            )
            self.assertTrue(report.ready)
            self.assertEqual(report.installed, ())
            self.assertEqual(installs, [])
            self.assertEqual(report.coding_agent, "codex")
            self.assertEqual(report.to_dict()["target"]["architecture"], "arm64")
            sbom_path, sbom = host_install.load_host_install_sbom(
                self.root / "flavors", self.target, flavor_names=self.flavors
            )
            for name in ("MSVCP140.dll", "VCRUNTIME140.dll"):
                selected = system / name
                original = selected.read_bytes()
                for variant in ("missing", "foreign"):
                    with self.subTest(selected=name, variant=variant):
                        if variant == "missing":
                            selected.unlink()
                        else:
                            foreign = bytearray(original)
                            foreign[68:70] = (0x8664).to_bytes(2, "little")
                            selected.write_bytes(foreign)
                        observation = host_install.observe_host_install_dependencies(
                            sbom_path=sbom_path,
                            sbom=sbom,
                            environment=environment,
                            locator=locator,
                            runner=runner,
                            managed_tool_root=Path(directory) / "managed",
                        )
                        runtime = next(
                            item
                            for item in observation.observations
                            if item.requirement.capability == "vcruntime"
                        )
                        self.assertFalse(runtime.ready)
                        self.assertFalse(observation.ready)
                        self.assertEqual(installs, [])
                        selected.write_bytes(original)

    def test_missing_required_compiler_never_falls_back_to_x64(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, locator, runner, installs = self.ports(
                directory, missing_compiler=True
            )
            for consent, install_status in (("no", 1), ("yes", 1), ("yes", 0)):
                environment["LITAI_INSTALL_DEPENDENCIES"] = consent
                previous_installs = len(installs)

                def installer(argv, *, status=install_status, **kwargs):
                    result = runner(argv, **kwargs)
                    if len(argv) > 1 and argv[1] == "install":
                        return subprocess.CompletedProcess(argv, status, "", "")
                    return result

                with (
                    self.subTest(consent=consent, install_status=install_status),
                    self.assertRaises(host_install.HostInstallError) as raised,
                ):
                    host_install.ensure_host_install_dependencies(
                        sbom_root=self.root / "flavors",
                        target=self.target,
                        flavor_names=self.flavors,
                        environment=environment,
                        locator=locator,
                        runner=installer,
                        interactive=False,
                        managed_tool_root=Path(directory) / "managed",
                        downloader=lambda *a: self.fail(
                            "native requirement must not replace the agent"
                        ),
                    )
                if consent == "no":
                    self.assertEqual(
                        raised.exception.code, "host-install.dependencies-declined"
                    )
                    self.assertEqual(installs, [])
                else:
                    self.assertEqual(len(installs), previous_installs + 1)
                    args = installs[-1]
                    self.assertNotIn("--architecture", args)
                    self.assertIn(
                        "Microsoft.VisualStudio.Component.VC.Tools.ARM64",
                        args[args.index("--override") + 1],
                    )


if __name__ == "__main__":
    unittest.main()
