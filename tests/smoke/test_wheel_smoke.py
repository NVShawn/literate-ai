"""Unit tests for installed-wheel smoke-test source custody."""

from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from threading import Event
from unittest import mock

from scripts import wheel_smoke
from scripts.installed_html_smoke import _command_timeout
from scripts.wheel_smoke import _host_flavor
from tools.build_backend.literate_ai_build_backend import distribution_origin_bytes


class CleanWheelSourceProjectionTests(unittest.TestCase):
    def test_concurrent_timing_writes_hold_one_guard_and_keep_every_span(self) -> None:
        entered = Event()
        release = Event()
        writer = wheel_smoke._write_canonical_json

        def blocked_write(path: Path, value: dict[str, object]) -> None:
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("timing writer was not released")
            writer(path, value)

        def record(label: str) -> None:
            with wheel_smoke._timed(label):
                pass

        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "timings.json"
            with (
                mock.patch.object(wheel_smoke, "_TIMING_REPORT", report),
                mock.patch.object(wheel_smoke, "_TIMINGS", []),
                mock.patch.object(
                    wheel_smoke, "_write_canonical_json", side_effect=blocked_write
                ),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                first = pool.submit(record, "first")
                try:
                    self.assertTrue(entered.wait(5), "first writer did not start")
                    second = pool.submit(record, "second")
                    acquired = wheel_smoke._TIMING_LOCK.acquire(blocking=False)
                    if acquired:
                        wheel_smoke._TIMING_LOCK.release()
                    self.assertFalse(acquired, "timing replacement was not guarded")
                finally:
                    release.set()
                first.result(timeout=5)
                second.result(timeout=5)
            spans = json.loads(report.read_bytes())["spans"]
            self.assertEqual(
                sorted(item["stage"] for item in spans), ["first", "second"]
            )
            self.assertTrue(all(item["successful"] for item in spans))
            self.assertFalse(report.with_name("timings.json.tmp").exists())

    def test_fixture_parent_fork_survives_other_branch_ref_changes(self) -> None:
        def git(*args: str) -> str:
            return subprocess.check_output(["git", *args], text=True).strip()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            git("-C", str(source), "init", "-q", "-b", "main")
            git("-C", str(source), "config", "user.name", "Fixture")
            git("-C", str(source), "config", "user.email", "fixture@example.test")
            (source / "input.txt").write_text("original\n")
            git("-C", str(source), "add", "input.txt")
            git("-C", str(source), "commit", "-qm", "original")
            revision = git("-C", str(source), "rev-parse", "HEAD")
            html_parent = root / "html-parent.git"
            project_parent = root / "project-parent.git"
            wheel_smoke._remotes_stripped_parent(source, html_parent, revision)
            locator = wheel_smoke._remotes_stripped_parent(
                html_parent, project_parent, revision
            )
            self.assertEqual(locator, f"{project_parent.resolve().as_uri()}#{revision}")
            (source / "input.txt").write_text("HTML update\n")
            git("-C", str(source), "commit", "-qam", "advance HTML parent")
            git("-C", str(source), "push", "-q", str(html_parent), "HEAD:main")
            self.assertNotEqual(
                git("--git-dir", str(html_parent), "rev-parse", "HEAD"), revision
            )
            self.assertEqual(
                git("--git-dir", str(project_parent), "rev-parse", "HEAD"), revision
            )
            self.assertEqual(
                git("--git-dir", str(project_parent), "show", "HEAD:input.txt"),
                "original",
            )
            self.assertEqual(git("--git-dir", str(project_parent), "remote"), "")

    def test_partial_timing_write_preserves_completed_spans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "timings.json"
            with (
                mock.patch.object(wheel_smoke, "_TIMING_REPORT", report),
                mock.patch.object(wheel_smoke, "_TIMINGS", []),
            ):
                with wheel_smoke._timed("completed"):
                    pass
                retained = report.read_bytes()
                self.assertEqual(json.loads(retained)["spans"][0]["stage"], "completed")

                def partial_write(path: Path, value: dict[str, object]) -> None:
                    path.write_bytes(b'{"schema":')
                    raise OSError("interrupted timing write")

                with (
                    mock.patch.object(
                        wheel_smoke, "_write_canonical_json", side_effect=partial_write
                    ),
                    self.assertRaisesRegex(OSError, "interrupted timing write"),
                    wheel_smoke._timed("later"),
                ):
                    pass
                self.assertEqual(report.read_bytes(), retained)

    def test_installed_html_budget_remains_finite_on_every_supported_host(self) -> None:
        for platform_name, seconds in (
            ("win32", 900),
            ("freebsd", 900),
            ("freebsd14", 900),
            ("freebsd16", 900),
            ("darwin", 300),
            ("linux", 300),
        ):
            with self.subTest(platform_name=platform_name):
                self.assertEqual(_command_timeout(platform_name), seconds)

    def test_installed_wheel_expectations_keep_freebsd_explicit(self) -> None:
        for platform_name, expected in (
            ("win32", "windows"),
            ("darwin", "macos"),
            ("linux", "linux"),
            ("linux2", "linux"),
            ("freebsd", "freebsd"),
            ("freebsd14", "freebsd"),
            ("freebsd16", "freebsd"),
        ):
            with self.subTest(platform_name=platform_name):
                self.assertEqual(_host_flavor(platform_name), expected)

    def test_every_template_data_file_is_admitted_by_wheel_package_data(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        definition = tomllib.loads(
            (repository / "pyproject.toml").read_text(encoding="utf-8")
        )
        patterns = definition["tool"]["setuptools"]["package-data"][
            "literate_ai.project_template"
        ]
        template = repository / "src/literate_ai/project_template"
        uncovered = []
        for path in sorted(template.rglob("*")):
            if (
                not path.is_file()
                or path.suffix in {".py", ".pyc"}
                or "__pycache__" in path.parts
            ):
                continue
            relative = PurePosixPath(path.relative_to(template).as_posix())
            if not any(relative.match(pattern) for pattern in patterns):
                uncovered.append(relative.as_posix())

        self.assertEqual(uncovered, [])

    def test_distribution_origin_is_exact_and_rejects_credentials(self) -> None:
        document = json.loads(
            distribution_origin_bytes(
                {
                    "LITAI_BUILD_REPOSITORY_URL": (
                        "ssh://git.example.test/operator/literate-ai.git"
                    ),
                    "LITAI_BUILD_GIT_REVISION": "a" * 40,
                }
            )
        )
        self.assertEqual(
            document,
            {
                "schema": "literate-ai/distribution-origin@1",
                "repository_url": ("ssh://git.example.test/operator/literate-ai.git"),
                "git_revision": "a" * 40,
            },
        )
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            distribution_origin_bytes(
                {
                    "LITAI_BUILD_REPOSITORY_URL": (
                        "https://token@example.test/literate-ai.git"
                    ),
                    "LITAI_BUILD_GIT_REVISION": "a" * 40,
                }
            )


if __name__ == "__main__":
    unittest.main()
