"""Hermetic contracts for prefix-installed CLI self-update."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

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


class HostSelfUpdateApplyTests(unittest.TestCase):
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
