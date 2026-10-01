from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
import venv
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai import build_version
from literate_ai.adapters import host_self_update as updater
from literate_ai.adapters.user_paths import (
    HOST_INSTALL_MANIFEST_SCHEMA,
    HostInstallLayout,
)
from literate_ai.version import DISTRIBUTION_VERSION
from tests.unit.test_host_self_update import _python_path, _write_manifest


class CliBuildIdentityTests(unittest.TestCase):
    def test_public_version_flag_reports_build_identity(self):
        from literate_ai.cli import dispatch

        output = io.StringIO()
        with (
            patch.object(dispatch, "maybe_host_self_update"),
            patch.object(
                build_version,
                "cli_version_label",
                return_value="1.1.0 (development; git abc)",
            ),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as stopped,
        ):
            dispatch.main(["--version"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("1.1.0 (development; git abc)", output.getvalue())

    def test_parser_construction_does_not_probe_git(self):
        from literate_ai.cli import dispatch

        with patch.object(build_version, "cli_version_label") as label:
            dispatch._parser()
            label.assert_not_called()

    def test_git_install_reports_commit_and_development_not_bare_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "site-packages" / "literate_ai"
            package.mkdir(parents=True)
            vcs = {"vcs_info": {"vcs": "git", "commit_id": "a" * 40}}
            with (
                patch.object(
                    build_version, "__file__", str(package / "build_version.py")
                ),
                patch.object(
                    build_version.metadata,
                    "distribution",
                    return_value=SimpleNamespace(read_text=lambda _: json.dumps(vcs)),
                ),
            ):
                label = build_version.cli_version_label()
            self.assertIn(DISTRIBUTION_VERSION, label)
            self.assertIn("development Git install", label)
            self.assertIn("a" * 40, label)
            self.assertIn("publication unverified", label)

    def test_wheel_origin_retains_revision_without_claiming_published_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "site-packages" / "literate_ai"
            package.mkdir(parents=True)
            (package / "_distribution_origin.json").write_text(
                json.dumps({"git_revision": "b" * 40})
            )
            with (
                patch.object(
                    build_version, "__file__", str(package / "build_version.py")
                ),
                patch.object(
                    build_version.metadata,
                    "distribution",
                    return_value=SimpleNamespace(read_text=lambda _: None),
                ),
            ):
                label = build_version.cli_version_label()
            self.assertIn("b" * 40, label)
            self.assertIn("publication unverified", label)

    def test_missing_or_malformed_provenance_never_claims_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            for raw in (
                None,
                "{bad",
                "[]",
                '{"vcs_info": {"vcs": "git", "commit_id": "bad\\nvalue"}}',
            ):
                with (
                    self.subTest(raw=raw),
                    patch.object(
                        build_version,
                        "__file__",
                        str(Path(temporary) / "pkg" / "build_version.py"),
                    ),
                    patch.object(
                        build_version.metadata,
                        "distribution",
                        return_value=SimpleNamespace(
                            read_text=lambda _, value=raw: value
                        ),
                    ),
                ):
                    label = build_version.cli_version_label()
                    self.assertIn("publication unverified", label)
                    self.assertNotIn("\n", label)

    def test_checkout_uses_framework_root_not_callers_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").write_text("gitdir: ignored")
            (root / "pyproject.toml").write_text("")
            module = root / "src" / "literate_ai" / "build_version.py"
            with (
                patch.object(build_version, "__file__", str(module)),
                patch.object(
                    build_version.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="c" * 40),
                ) as git,
            ):
                label = build_version.cli_version_label()
            self.assertIn("development source", label)
            self.assertIn("c" * 40, label)
            self.assertEqual(git.call_args.args[0][2], str(root.resolve()))


class ExplicitSelfUpdateTests(unittest.TestCase):
    def test_update_dispatch_checks_immediately_with_original_global_arguments(self):
        from literate_ai.cli import dispatch

        argv = ["--json", "update", "project with spaces"]
        with (
            patch.object(
                dispatch,
                "maybe_host_self_update",
                side_effect=RuntimeError("stop before project mutation"),
            ) as update,
            self.assertRaisesRegex(RuntimeError, "stop before project mutation"),
        ):
            dispatch.main(argv)
        update.assert_called_once_with(argv, check_now=True)

    def test_update_help_and_invalid_options_do_not_trigger_installation(self):
        from literate_ai.cli import dispatch

        with patch.object(dispatch, "maybe_host_self_update") as update:
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                dispatch.main(["update", "--help"])
            self.assertEqual(
                dispatch.main(["update", "--invalid-option"], stderr=io.StringIO()), 2
            )
            update.assert_not_called()

    def test_explicit_check_bypasses_recent_negative_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enrollment = updater.HostSelfUpdateEnrollment(
                root, root / "venv", root / "litai", root / "manifest", root / "staged"
            )
            enrollment.staging_root.mkdir()
            updater._write_cache(
                enrollment.staging_root / "cache.json", {"checked_at": 49, "ok": True}
            )
            calls = []

            def fetch(*args):
                calls.append(args)
                return b'{"tag_name":"v1.0.0", "assets":[]}'

            for force in (False, True):
                updater.stage_github_wheel(
                    enrollment,
                    now=50,
                    installed_version="1.0.0",
                    environ={},
                    releases_url="https://example.invalid/releases/latest",
                    fetch_json=fetch,
                    force_check=force,
                )
            self.assertEqual(len(calls), 1)

    def test_real_prefix_pip_upgrade_and_original_command_reexecution(self):
        """Use real wheels, pip and launcher; only release discovery is synthetic."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            layout = HostInstallLayout.for_prefix(root / "prefix")
            environment = Path(layout.environment)
            venv.EnvBuilder(with_pip=True).create(environment)
            python = updater._venv_python(environment)
            _write_manifest(layout, schema=HOST_INSTALL_MANIFEST_SCHEMA)

            def wheel(version):
                path = root / f"literate_ai-{version}-py3-none-any.whl"
                info = f"literate_ai-{version}.dist-info"
                files = {
                    "update_fixture.py": (
                        "import json, sys\n"
                        "def main():\n"
                        f"    print(json.dumps({{'version': '{version}', "
                        "'argv': sys.argv[1:]}))\n"
                    ),
                    f"{info}/METADATA": (
                        "Metadata-Version: 2.1\nName: literate-ai\n"
                        f"Version: {version}\n"
                    ),
                    f"{info}/WHEEL": (
                        "Wheel-Version: 1.0\nGenerator: test\n"
                        "Root-Is-Purelib: true\nTag: py3-none-any\n"
                    ),
                    f"{info}/entry_points.txt": (
                        "[console_scripts]\nlitai = update_fixture:main\n"
                    ),
                }
                files[f"{info}/RECORD"] = "".join(
                    f"{name},,\n" for name in (*files, f"{info}/RECORD")
                )
                with zipfile.ZipFile(path, "w") as archive:
                    for name, content in files.items():
                        archive.writestr(name, content)
                return path

            old, new = wheel("1.0.0"), wheel("2.0.0")
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    str(old),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            variables = dict(updater.os.environ)
            for key in (
                "CI",
                "GITHUB_ACTIONS",
                "LITAI_NO_SELF_UPDATE",
                "LITAI_SELF_UPDATE_REEXEC",
                "LITAI_EVIDENCE_RUN",
                "PYTHONPATH",
            ):
                variables.pop(key, None)
            variables.update(LITAI_HOST_INSTALL="1", LITAI_PREFIX=str(layout.prefix))
            payload = {
                "tag_name": "v2.0.0",
                "assets": [
                    {
                        "name": new.name,
                        "browser_download_url": "https://example.invalid/release.whl",
                    }
                ],
            }
            stage = updater.stage_github_wheel

            def fixture_stage(enrollment, **kwargs):
                stage(
                    enrollment,
                    **kwargs,
                    releases_url="https://example.invalid/releases/latest",
                    fetch_json=lambda *a: json.dumps(payload).encode(),
                    fetch_file=lambda url, headers, timeout, destination: (
                        destination.write_bytes(new.read_bytes())
                    ),
                )

            observed = []

            def execute(command, env):
                self.assertEqual(env[updater.REEXEC_ENVIRONMENT], "1")
                result = subprocess.run(
                    command,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                observed.append(json.loads(result.stdout))

            errors = io.StringIO()
            with (
                patch.object(updater, "DISTRIBUTION_VERSION", "1.0.0"),
                patch.object(updater, "stage_github_wheel", side_effect=fixture_stage),
            ):
                updater.maybe_host_self_update(
                    ["update", "--json", "project with spaces"],
                    environ=variables,
                    executable=python,
                    stderr=errors,
                    check_now=True,
                    exec_fn=execute,
                )
            self.assertEqual(
                observed,
                [
                    {
                        "version": "2.0.0",
                        "argv": ["update", "--json", "project with spaces"],
                    }
                ],
            )
            self.assertIn("from 1.0.0 to 2.0.0", errors.getvalue())

    def test_explicit_update_checks_before_apply_without_background_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = HostInstallLayout.for_prefix(Path(temporary).resolve())
            _write_manifest(layout, schema=HOST_INSTALL_MANIFEST_SCHEMA)
            python = _python_path(layout)
            environment = {
                "LITAI_HOST_INSTALL": "1",
                "LITAI_PREFIX": str(layout.prefix),
            }
            calls = []
            with (
                patch.object(
                    updater,
                    "stage_github_wheel",
                    side_effect=lambda *a, **kw: calls.append(("check", kw)),
                ),
                patch.object(
                    updater,
                    "apply_staged_wheel",
                    side_effect=lambda *a, **kw: calls.append(("apply", kw)),
                ),
                patch.object(updater, "spawn_self_update_worker") as spawn,
            ):
                updater.maybe_host_self_update(
                    ["update", "--json"],
                    environ=environment,
                    executable=python,
                    stderr=io.StringIO(),
                    now=50,
                    check_now=True,
                )
            self.assertEqual([item[0] for item in calls], ["check", "apply"])
            self.assertTrue(calls[0][1]["force_check"])
            self.assertEqual(calls[1][1]["argv"], ["update", "--json"])
            spawn.assert_not_called()

    def test_explicit_update_respects_opt_out(self):
        with patch.object(updater, "stage_github_wheel") as stage:
            updater.maybe_host_self_update(
                ["update"], environ={"LITAI_NO_SELF_UPDATE": "1"}, check_now=True
            )
            stage.assert_not_called()

    def test_mismatched_wheel_and_hidden_prerelease_are_not_selected(self):
        for tag, wheel in (
            ("v9.0.0", "literate_ai-8.0.0-py3-none-any.whl"),
            ("v9.0.0rc1", "literate_ai-9.0.0rc1-py3-none-any.whl"),
        ):
            with self.subTest(tag=tag):
                self.assertIsNone(
                    updater.select_github_release_wheel(
                        {
                            "tag_name": tag,
                            "assets": [
                                {
                                    "name": wheel,
                                    "browser_download_url": "https://example.invalid/wheel",
                                }
                            ],
                        },
                        installed_version="1.0.0",
                    )
                )
