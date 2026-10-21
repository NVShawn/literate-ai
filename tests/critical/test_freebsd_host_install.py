"""Native pkg provenance and tuple-specific alternatives never become Linux proof."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import host_install
from literate_ai.bootstrap.host_install_requirements import (
    HostInstallRequirementError,
    HostInstallTarget,
    compose_toolchain_sboms,
    detect_host_install_target,
    parse_host_install_sbom,
)


class FreeBSDHostInstallTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2]
        self.path, self.sbom = host_install.load_host_install_sbom(
            self.root / "flavors",
            HostInstallTarget("freebsd", "x86_64"),
            flavor_names=("lang-python", "build-make"),
        )

    def test_native_tuple_and_template_agree(self):
        target = detect_host_install_target(system="FreeBSD", machine="amd64")
        self.assertEqual(target, self.sbom.target)
        self.assertEqual(self.sbom.package_manager.identifier, "pkg")
        self.assertEqual(self.sbom.artifacts, ())
        self.assertEqual(
            self.path.read_bytes(),
            (
                self.root
                / "src/literate_ai/project_template"
                / self.path.relative_to(self.root)
            ).read_bytes(),
        )
        self.assertEqual(
            {
                p.capability
                for p in self.sbom.packages
                if p.capability in {a.capability for a in self.sbom.base.coding_agents}
            },
            {"codex"},
        )

    def test_missing_agent_or_non_alternative_capability_refuses(self):
        catalog = compose_toolchain_sboms(
            (
                self.sbom.base,
                *host_install._load_flavor_toolchains(self.root / "flavors").values(),
            )
        )
        for missing in ("codex", "git", "gmake"):
            document = json.loads(self.path.read_bytes())
            removed = next(
                c for c in document["components"] if c["name"] == "FreeBSD " + missing
            )
            document["components"].remove(removed)
            # Keep a valid exact graph so failure proves capability coverage.
            document["dependencies"] = [
                d for d in document["dependencies"] if d["ref"] != removed["bom-ref"]
            ]
            document["dependencies"][0]["dependsOn"].remove(removed["bom-ref"])
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(HostInstallRequirementError, "does not close"),
            ):
                parse_host_install_sbom(document, base=self.sbom.base, catalog=catalog)

    def test_cmake_uses_native_command_package(self):
        _, sbom = host_install.load_host_install_sbom(
            self.root / "flavors", self.sbom.target, flavor_names=("build-cmake",)
        )
        requirement = next(p for p in sbom.packages if p.capability == "cmake")
        self.assertEqual(requirement.manager_package, "cmake-core")

    def test_explicit_unrealized_provider_refuses_before_probe(self):
        def forbidden(*args, **kwargs):
            self.fail("unsupported provider must not probe or run an ambient command")

        for agent in ("claude", "cursor-agent", "opencode"):
            with (
                self.subTest(agent=agent),
                self.assertRaises(host_install.HostInstallError) as raised,
            ):
                host_install._select_coding_agent(
                    self.sbom,
                    environment={"CODING_CLI": agent},
                    runner=forbidden,
                    locator=forbidden,
                )
            self.assertEqual(
                raised.exception.code, "host-install.coding-agent-unsupported"
            )
            self.assertIn("codex", raised.exception.message)

    def test_automatic_selection_ignores_ambient_unrealized_provider(self):
        visited = []

        def locator(command, path):
            visited.append(command)
            return None

        selected = host_install._select_coding_agent(
            self.sbom,
            environment={},
            runner=lambda *a, **k: self.fail("absent command executed"),
            locator=locator,
        )
        self.assertEqual(selected, "codex")
        self.assertTrue(visited)
        self.assertTrue(all(command.startswith("codex") for command in visited))

    def test_native_probe_uses_gmake_and_requires_pkg_provenance(self):
        requirement = next(p for p in self.sbom.packages if p.capability == "make")
        native = Path(tempfile.gettempdir()).resolve() / "freebsd-native-tools"
        pkg, gmake = str(native / "pkg"), str(native / "gmake")
        for present in (True, False):
            commands = []

            def runner(argv, *, commands=commands, present=present, **kwargs):
                commands.append(argv)
                if argv[0] == pkg:
                    return subprocess.CompletedProcess(
                        argv, 0 if present else 1, "gmake-4.4.1" if present else "", ""
                    )
                return subprocess.CompletedProcess(argv, 0, "GNU Make 4.4.1", "")

            def locator(command, path):
                self.assertEqual(command, "gmake")
                return gmake

            observed = host_install._observe_native_package(
                requirement,
                sbom=self.sbom,
                query_path=pkg,
                environment={},
                runner=runner,
                locator=locator,
            )
            self.assertEqual(observed.ready, present)
            self.assertTrue(observed.probe_satisfied)
            self.assertEqual(
                commands, [(pkg, "query", "%n-%v", "gmake"), (gmake, "--version")]
            )

    def test_split_sqlite_requires_native_package_and_working_current_interpreter(self):
        requirement = next(
            p for p in self.sbom.packages if p.capability == "python-sqlite3"
        )
        self.assertEqual(requirement.manager_package, "py312-sqlite3")
        self.assertEqual(requirement.probe_kind, "python-module")
        self.assertIn("select 1", requirement.probe_arguments[-1])
        for registered, usable, python_version in (
            (True, True, "3.12.15"),
            (True, False, "3.12.15"),
            (False, True, "3.12.15"),
            (True, True, "3.11.14"),
        ):
            calls = []

            def runner(
                argv, *, calls=calls, registered=registered, usable=usable, **kwargs
            ):
                calls.append(argv)
                if argv[0] == "pkg":
                    return subprocess.CompletedProcess(
                        argv,
                        0 if registered else 1,
                        "py312-sqlite3-3.12.15" if registered else "",
                        "",
                    )
                self.assertEqual(argv, (sys.executable, *requirement.probe_arguments))
                return subprocess.CompletedProcess(
                    argv, 0 if usable else 1, "", "missing extension"
                )

            with (
                self.subTest(
                    registered=registered, usable=usable, python_version=python_version
                ),
                mock.patch.object(
                    host_install.platform, "python_version", return_value=python_version
                ),
            ):
                observed = host_install._observe_native_package(
                    requirement,
                    sbom=self.sbom,
                    query_path="pkg",
                    environment={},
                    runner=runner,
                    locator=lambda *a: self.fail(
                        "Python module probe must use current interpreter"
                    ),
                )
                self.assertEqual(
                    observed.ready,
                    registered and usable and python_version == "3.12.15",
                )
            self.assertEqual(len(calls), 2)

    def test_declining_install_does_not_run_pkg_install_or_download(self):
        def runner(argv, **kwargs):
            self.assertEqual(argv[0], sys.executable)
            self.assertTrue(
                argv[1:] == ("-c", "import ensurepip,venv")
                or argv[1:] == ("-m", "sqlite3", ":memory:", "select 1")
            )
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(host_install.HostInstallError) as raised:
                host_install.ensure_host_install_dependencies(
                    sbom_root=self.root / "flavors",
                    target=self.sbom.target,
                    flavor_names=("build-make",),
                    environment={
                        "LITAI_INSTALL_DEPENDENCIES": "no",
                        "CODING_CLI": "codex",
                    },
                    runner=runner,
                    locator=lambda *a: None,
                    downloader=lambda *a: self.fail(
                        "declining consent must not download"
                    ),
                    managed_tool_root=Path(temporary),
                    interactive=False,
                )
        self.assertEqual(raised.exception.code, "host-install.dependencies-declined")

    def test_pkg_install_is_reobserved_and_an_old_agent_still_refuses(self):
        # Exercise consent -> exact pkg argv -> query/probe again. A successful
        # package-manager exit never supplies the missing runtime version proof.
        for agent_version in ("0.162.1", "0.39.0"):
            installed = False
            installs = []
            versions = {
                "git": "2.56.0",
                "git-lfs": "3.8.0",
                "gmake": "4.4.1",
                "codex": agent_version,
            }
            native = Path(tempfile.gettempdir()).resolve() / "freebsd-native-tools"

            def locator(command, path, *, versions=versions, native=native):
                if command in {"pkg", *versions}:
                    return str(native / command)
                return None

            def runner(
                argv, *, native=native, installs=installs, versions=versions, **kwargs
            ):
                nonlocal installed
                if argv[0] == sys.executable:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv[:2] == (str(native / "pkg"), "install"):
                    installs.append(argv)
                    installed = True
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv[:2] == (str(native / "pkg"), "query"):
                    package = argv[-1]
                    return subprocess.CompletedProcess(
                        argv,
                        0 if installed else 1,
                        package + "-1.0" if installed else "",
                        "",
                    )
                command = Path(argv[0]).name
                self.assertIn(command, versions)
                return subprocess.CompletedProcess(
                    argv,
                    0 if installed else 1,
                    versions[command] if installed else "",
                    "",
                )

            # This fixture models a FreeBSD Python 3.12 host independently of
            # the interpreter that runs the cross-platform controller suite.
            with (
                tempfile.TemporaryDirectory() as temporary,
                mock.patch.object(
                    host_install.platform, "python_version", return_value="3.12.15"
                ),
            ):

                def execute():
                    return host_install.ensure_host_install_dependencies(
                        sbom_root=self.root / "flavors",
                        target=self.sbom.target,
                        flavor_names=("build-make",),
                        environment={
                            "LITAI_INSTALL_DEPENDENCIES": "yes",
                            "CODING_CLI": "codex",
                        },
                        runner=runner,
                        locator=locator,
                        root_user=True,
                        downloader=lambda *a: self.fail(
                            "native pkg tuple cannot download an agent archive"
                        ),
                        managed_tool_root=Path(temporary),
                        interactive=False,
                    )

                if agent_version == "0.162.1":
                    self.assertTrue(execute().ready)
                else:
                    with self.assertRaises(host_install.HostInstallError) as raised:
                        execute()
                    self.assertEqual(
                        raised.exception.code, "host-install.install-incomplete"
                    )
            self.assertEqual(
                installs,
                [
                    (
                        str(native / "pkg"),
                        "install",
                        "-y",
                        "python312",
                        "py312-sqlite3",
                        "git",
                        "git-lfs",
                        "codex",
                        "gmake",
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main()
