from __future__ import annotations

import io
import json
import os
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
from tests.support.fixtures_test_host_self_update import _write_manifest


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


class ExplicitSelfUpdateTests(unittest.TestCase):
    def test_real_prefix_pip_upgrade_and_original_command_reexecution(self):
        """Use real wheels, pip and launcher; only release discovery is synthetic."""
        self._real_prefix_upgrade()

    def _real_prefix_upgrade(self):
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
            # Force an external cache and identical source size/timestamps. The
            # regression must not depend on pip finishing within a clock tick.
            variables["PYTHONPYCACHEPREFIX"] = str(root / "external-bytecode")
            variables["PYTHONOPTIMIZE"] = "1"
            variables["PIP_NO_COMPILE"] = "1"  # explicit --compile must win
            baseline = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import update_fixture; print(update_fixture.__file__)",
                ],
                env=variables,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            fixture_source = Path(baseline.stdout.strip())
            timestamp = 1700000000
            os.utime(fixture_source, (timestamp, timestamp))
            subprocess.run(
                [str(python), "-c", "import update_fixture; update_fixture.main()"],
                env=variables,
                check=True,
                capture_output=True,
                timeout=30,
            )
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
            diagnostics = []

            def install(*args, **kwargs):
                result = updater._default_runner(*args, **kwargs)
                os.utime(fixture_source, (timestamp, timestamp))
                diagnostics.append(f"pip result: {result!r}")
                return result

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
                probe = subprocess.run(
                    [
                        str(python),
                        "-c",
                        (
                            "import importlib.metadata as m, pathlib, hashlib; "
                            "import update_fixture as f; "
                            "p=pathlib.Path(f.__file__); "
                            "print('distribution:', m.version('literate-ai')); "
                            "print('source:', p, p.stat().st_mtime_ns, p.read_text()); "
                            "print('executed constants:', f.main.__code__.co_consts); "
                            "c=pathlib.Path(f.__cached__); "
                            "print('bytecode:', c, c.read_bytes()[:16].hex(), "
                            "hashlib.sha256(c.read_bytes()).hexdigest()) "
                            "if c.exists() else print('no bytecode')"
                        ),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                diagnostics.append(f"installed probe: {probe!r}")

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
                    runner=install,
                )
            self.assertEqual(
                observed,
                [
                    {
                        "version": "2.0.0",
                        "argv": ["update", "--json", "project with spaces"],
                    }
                ],
                msg=errors.getvalue() + "\n" + "\n".join(diagnostics),
            )
            self.assertIn("from 1.0.0 to 2.0.0", errors.getvalue())
