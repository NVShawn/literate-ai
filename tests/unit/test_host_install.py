"""Contracts for composed Flavor host-toolchain installation."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

from literate_ai.adapters import host_install
from literate_ai.adapters.host_install import (
    HostInstallError,
    ensure_host_install_dependencies,
    load_host_install_sbom,
    observe_host_install_dependencies,
)
from literate_ai.bootstrap.host_install_requirements import (
    HostInstallRequirementError,
    HostInstallTarget,
    HostManagedArtifact,
    compose_toolchain_sboms,
    detect_host_install_target,
    parse_base_toolchain_sbom,
    parse_flavor_toolchain_sbom,
    supported_targets,
    version_satisfies,
)

REPOSITORY = Path(__file__).resolve().parents[2]
FLAVORS = REPOSITORY / "flavors"


def _completed(
    arguments: object, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


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
    def test_target_detection_normalizes_os_distribution_and_architecture(self) -> None:
        cases = (
            (
                {"system": "Darwin", "machine": "arm64"},
                HostInstallTarget("macos", "arm64"),
            ),
            (
                {"system": "Windows", "machine": "AMD64"},
                HostInstallTarget("windows", "x86_64"),
            ),
            (
                {
                    "system": "Linux",
                    "machine": "aarch64",
                    "os_release": {"ID": "ubuntu", "ID_LIKE": "debian"},
                },
                HostInstallTarget("linux-debian", "arm64"),
            ),
            (
                {
                    "system": "Linux",
                    "machine": "x86_64",
                    "os_release": {"ID": "fedora", "ID_LIKE": "rhel"},
                },
                HostInstallTarget("linux-fedora", "x86_64"),
            ),
        )
        for arguments, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(detect_host_install_target(**arguments), expected)

    def test_every_toolchain_and_realization_sbom_is_strict_cyclonedx(self) -> None:
        logical = [FLAVORS / "os-base" / "toolchain.cdx.json"]
        logical.extend(sorted(FLAVORS.glob("*/host-toolchain.cdx.json")))
        realizations = sorted(FLAVORS.glob("os-*/host-install/*/*/*.cdx.json"))
        self.assertEqual(len(logical), 13)
        self.assertEqual(len(realizations), 5)
        validator = JsonStrictValidator(SchemaVersion.V1_7)
        base = parse_base_toolchain_sbom(
            json.loads(logical[0].read_text(encoding="utf-8"))
        )
        self.assertEqual(
            {item.capability for item in base.capabilities},
            {
                "python",
                "python-venv",
                "git",
                "git-lfs",
                "codex",
                "claude",
                "cursor-agent",
                "opencode",
            },
        )
        self.assertNotIn("node", {item.capability for item in base.capabilities})
        for path in logical:
            with self.subTest(path=path):
                raw = path.read_text(encoding="utf-8")
                self.assertIsNone(validator.validate_str(raw))
                if path != logical[0]:
                    self.assertTrue(parse_flavor_toolchain_sbom(json.loads(raw)))
        for path in realizations:
            with self.subTest(path=path):
                self.assertIsNone(
                    validator.validate_str(path.read_text(encoding="utf-8"))
                )
        self.assertEqual(
            len(
                supported_targets([path.relative_to(FLAVORS) for path in realizations])
            ),
            5,
        )

    def test_every_codex_realization_installs_the_complete_runtime_package(
        self,
    ) -> None:
        targets = (
            HostInstallTarget("windows", "x86_64"),
            HostInstallTarget("macos", "arm64"),
            HostInstallTarget("macos", "x86_64"),
            HostInstallTarget("linux-debian", "arm64"),
            HostInstallTarget("linux-debian", "x86_64"),
        )
        for target in targets:
            with self.subTest(target=target.display()):
                _path, sbom = load_host_install_sbom(FLAVORS, target)
                artifact = next(
                    item for item in sbom.artifacts if "codex" in item.capabilities
                )
                self.assertIn("/codex-package-", artifact.url)
                self.assertEqual(artifact.archive_format, "tar.gz")
                self.assertEqual(artifact.path_entries, ("bin",))
                self.assertTrue(artifact.executable_path.startswith("bin/codex"))
                self.assertTrue(
                    any(
                        path.startswith("bin/codex-code-mode-host")
                        for path in artifact.required_runtime_paths
                    )
                )

    def test_selected_flavors_compose_and_duplicate_capabilities_warn(self) -> None:
        _path, sbom = load_host_install_sbom(
            FLAVORS,
            HostInstallTarget("macos", "arm64"),
            flavor_names=("lang-python", "build-make"),
        )
        self.assertEqual(
            {item.capability for item in sbom.base.capabilities},
            {
                "python",
                "python-venv",
                "git",
                "git-lfs",
                "codex",
                "claude",
                "cursor-agent",
                "opencode",
                "make",
            },
        )
        self.assertEqual(len(sbom.base.warnings), 1)
        self.assertEqual(sbom.base.warnings[0].capability, "python")
        self.assertEqual(
            sbom.base.warnings[0].sources,
            ("literate-ai-os-base-toolchain", "lang-python"),
        )

    def test_conflicting_duplicate_capability_fails_closed(self) -> None:
        base = parse_base_toolchain_sbom(
            json.loads(
                (FLAVORS / "os-base" / "toolchain.cdx.json").read_text(encoding="utf-8")
            )
        )
        python = base.capability("python")
        conflict = replace(
            base,
            sources=("conflicting-flavor",),
            default_coding_agent=None,
            capabilities=(replace(python, version_constraint=">=3.12,<4"),),
        )
        with self.assertRaises(HostInstallRequirementError) as raised:
            compose_toolchain_sboms((base, conflict))
        self.assertEqual(raised.exception.code, "host-install.capability-conflict")

    def test_unsupported_tuple_lists_ports_and_flavor_directory(self) -> None:
        unsupported = HostInstallTarget("freebsd", "x86_64")
        with self.assertRaises(HostInstallError) as raised:
            load_host_install_sbom(FLAVORS, unsupported)
        self.assertEqual(raised.exception.code, "host-install.target-unsupported")
        self.assertIn(unsupported.display(), str(raised.exception))
        self.assertIn("Supported tuples", str(raised.exception))
        self.assertIn(str(FLAVORS), str(raised.exception))
        self.assertIn("begin a new port", str(raised.exception))

    def test_version_constraints_reject_old_or_unparseable_tools(self) -> None:
        self.assertTrue(version_satisfies("git version 2.55.0", ">=2.30,<3"))
        self.assertFalse(version_satisfies("git version 2.29.9", ">=2.30,<3"))
        self.assertFalse(version_satisfies("unknown", ">=2.30,<3"))

    def test_observer_honors_exact_explicit_coding_agent(self) -> None:
        path, sbom = load_host_install_sbom(
            FLAVORS, HostInstallTarget("macos", "arm64")
        )

        def locate(command: str, _path: str | None) -> str | None:
            return f"/native/bin/{command}"

        def run(arguments, **_kwargs):
            command = tuple(arguments)
            if command[1:3] == ("list", "--versions"):
                package = command[-1]
                versions = {
                    "python@3.13": "3.13.15",
                    "git": "2.55.0",
                    "git-lfs": "3.8.0",
                }
                return _completed(command, stdout=f"{package} {versions[package]}\n")
            if command[0] == str(Path(__import__("sys").executable)):
                return _completed(command)
            if command[-1] == "--version":
                output = (
                    "2.1.259 (Claude Code)"
                    if "claude" in command[0]
                    else "git version 2.55.0"
                )
                return _completed(command, stdout=output)
            if command[-1] == "version":
                return _completed(command, stdout="git-lfs/3.8.0")
            raise AssertionError(command)

        report = observe_host_install_dependencies(
            sbom_path=path,
            sbom=sbom,
            environment={"PATH": "/native/bin", "CODING_CLI": "claude"},
            runner=run,
            locator=locate,
            managed_tool_root=Path(tempfile.mkdtemp()),
        )
        self.assertTrue(report.ready)
        self.assertEqual(report.coding_agent, "claude")
        self.assertEqual(report.observations[-1].requirement.capability, "claude")

    def test_noninteractive_decline_lists_semantic_requirements(self) -> None:
        def locate(command: str, _path: str | None) -> str | None:
            return f"/native/bin/{command}"

        def run(arguments, **_kwargs):
            return _completed(arguments, returncode=1, stderr="not installed")

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(HostInstallError) as raised:
                ensure_host_install_dependencies(
                    sbom_root=FLAVORS,
                    target=HostInstallTarget("macos", "arm64"),
                    environment={"PATH": "/native/bin", "CODING_CLI": "codex"},
                    runner=run,
                    locator=locate,
                    interactive=False,
                    managed_tool_root=Path(temporary),
                )
        self.assertEqual(raised.exception.code, "host-install.dependencies-declined")
        self.assertIn("python", str(raised.exception))
        self.assertIn("git", str(raised.exception))
        self.assertIn("codex", str(raised.exception))
        self.assertIn("No host tools were changed", str(raised.exception))

    def test_winget_each_mode_installs_each_missing_native_package(self) -> None:
        installed: set[str] = set()
        commands: list[tuple[str, ...]] = []

        def locate(command: str, _path: str | None) -> str | None:
            if command in {"codex", "codex.cmd", "codex.exe"}:
                return "C:/native/codex.exe"
            return f"C:/native/{command}"

        def run(arguments, **_kwargs):
            command = tuple(arguments)
            commands.append(command)
            if command[1] == "list":
                package = command[command.index("--id") + 1]
                if package not in installed:
                    return _completed(command, returncode=1)
                return _completed(command, stdout=f"{package} 3.13.15\n")
            if command[1] == "install":
                installed.add(command[command.index("--id") + 1])
                return _completed(command)
            if command[0] == str(Path(__import__("sys").executable)):
                return _completed(command)
            if command[-1] == "--version":
                if "codex" in command[0]:
                    return _completed(command, stdout="codex-cli 0.153.0")
                if "bash" in command[0]:
                    return _completed(command, stdout="GNU bash, version 5.2.0")
                return _completed(command, stdout="git version 2.55.0")
            if command[-1] == "version":
                return _completed(command, stdout="git-lfs/3.8.0")
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as temporary:
            system_root = Path(temporary) / "Windows"
            system32 = system_root / "System32"
            system32.mkdir(parents=True)
            for name in ("MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll"):
                (system32 / name).touch()
            report = ensure_host_install_dependencies(
                sbom_root=FLAVORS,
                target=HostInstallTarget("windows", "x86_64"),
                environment={
                    "PATH": "C:/native",
                    "USERPROFILE": "C:/Users/test",
                    "LOCALAPPDATA": "C:/Users/test/AppData/Local",
                    "SystemRoot": str(system_root),
                    "CODING_CLI": "codex",
                    "LITAI_INSTALL_DEPENDENCIES": "yes",
                },
                runner=run,
                locator=locate,
                interactive=False,
                managed_tool_root=Path(temporary),
            )
        self.assertTrue(report.ready)
        install_commands = [item for item in commands if item[1] == "install"]
        self.assertEqual(len(install_commands), 4)
        self.assertEqual(
            installed,
            {
                "Python.Python.3.13",
                "Git.Git",
                "GitHub.GitLFS",
                "Microsoft.VCRedist.2015+.x64",
            },
        )
        self.assertTrue(all("--id" in item for item in install_commands))

    def test_winget_reprobes_new_tools_through_declared_native_paths(self) -> None:
        installed = {
            "Python.Python.3.13",
            "Git.Git",
            "GitHub.GitLFS",
            "Microsoft.VCRedist.2015+.x64",
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_files = root / "Program Files"
            program_files_x86 = root / "Program Files (x86)"
            node_directory = program_files / "nodejs"
            make_directory = program_files_x86 / "GnuWin32" / "bin"
            system_root = root / "Windows"
            system32 = system_root / "System32"
            system32.mkdir(parents=True)
            for name in ("MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll"):
                (system32 / name).touch()

            def locate(command: str, path: str | None) -> str | None:
                fixed = {
                    "winget.exe": "/native/winget.exe",
                    "git": "/native/git.exe",
                    "git.exe": "/native/git.exe",
                    "bash.exe": "/native/bash.exe",
                    "git-lfs": "/native/git-lfs.exe",
                    "git-lfs.exe": "/native/git-lfs.exe",
                    "codex": "/native/codex.exe",
                    "codex.cmd": "/native/codex.exe",
                    "codex.exe": "/native/codex.exe",
                }
                if command in fixed:
                    return fixed[command]
                for directory in (path or "").split(__import__("os").pathsep):
                    candidate = Path(directory) / command
                    if candidate.is_file():
                        return str(candidate)
                return None

            def run(arguments, **_kwargs):
                command = tuple(arguments)
                if len(command) > 1 and command[1] == "list":
                    package = command[command.index("--id") + 1]
                    if package not in installed:
                        return _completed(command, returncode=1)
                    return _completed(command, stdout=f"{package} installed\n")
                if len(command) > 1 and command[1] == "install":
                    package = command[command.index("--id") + 1]
                    installed.add(package)
                    if package == "OpenJS.NodeJS.LTS":
                        node_directory.mkdir(parents=True)
                        (node_directory / "node.exe").touch()
                    if package == "GnuWin32.Make":
                        make_directory.mkdir(parents=True)
                        (make_directory / "make.exe").touch()
                    return _completed(command)
                executable = Path(command[0]).name.casefold()
                if command[0] == str(Path(__import__("sys").executable)):
                    return _completed(command)
                outputs = {
                    "bash.exe": "GNU bash, version 5.2.0",
                    "codex.exe": "codex-cli 0.153.0",
                    "git.exe": "git version 2.55.0",
                    "git-lfs.exe": "git-lfs/3.8.0",
                    "make.exe": "GNU Make 3.81",
                    "node.exe": "v24.19.0",
                }
                if executable in outputs:
                    return _completed(command, stdout=outputs[executable])
                raise AssertionError(command)

            report = ensure_host_install_dependencies(
                sbom_root=FLAVORS,
                target=HostInstallTarget("windows", "x86_64"),
                flavor_names=("lang-javascript", "build-make"),
                environment={
                    "PATH": "/native",
                    "ProgramFiles": str(program_files),
                    "ProgramFiles(x86)": str(program_files_x86),
                    "USERPROFILE": str(root / "Users" / "test"),
                    "LOCALAPPDATA": str(root / "Users" / "test" / "Local"),
                    "SystemRoot": str(system_root),
                    "CODING_CLI": "codex",
                    "LITAI_INSTALL_DEPENDENCIES": "yes",
                },
                runner=run,
                locator=locate,
                interactive=False,
                managed_tool_root=root / "tools",
            )

        self.assertTrue(report.ready)
        self.assertIn("native:OpenJS.NodeJS.LTS", report.installed)
        self.assertIn("native:GnuWin32.Make", report.installed)
        executables = {
            item.requirement.capability: item.executable for item in report.observations
        }
        self.assertEqual(executables["node"], (node_directory / "node.exe").resolve())
        self.assertEqual(executables["make"], (make_directory / "make.exe").resolve())

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

    def test_managed_artifact_preserves_and_verifies_runtime_companions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "package.tar.gz"
            executable = b"fixture executable"
            companion = b"fixture code mode host"
            with tarfile.open(archive, "w:gz") as bundle:
                for name, content in (
                    ("bundle/bin/codex", executable),
                    ("bundle/bin/codex-code-mode-host", companion),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    info.mode = 0o755
                    bundle.addfile(info, io.BytesIO(content))
            artifact = replace(
                _artifact(archive),
                path_entries=("bin",),
                executable_path="bin/codex",
                required_runtime_paths=("bin/codex-code-mode-host",),
            )

            def download(_url: str, destination: Path) -> None:
                shutil.copy2(archive, destination)

            installed = host_install._install_managed_artifact(
                artifact, tool_root=root / "tools", downloader=download
            )
            self.assertEqual((installed / "bin" / "codex").read_bytes(), executable)
            runtime_host = installed / "bin" / "codex-code-mode-host"
            self.assertEqual(runtime_host.read_bytes(), companion)
            self.assertFalse((installed / "codex").exists())
            self.assertTrue(
                host_install._artifact_manifest_matches(installed, artifact)
            )

            runtime_host.unlink()
            self.assertFalse(
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


if __name__ == "__main__":
    unittest.main()
