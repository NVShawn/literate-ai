"""Installing from a dirty Git checkout must fail fast and clearly."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.install_litai import (
    _create_private_environment,
    _require_clean_git_checkout,
    main,
)


class InstallLitaiCleanCheckoutTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX interpreter aliases")
    def test_private_environment_from_a_real_interpreter_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            alias = root / "python-alias"
            alias.symlink_to(Path(sys._base_executable).resolve(strict=True))
            environment = root / "private-runtime"
            bootstrap = (
                Path(__file__).resolve().parents[2]
                / "src/literate_ai/remote_worker_bootstrap.py"
            )
            created = subprocess.run(
                (
                    str(alias),
                    "-I",
                    "-c",
                    "import runpy, sys; from pathlib import Path; "
                    "runpy.run_path(sys.argv[1])['_create_python_environment']"
                    "(Path(sys.argv[2]))",
                    str(bootstrap),
                    str(environment),
                ),
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            python = environment / "bin/python"
            self.assertFalse(python.is_symlink())
            probe = subprocess.run(
                (
                    str(python),
                    "-I",
                    "-c",
                    "import json, sys, pip; "
                    "print(json.dumps([sys.prefix, sys.base_prefix, pip.__version__]))",
                ),
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            prefix, base_prefix, pip_version = json.loads(probe.stdout)
            self.assertEqual(Path(prefix).resolve(), environment)
            self.assertEqual(
                Path(base_prefix).resolve(), Path(sys.base_prefix).resolve()
            )
            self.assertTrue(pip_version)

    def test_private_environment_runs_its_python_and_pip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary).resolve() / "private-runtime"
            _create_private_environment(environment)
            python = environment / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            probe = subprocess.run(
                (
                    str(python),
                    "-c",
                    "import json, sys, pip; "
                    "print(json.dumps([sys.prefix, pip.__version__]))",
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            prefix, pip_version = json.loads(probe.stdout)
            self.assertEqual(Path(prefix).resolve(), environment)
            self.assertTrue(pip_version)

    def _git(self, root: Path, *arguments: str) -> None:
        subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=True,
            capture_output=True,
            text=True,
        )

    def _init_repository(self, root: Path) -> None:
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "install@example.invalid")
        self._git(root, "config", "user.name", "Install Test")

    def test_dirty_or_untracked_checkout_raises_an_actionable_error(self) -> None:
        for case in ("uncommitted", "untracked"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self._init_repository(root)
                (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
                self._git(root, "add", ".")
                self._git(root, "commit", "-q", "-m", "initial")
                _require_clean_git_checkout(root)
                if case == "uncommitted":
                    (root / "pyproject.toml").write_text(
                        "[project]\nchanged\n", encoding="utf-8"
                    )
                else:
                    (root / "untracked.txt").write_text("new\n", encoding="utf-8")
                with self.assertRaises(RuntimeError) as raised:
                    _require_clean_git_checkout(root)
                message = str(raised.exception)
                self.assertIn("uncommitted or untracked changes", message)
                self.assertIn("Commit or stash", message)

    def test_main_stops_before_install_when_host_preflight_fails(self) -> None:
        from literate_ai.adapters.host_install import HostInstallError

        with (
            mock.patch(
                "scripts.install_litai.ensure_host_install_dependencies",
                side_effect=HostInstallError(
                    "host-install.test", "requirements missing"
                ),
            ),
            mock.patch("scripts.install_litai.install") as install,
            mock.patch.object(
                sys,
                "argv",
                [
                    "install_litai.py",
                    "--source",
                    "/source",
                    "--host-sbom-root",
                    "/sboms",
                ],
            ),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(main(), 2)
        install.assert_not_called()
        self.assertIn("requirements missing", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
