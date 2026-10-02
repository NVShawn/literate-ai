"""Hermetic contracts for prefix-installed CLI self-update."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.user_paths import (
    HOST_INSTALL_MANIFEST_SCHEMA,
    HostInstallLayout,
)


def _python_path(layout: HostInstallLayout) -> Path:
    scripts = Path(layout.environment) / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True, exist_ok=True)
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    python.write_text("python\n", encoding="utf-8")
    return python


def _write_manifest(
    layout: HostInstallLayout,
    *,
    schema: str,
    self_update: bool | None = True,
) -> None:
    Path(layout.environment).mkdir(parents=True, exist_ok=True)
    Path(layout.launcher).parent.mkdir(parents=True, exist_ok=True)
    Path(layout.launcher).write_text("launcher\n", encoding="utf-8")
    document: dict[str, object] = {
        "schema": schema,
        "prefix": str(layout.prefix),
        "environment": str(layout.environment),
        "launcher": str(layout.launcher),
    }
    if schema.endswith("@2"):
        document["self_update"] = bool(self_update)
    Path(layout.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(layout.manifest).write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


class HostSelfUpdateEnrollmentTests(unittest.TestCase):
    def test_release_endpoint_comes_from_embedded_distribution_origin(self) -> None:
        from literate_ai.adapters import host_self_update

        with mock.patch(
            "literate_ai.adapters.standard_lifecycle_binding.observe_installed_framework_origin",
            return_value=SimpleNamespace(
                repository_url="git@github.com:public-owner/literate-ai.git"
            ),
        ):
            self.assertEqual(
                host_self_update.github_releases_latest_url(),
                "https://api.github.com/repos/public-owner/literate-ai/releases/latest",
            )

    def test_non_github_distribution_origin_disables_release_check(self) -> None:
        from literate_ai.adapters import host_self_update

        with mock.patch(
            "literate_ai.adapters.standard_lifecycle_binding.observe_installed_framework_origin",
            return_value=SimpleNamespace(
                repository_url="https://git.example.org/public/literate-ai.git"
            ),
        ):
            self.assertIsNone(host_self_update.github_releases_latest_url())

    def test_enrolls_only_when_flag_manifest_and_venv_python_match(self) -> None:
        from literate_ai.adapters.host_self_update import (
            resolve_host_self_update_enrollment,
        )

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            layout = HostInstallLayout.for_prefix(prefix)
            _write_manifest(layout, schema=HOST_INSTALL_MANIFEST_SCHEMA)
            python = _python_path(layout)
            enrolled = resolve_host_self_update_enrollment(
                environ={
                    "LITAI_HOST_INSTALL": "1",
                    "LITAI_PREFIX": str(prefix),
                },
                executable=python,
            )
            self.assertIsNotNone(enrolled)
            assert enrolled is not None
            self.assertEqual(
                enrolled.staging_root, Path(layout.application_root) / "self-update"
            )
            self.assertEqual(enrolled.prefix, prefix)
            self.assertEqual(enrolled.environment, Path(layout.environment))

    def test_skips_v1_manifest_checkout_ci_and_opt_out(self) -> None:
        from literate_ai.adapters.host_self_update import (
            resolve_host_self_update_enrollment,
        )

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            layout = HostInstallLayout.for_prefix(prefix)
            python = _python_path(layout)
            _write_manifest(
                layout, schema="literate-ai/host-install-manifest@1", self_update=None
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={
                        "LITAI_HOST_INSTALL": "1",
                        "LITAI_PREFIX": str(prefix),
                    },
                    executable=python,
                )
            )
            _write_manifest(layout, schema=HOST_INSTALL_MANIFEST_SCHEMA)
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={
                        "LITAI_HOST_INSTALL": "1",
                        "LITAI_PREFIX": str(prefix),
                        "LITAI_NO_SELF_UPDATE": "1",
                    },
                    executable=python,
                )
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={
                        "LITAI_HOST_INSTALL": "1",
                        "LITAI_PREFIX": str(prefix),
                        "CI": "true",
                    },
                    executable=python,
                )
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={
                        "LITAI_HOST_INSTALL": "1",
                        "LITAI_PREFIX": str(prefix),
                        "GITHUB_ACTIONS": "1",
                    },
                    executable=python,
                )
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={
                        "LITAI_HOST_INSTALL": "1",
                        "LITAI_PREFIX": str(prefix),
                        "LITAI_EVIDENCE_RUN": "1",
                    },
                    executable=python,
                )
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={
                        "LITAI_HOST_INSTALL": "1",
                        "LITAI_PREFIX": str(prefix),
                        "LITAI_SELF_UPDATE_REEXEC": "1",
                    },
                    executable=python,
                )
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={"LITAI_PREFIX": str(prefix)},
                    executable=python,
                )
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={
                        "LITAI_HOST_INSTALL": "1",
                        "LITAI_PREFIX": str(prefix),
                    },
                    executable=Path(sys.executable),
                )
            )


def _release_payload(
    tag: str, *, draft: bool = False, prerelease: bool = False, wheel: str | None = None
) -> dict[str, object]:
    name = (
        wheel
        if wheel is not None
        else f"literate_ai-{tag.lstrip('v')}-py3-none-any.whl"
    )
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {
                "name": name,
                "browser_download_url": f"https://github.example.invalid/{name}",
            }
        ],
    }


class HostSelfUpdateSelectionTests(unittest.TestCase):
    def test_selects_newer_wheel_and_rejects_downgrade_prerelease_and_missing_asset(
        self,
    ) -> None:
        from literate_ai.adapters.host_self_update import select_github_release_wheel

        plan = select_github_release_wheel(
            _release_payload("v0.12.0"), installed_version="0.11.0"
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.version, "0.12.0")
        self.assertEqual(plan.wheel_name, "literate_ai-0.12.0-py3-none-any.whl")
        self.assertIsNone(
            select_github_release_wheel(
                _release_payload("v0.11.0"), installed_version="0.11.0"
            )
        )
        self.assertIsNone(
            select_github_release_wheel(
                _release_payload("v0.10.0"), installed_version="0.11.0"
            )
        )
        self.assertIsNone(
            select_github_release_wheel(
                _release_payload("v0.12.0", prerelease=True),
                installed_version="0.11.0",
            )
        )
        self.assertIsNone(
            select_github_release_wheel(
                _release_payload("v0.12.0", draft=True),
                installed_version="0.11.0",
            )
        )
        self.assertIsNone(
            select_github_release_wheel(
                _release_payload("v0.12.0", wheel="notes.md"),
                installed_version="0.11.0",
            )
        )

    def test_cache_throttle_success_failure_and_staged_newer(self) -> None:
        from literate_ai.adapters.host_self_update import (
            FAILURE_RETRY_SECONDS,
            SUCCESS_CACHE_SECONDS,
            cache_allows_github_check,
        )

        now = 1_000_000.0
        self.assertFalse(
            cache_allows_github_check({"checked_at": now - 60, "ok": True}, now=now)
        )
        self.assertTrue(
            cache_allows_github_check(
                {"checked_at": now - SUCCESS_CACHE_SECONDS - 1, "ok": True},
                now=now,
            )
        )
        self.assertFalse(
            cache_allows_github_check({"checked_at": now - 60, "ok": False}, now=now)
        )
        self.assertTrue(
            cache_allows_github_check(
                {"checked_at": now - FAILURE_RETRY_SECONDS - 1, "ok": False},
                now=now,
            )
        )
        self.assertTrue(cache_allows_github_check({}, now=now))
        self.assertFalse(
            cache_allows_github_check(
                {"checked_at": now - 10, "ok": True},
                now=now,
                staged_newer=True,
            )
        )


class HostSelfUpdateStagingTests(unittest.TestCase):
    def _enrollment(self, prefix: Path):
        from literate_ai.adapters.host_self_update import (
            HostSelfUpdateEnrollment,
            write_host_install_manifest,
        )

        layout = HostInstallLayout.for_prefix(prefix)
        python = _python_path(layout)
        _write_manifest(layout, schema=HOST_INSTALL_MANIFEST_SCHEMA)
        write_host_install_manifest(
            Path(layout.manifest),
            prefix=prefix,
            environment=Path(layout.environment),
            launcher=Path(layout.launcher),
        )
        return (
            HostSelfUpdateEnrollment(
                prefix,
                Path(layout.environment),
                Path(layout.launcher),
                Path(layout.manifest),
                Path(layout.application_root) / "self-update",
            ),
            python,
            layout,
        )

    def test_stage_writes_wheel_without_touching_venv(self) -> None:
        from literate_ai.adapters.host_self_update import stage_github_wheel

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            enrollment, _python, layout = self._enrollment(prefix)
            marker = Path(layout.environment) / "runtime-marker"
            marker.write_text("keep\n", encoding="utf-8")
            fetches: list[str] = []

            def fetch_json(url: str, headers: object, timeout: int) -> bytes:
                fetches.append(url)
                self.assertEqual(timeout, 10)
                return json.dumps(_release_payload("v0.12.0")).encode("utf-8")

            def fetch_file(
                url: str, headers: object, timeout: int, destination: Path
            ) -> None:
                fetches.append(url)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"wheel-bytes")

            stage_github_wheel(
                enrollment,
                fetch_json=fetch_json,
                fetch_file=fetch_file,
                now=1_000_000.0,
                installed_version="0.11.0",
                environ={},
                releases_url="https://api.github.com/repos/example/literate-ai/releases/latest",
            )
            wheel = enrollment.staging_root / "literate_ai-0.12.0-py3-none-any.whl"
            self.assertEqual(wheel.read_bytes(), b"wheel-bytes")
            cache = json.loads(
                (enrollment.staging_root / "cache.json").read_text(encoding="utf-8")
            )
            self.assertTrue(cache["ok"])
            self.assertEqual(cache["version"], "0.12.0")
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(len(fetches), 2)

            stage_github_wheel(
                enrollment,
                fetch_json=fetch_json,
                fetch_file=fetch_file,
                now=1_000_060.0,
                installed_version="0.11.0",
                environ={},
                releases_url="https://api.github.com/repos/example/literate-ai/releases/latest",
            )
            self.assertEqual(len(fetches), 2)

    def test_stage_records_failure_without_raising(self) -> None:
        from literate_ai.adapters.host_self_update import stage_github_wheel

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            enrollment, _python, _layout = self._enrollment(prefix)

            def fetch_json(url: str, headers: object, timeout: int) -> bytes:
                raise TimeoutError("github down")

            stage_github_wheel(
                enrollment,
                fetch_json=fetch_json,
                fetch_file=lambda *args: None,
                now=1.0,
                installed_version="0.11.0",
                environ={},
                releases_url="https://api.github.com/repos/example/literate-ai/releases/latest",
            )
            cache = json.loads(
                (enrollment.staging_root / "cache.json").read_text(encoding="utf-8")
            )
            self.assertFalse(cache["ok"])

    def test_spawn_skips_when_lock_held(self) -> None:
        from literate_ai.adapters.host_self_update import (
            release_exclusive_lock,
            spawn_self_update_worker,
            try_exclusive_lock,
        )

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            enrollment, python, _layout = self._enrollment(prefix)
            held = try_exclusive_lock(enrollment.staging_root / "worker.lock")
            self.assertIsNotNone(held)
            assert held is not None
            try:
                started: list[object] = []
                spawn_self_update_worker(
                    enrollment,
                    argv_executable=python,
                    popen=lambda *args, **kwargs: started.append((args, kwargs)),
                )
                self.assertEqual(started, [])
            finally:
                release_exclusive_lock(held)

    def test_spawn_starts_detached_worker_logging_to_staging(self) -> None:
        from literate_ai.adapters.host_self_update import spawn_self_update_worker

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            enrollment, python, _layout = self._enrollment(prefix)
            started: list[tuple] = []
            spawn_self_update_worker(
                enrollment,
                argv_executable=python,
                popen=lambda *args, **kwargs: started.append((args, kwargs)),
            )
            self.assertEqual(len(started), 1)
            command, kwargs = started[0][0][0], started[0][1]
            self.assertEqual(
                command[1:5],
                (
                    "-m",
                    "literate_ai.adapters.host_self_update",
                    "--stage",
                    str(prefix),
                ),
            )
            if os.name == "nt":
                self.assertIn("creationflags", kwargs)
            else:
                self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(
                kwargs["stdout"].name, str(enrollment.staging_root / "worker.log")
            )
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_stage_cli_skips_when_worker_lock_held(self) -> None:
        from literate_ai.adapters.host_self_update import (
            _stage_cli,
            release_exclusive_lock,
            try_exclusive_lock,
        )

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            enrollment, _python, _layout = self._enrollment(prefix)
            held = try_exclusive_lock(enrollment.staging_root / "worker.lock")
            self.assertIsNotNone(held)
            assert held is not None
            try:
                with mock.patch(
                    "literate_ai.adapters.host_self_update.stage_github_wheel"
                ) as staged:
                    self.assertEqual(_stage_cli(str(prefix)), 0)
                staged.assert_not_called()
            finally:
                release_exclusive_lock(held)


class HostSelfUpdateApplyTests(unittest.TestCase):
    def test_pip_preserves_cache_selection_without_import_path_injection(self) -> None:
        from literate_ai.adapters.host_self_update import _pip_environment

        self.assertEqual(
            _pip_environment(
                {
                    "PATH": "/bin",
                    "PYTHONPYCACHEPREFIX": "/operator-cache",
                    "PYTHONOPTIMIZE": "1",
                    "PYTHONPATH": "/untrusted-imports",
                    "PYTHONHOME": "/untrusted-interpreter",
                    "PYTHONSTARTUP": "/untrusted-startup",
                }
            ),
            {
                "PATH": "/bin",
                "PYTHONPYCACHEPREFIX": "/operator-cache",
                "PYTHONOPTIMIZE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )

    def test_apply_installs_and_reexecs_original_argv(self) -> None:
        from literate_ai.adapters.host_self_update import (
            apply_staged_wheel,
            maybe_host_self_update,
            write_host_install_manifest,
        )

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            layout = HostInstallLayout.for_prefix(prefix)
            _python_path(layout)
            write_host_install_manifest(
                Path(layout.manifest),
                prefix=prefix,
                environment=Path(layout.environment),
                launcher=Path(layout.launcher),
            )
            Path(layout.launcher).parent.mkdir(parents=True, exist_ok=True)
            Path(layout.launcher).write_text("old-launcher\n", encoding="utf-8")
            staging = Path(layout.application_root) / "self-update"
            staging.mkdir(parents=True)
            wheel = staging / "literate_ai-0.12.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            (staging / "cache.json").write_text(
                json.dumps(
                    {
                        "checked_at": 1.0,
                        "ok": True,
                        "tag": "v0.12.0",
                        "version": "0.12.0",
                        "wheel_name": wheel.name,
                        "wheel_url": "https://github.example.invalid/wheel",
                    }
                ),
                encoding="utf-8",
            )
            from literate_ai.adapters.host_self_update import HostSelfUpdateEnrollment

            enrollment = HostSelfUpdateEnrollment(
                prefix,
                Path(layout.environment),
                Path(layout.launcher),
                Path(layout.manifest),
                staging,
            )
            ran: list[tuple] = []
            execs: list[tuple] = []
            stderr = io.StringIO()

            def runner(*args: str, timeout: int, env: dict[str, str]) -> object:
                ran.append((args, timeout, env))
                return type("Result", (), {"returncode": 0})()

            def exec_fn(
                command: list[str] | tuple[str, ...], env: dict[str, str]
            ) -> None:
                execs.append((tuple(command), dict(env)))

            apply_staged_wheel(
                enrollment,
                argv=("help", "--json"),
                installed_version="0.11.0",
                runner=runner,
                exec_fn=exec_fn,
                stderr=stderr,
                environ={"PATH": "/bin"},
            )
            self.assertEqual(len(ran), 1)
            self.assertIn(str(wheel), ran[0][0])
            self.assertIn("--no-index", ran[0][0])
            self.assertIn("--no-deps", ran[0][0])
            self.assertIn("--compile", ran[0][0])
            self.assertEqual(ran[0][1], 120)
            self.assertEqual(len(execs), 1)
            self.assertEqual(execs[0][0][0], str(layout.launcher))
            self.assertEqual(execs[0][0][1:], ("help", "--json"))
            self.assertEqual(execs[0][1]["LITAI_SELF_UPDATE_REEXEC"], "1")
            self.assertIn("0.11.0", stderr.getvalue())
            self.assertIn("0.12.0", stderr.getvalue())

            execs.clear()
            ran.clear()
            apply_staged_wheel(
                enrollment,
                argv=("help",),
                installed_version="0.12.0",
                runner=runner,
                exec_fn=exec_fn,
                stderr=stderr,
                environ={},
            )
            self.assertEqual(ran, [])
            self.assertEqual(execs, [])

            spawned: list[object] = []
            maybe_host_self_update(
                ["version"],
                environ={"PATH": "/bin"},
                executable=Path(sys.executable),
                stderr=stderr,
                spawn=lambda *args, **kwargs: spawned.append(True),
            )
            self.assertEqual(spawned, [])

    def test_apply_skips_when_lock_busy_or_pip_fails(self) -> None:
        from literate_ai.adapters.host_self_update import (
            HostSelfUpdateEnrollment,
            apply_staged_wheel,
            try_exclusive_lock,
            write_host_install_manifest,
        )

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            layout = HostInstallLayout.for_prefix(prefix)
            _python_path(layout)
            write_host_install_manifest(
                Path(layout.manifest),
                prefix=prefix,
                environment=Path(layout.environment),
                launcher=Path(layout.launcher),
            )
            Path(layout.launcher).parent.mkdir(parents=True, exist_ok=True)
            Path(layout.launcher).write_text("old-launcher\n", encoding="utf-8")
            staging = Path(layout.application_root) / "self-update"
            staging.mkdir(parents=True)
            wheel = staging / "literate_ai-0.12.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            (staging / "cache.json").write_text(
                json.dumps(
                    {
                        "checked_at": 1.0,
                        "ok": True,
                        "tag": "v0.12.0",
                        "version": "0.12.0",
                        "wheel_name": wheel.name,
                        "wheel_url": "https://github.example.invalid/wheel",
                    }
                ),
                encoding="utf-8",
            )
            enrollment = HostSelfUpdateEnrollment(
                prefix,
                Path(layout.environment),
                Path(layout.launcher),
                Path(layout.manifest),
                staging,
            )
            held = try_exclusive_lock(staging / "apply.lock")
            self.assertIsNotNone(held)
            ran: list[object] = []
            execs: list[object] = []
            stderr = io.StringIO()

            def runner(*args: str, timeout: int, env: dict[str, str]) -> object:
                ran.append(args)
                return type("Result", (), {"returncode": 1})()

            def exec_fn(command: object, env: object) -> None:
                execs.append(command)

            apply_staged_wheel(
                enrollment,
                argv=("help",),
                installed_version="0.11.0",
                runner=runner,
                exec_fn=exec_fn,
                stderr=stderr,
                environ={},
            )
            self.assertEqual(ran, [])
            self.assertEqual(execs, [])

            from literate_ai.adapters.host_self_update import release_exclusive_lock

            assert held is not None
            release_exclusive_lock(held)
            apply_staged_wheel(
                enrollment,
                argv=("help",),
                installed_version="0.11.0",
                runner=runner,
                exec_fn=exec_fn,
                stderr=stderr,
                environ={},
            )
            self.assertEqual(len(ran), 1)
            self.assertEqual(execs, [])
            self.assertIn("failed", stderr.getvalue())


class HostSelfUpdateLauncherTests(unittest.TestCase):
    def test_embedded_launchers_match_scripts(self) -> None:
        from literate_ai.adapters.host_self_update import (
            POSIX_LAUNCHER,
            WINDOWS_LAUNCHER,
        )

        root = Path(__file__).resolve().parents[2]
        self.assertEqual(
            POSIX_LAUNCHER.splitlines(),
            (root / "scripts" / "litai-launcher")
            .read_text(encoding="utf-8")
            .splitlines(),
        )
        self.assertEqual(
            WINDOWS_LAUNCHER.splitlines(),
            (root / "scripts" / "litai-launcher.cmd")
            .read_text(encoding="utf-8")
            .splitlines(),
        )


class HostSelfUpdateDispatchHookTests(unittest.TestCase):
    def test_dispatch_source_calls_hook_before_parser(self) -> None:
        text = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "literate_ai"
            / "cli"
            / "dispatch.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "from literate_ai.adapters.host_self_update import maybe_host_self_update",
            text,
        )
        hook = text.index("maybe_host_self_update(raw)")
        parser = text.index("parser.parse_args(arguments)")
        self.assertLess(hook, parser)

    def test_main_invokes_self_update_before_help(self) -> None:
        try:
            from literate_ai.cli.dispatch import main
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(str(exc)) from exc

        seen: list[list[str]] = []

        def fake(argv, **kwargs) -> None:
            seen.append(list(argv))

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("literate_ai.cli.dispatch.maybe_host_self_update", fake):
            status = main(["help"], stdout=stdout, stderr=stderr)
        self.assertEqual(status, 0)
        self.assertEqual(seen, [["help"]])
        self.assertTrue(stdout.getvalue())


class HostSelfUpdateInstallCheckSilenceTests(unittest.TestCase):
    def test_prefix_install_drivers_opt_out_of_github_self_update(self) -> None:
        root = Path(__file__).resolve().parents[2]
        smoke = (root / "scripts" / "installed_project_smoke.py").read_text(
            encoding="utf-8"
        )
        e2e = (root / "scripts" / "installed_e2e_gate.py").read_text(encoding="utf-8")
        roundtrip = (root / "scripts" / "installed_roundtrip.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('environment["LITAI_NO_SELF_UPDATE"] = "1"', smoke)
        self.assertIn('"LITAI_NO_SELF_UPDATE": "1"', e2e)
        self.assertIn('environment["LITAI_NO_SELF_UPDATE"] = "1"', roundtrip)
