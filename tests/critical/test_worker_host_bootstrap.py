"""Worker bootstrap composes native FreeBSD observation without host mutation."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import host_install


class WorkerHostBootstrapTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        path = root / "skills/agent/detect-before-install/scripts/bootstrap.py"
        spec = importlib.util.spec_from_file_location("worker_host_bootstrap", path)
        self.bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bootstrap)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tool_root = Path(temporary.name) / "tools"

    def run_current_python_probe(self, argv, **kwargs):
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(kwargs["timeout"], 120)
        self.assertIn(
            tuple(argv[1:]),
            (
                ("-c", "import ensurepip,venv"),
                ("-m", "sqlite3", ":memory:", "select 1"),
            ),
        )
        return subprocess.CompletedProcess(argv, 1, "", "fixture missing capability")

    def invoke(self, *arguments):
        output = io.StringIO()
        observe = host_install.observe_host_install_dependencies

        def observe_absent(**kwargs):
            return observe(
                **kwargs,
                locator=lambda *_: None,
                managed_tool_root=self.tool_root,
            )

        with (
            mock.patch.object(sys, "argv", ["bootstrap.py", *arguments]),
            mock.patch.object(
                self.bootstrap.host_platform, "system", return_value="FreeBSD"
            ),
            mock.patch.object(
                self.bootstrap.host_platform, "machine", return_value="amd64"
            ),
            mock.patch.dict(
                os.environ, {"PATH": "", "CODING_CLI": "codex"}, clear=True
            ),
            mock.patch.object(
                self.bootstrap,
                "observe_host_install_dependencies",
                side_effect=observe_absent,
            ),
            mock.patch.object(
                self.bootstrap.subprocess,
                "run",
                side_effect=self.run_current_python_probe,
            ),
            contextlib.redirect_stdout(output),
        ):
            status = self.bootstrap.main()
        return status, json.loads(output.getvalue())

    def test_native_detection_reports_missing_pkg_tools_without_clock_or_install(self):
        with (
            mock.patch.object(
                self.bootstrap, "ensure_host_install_dependencies"
            ) as install,
            mock.patch.object(self.bootstrap, "_query_ntp_unix") as query_clock,
            mock.patch.object(self.bootstrap, "_synchronize_clock") as sync_clock,
        ):
            status, report = self.invoke(
                "--platform", "freebsd", "--flavor", "+lang-python"
            )
        self.assertEqual(status, 1)
        self.assertEqual(report["platform"], "freebsd")
        self.assertEqual(report["host_install"]["package_manager"], "pkg")
        self.assertEqual(report["coding_agent"], "codex")
        self.assertIn("git-lfs", report["missing"])
        self.assertFalse(report["passed"])
        self.assertFalse(report["install_requested"])
        self.assertEqual(report["commands"], [])
        self.assertEqual(report["clock_sync"]["status"], "skipped")
        self.assertFalse(report["clock_sync"]["attempted"])
        install.assert_not_called()
        query_clock.assert_not_called()
        sync_clock.assert_not_called()

    def test_explicit_linux_does_not_relabel_freebsd(self):
        status, report = self.invoke("--platform", "linux")
        self.assertEqual(status, 1)
        self.assertEqual(report["error"]["code"], "host-install.platform-mismatch")
        self.assertEqual(report["before"], {})
        self.assertFalse(report["clock_sync"]["attempted"])

    def test_selected_unsupported_agent_is_not_replaced(self):
        status, report = self.invoke("--coding-cli", "opencode")
        self.assertEqual(status, 1)
        self.assertEqual(report["coding_agent"], "opencode")
        self.assertEqual(
            report["error"]["code"], "host-install.coding-agent-unsupported"
        )
        self.assertFalse(report["clock_sync"]["attempted"])

    def test_explicit_install_reaches_native_owner_and_clock_step(self):
        def retain_missing(**kwargs):
            self.assertFalse(kwargs["interactive"])
            self.assertEqual(kwargs["target"].operating_system, "freebsd")
            self.assertEqual(kwargs["environment"]["LITAI_INSTALL_DEPENDENCIES"], "yes")
            path, sbom = host_install.load_host_install_sbom(
                kwargs["sbom_root"], kwargs["target"]
            )
            return host_install.observe_host_install_dependencies(
                sbom_path=path,
                sbom=sbom,
                environment=kwargs["environment"],
                runner=kwargs["runner"],
                locator=lambda *_: None,
                managed_tool_root=self.tool_root,
            )

        with (
            mock.patch.object(
                self.bootstrap,
                "ensure_host_install_dependencies",
                side_effect=retain_missing,
            ) as install,
            mock.patch.object(
                self.bootstrap, "_synchronize_clock", return_value={"status": "failed"}
            ) as sync_clock,
        ):
            status, report = self.invoke("--install-missing")
        install.assert_called_once()
        sync_clock.assert_called_once()
        self.assertEqual(sync_clock.call_args.args[0], "freebsd")
        self.assertTrue(report["install_requested"])
        self.assertFalse(report["passed"])
        self.assertEqual(status, 1)

    def test_freebsd_clock_does_not_select_linux_service(self):
        with mock.patch.object(
            self.bootstrap.shutil,
            "which",
            side_effect=lambda name, **_: "/native/" + name,
        ) as locate:
            command = self.bootstrap._clock_command("freebsd", {"PATH": "/native"})
        self.assertEqual(command, ("/native/ntpdate", "-u", "time.nist.gov"))
        locate.assert_called_once_with("ntpdate", path="/native")

    def test_other_platforms_retain_identity_and_unknown_system_refuses(self):
        for system, expected in (
            ("Darwin", "macos"),
            ("Linux", "linux"),
            ("Windows", "windows"),
        ):
            with (
                self.subTest(system=system),
                mock.patch.object(
                    self.bootstrap.host_platform, "system", return_value=system
                ),
            ):
                self.assertEqual(self.bootstrap._platform_family(), expected)
        with mock.patch.object(
            self.bootstrap.host_platform, "system", return_value="Unknown"
        ):
            with self.assertRaises(host_install.HostInstallError):
                self.bootstrap._platform_family()
