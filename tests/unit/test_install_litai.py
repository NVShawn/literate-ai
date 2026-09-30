"""Installing from a dirty Git checkout must fail fast and clearly."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.user_paths import HostInstallLayout
from scripts.install_litai import (
    _install_environment,
    _require_clean_git_checkout,
    _venv_python_is_usable,
    install,
    main,
)


class InstallLitaiCleanCheckoutTests(unittest.TestCase):
    def test_install_environment_rejects_python_startup_policy(self) -> None:
        with mock.patch.dict(
            "scripts.install_litai.os.environ",
            {
                "PATH": "/host/bin",
                "PYTHONPYCACHEPREFIX": "/repository/object-cache",
                "PythonPath": "/ambient/imports",
                "PIP_INDEX_URL": "https://unreviewed.invalid/simple",
                "Pip_Config_File": "/ambient/pip.conf",
            },
            clear=True,
        ):
            environment = _install_environment()

        by_name = {name.casefold(): value for name, value in environment.items()}
        self.assertEqual(by_name["path"], "/host/bin")
        self.assertEqual(by_name["pythondontwritebytecode"], "1")
        self.assertNotIn("pythonpycacheprefix", by_name)
        self.assertNotIn("pythonpath", by_name)
        self.assertEqual(by_name["pip_index_url"], "https://unreviewed.invalid/simple")
        self.assertEqual(by_name["pip_config_file"], "/ambient/pip.conf")

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

    def test_clean_committed_checkout_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repository(root)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-q", "-m", "initial")
            _require_clean_git_checkout(root)

    def test_uncommitted_change_raises_an_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repository(root)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-q", "-m", "initial")
            (root / "pyproject.toml").write_text(
                "[project]\nchanged\n", encoding="utf-8"
            )
            with self.assertRaises(RuntimeError) as raised:
                _require_clean_git_checkout(root)
            self.assertIn("uncommitted or untracked changes", str(raised.exception))
            self.assertIn("Commit or stash", str(raised.exception))

    def test_untracked_file_raises_an_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repository(root)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-q", "-m", "initial")
            (root / "untracked.txt").write_text("new\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as raised:
                _require_clean_git_checkout(root)
            self.assertIn("uncommitted or untracked changes", str(raised.exception))

    def test_non_git_source_is_not_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            _require_clean_git_checkout(root)

    def test_install_force_reinstalls_same_version_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = (root / "source").resolve()
            prefix = (root / "prefix").resolve()
            (source / "scripts").mkdir(parents=True)
            (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            launcher_name = (
                "litai-launcher.cmd" if sys.platform == "win32" else "litai-launcher"
            )
            (source / "scripts" / launcher_name).write_text(
                "launcher\n", encoding="utf-8"
            )
            layout = HostInstallLayout.for_prefix(prefix)
            scripts = Path(layout.environment) / (
                "Scripts" if sys.platform == "win32" else "bin"
            )
            scripts.mkdir(parents=True)
            python = scripts / ("python.exe" if sys.platform == "win32" else "python")
            python.write_text("existing runtime\n", encoding="utf-8")

            with (
                mock.patch("scripts.install_litai._run") as run,
                mock.patch(
                    "scripts.install_litai._venv_python_is_usable", return_value=True
                ),
            ):
                install(prefix=prefix, source=source)

            run.assert_called_once_with(
                str(python),
                "-m",
                "pip",
                "--disable-pip-version-check",
                "install",
                "--upgrade",
                "--force-reinstall",
                str(source),
            )
            manifest = json.loads(Path(layout.manifest).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], "literate-ai/host-install-manifest@2")
            self.assertTrue(manifest["self_update"])

    def test_install_recreates_a_stale_venv_whose_interpreter_cannot_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = (root / "source").resolve()
            prefix = (root / "prefix").resolve()
            (source / "scripts").mkdir(parents=True)
            (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            launcher_name = (
                "litai-launcher.cmd" if sys.platform == "win32" else "litai-launcher"
            )
            (source / "scripts" / launcher_name).write_text(
                "launcher\n", encoding="utf-8"
            )
            layout = HostInstallLayout.for_prefix(prefix)
            environment = Path(layout.environment)
            scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
            scripts.mkdir(parents=True)
            python = scripts / ("python.exe" if sys.platform == "win32" else "python")
            python.write_text("broken interpreter\n", encoding="utf-8")
            marker = environment / "stale-marker"
            marker.write_text("leftover from the broken venv\n", encoding="utf-8")

            with (
                mock.patch("scripts.install_litai._run") as run,
                mock.patch(
                    "scripts.install_litai._venv_python_is_usable", return_value=False
                ),
                mock.patch("scripts.install_litai.venv.EnvBuilder") as env_builder_cls,
            ):
                env_builder = env_builder_cls.return_value

                def _recreate(target: Path) -> None:
                    Path(target).mkdir(parents=True, exist_ok=True)

                env_builder.create.side_effect = _recreate

                install(prefix=prefix, source=source)

            self.assertFalse(marker.exists())
            env_builder_cls.assert_called_once_with(with_pip=True)
            env_builder.create.assert_called_once_with(environment)
            run.assert_called_once_with(
                str(python),
                "-m",
                "pip",
                "--disable-pip-version-check",
                "install",
                "--upgrade",
                "--force-reinstall",
                str(source),
            )

    def test_venv_python_is_usable_reports_working_interpreter(self) -> None:
        self.assertTrue(_venv_python_is_usable(Path(sys.executable)))

    def test_venv_python_is_usable_reports_broken_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broken = Path(temporary) / "python"
            broken.write_text("not an interpreter\n", encoding="utf-8")
            broken.chmod(0o755)
            self.assertFalse(_venv_python_is_usable(broken))

    def test_main_preflights_host_dependencies_before_install(self) -> None:
        report = SimpleNamespace(sbom_path=Path("/sboms/host.cdx.json"))
        installed = {
            "prefix": "/prefix",
            "environment": "/prefix/environment",
            "launcher": "/prefix/bin/litai",
        }
        with (
            mock.patch(
                "scripts.install_litai.ensure_host_install_dependencies",
                return_value=report,
            ) as ensure,
            mock.patch("scripts.install_litai.apply_host_install_path") as apply_path,
            mock.patch(
                "scripts.install_litai.install", return_value=installed
            ) as install,
            mock.patch.object(
                sys,
                "argv",
                [
                    "install_litai.py",
                    "--source",
                    "/source",
                    "--prefix",
                    "/prefix",
                    "--host-sbom-root",
                    "/sboms",
                ],
            ),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(main(), 0)
        ensure.assert_called_once_with(sbom_root=Path("/sboms"))
        apply_path.assert_called_once_with(report)
        install.assert_called_once_with(
            prefix=Path("/prefix"), source=Path("/source").resolve()
        )
        self.assertIn("host_dependency_sbom", output.call_args.args[0])

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


class InstallLitaiBootstrapImportTests(unittest.TestCase):
    def test_write_host_install_manifest_imports_without_packaging(self) -> None:
        """Prefix install runs before pip can provide packaging."""

        root = Path(__file__).resolve().parents[2]
        probe = r"""
from importlib.abc import MetaPathFinder
from pathlib import Path
import json
import sys
import tempfile

class BlockPackaging(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "packaging" or fullname.startswith("packaging."):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None

sys.meta_path.insert(0, BlockPackaging())
from literate_ai.adapters.host_self_update import write_host_install_manifest

with tempfile.TemporaryDirectory() as temporary:
    prefix = Path(temporary) / "prefix"
    manifest = prefix / "share" / "literate-ai" / "install.json"
    write_host_install_manifest(
        manifest,
        prefix=prefix,
        environment=prefix / "share" / "literate-ai" / "venv",
        launcher=prefix / "bin" / "litai",
    )
    document = json.loads(manifest.read_text(encoding="utf-8"))
assert document["self_update"] is True
print("ok")
"""
        completed = subprocess.run(
            (sys.executable, "-c", probe),
            check=False,
            capture_output=True,
            cwd=str(root),
            env={**os.environ, "PYTHONPATH": str(root / "src")},
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr or completed.stdout,
        )
        self.assertEqual(completed.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
