"""Unit tests for installed-wheel smoke-test source custody."""

from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from scripts.wheel_smoke import (
    _remotes_stripped_parent,
    _subprocess_environment,
    assert_installed_graph_exports,
    project_clean_source,
)
from tools.build_backend import literate_ai_build_backend as build_backend
from tools.build_backend.literate_ai_build_backend import distribution_origin_bytes


class CleanWheelSourceProjectionTests(unittest.TestCase):
    def test_console_entrypoint_uses_the_cli_dispatch_module(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        definition = tomllib.loads(
            (repository / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            definition["project"]["scripts"]["litai"], "literate_ai.cli:main"
        )

    def test_every_wheel_smoke_project_creation_selects_the_local_parent(self) -> None:
        source = Path(__file__).resolve().parents[2] / "scripts/wheel_smoke.py"
        text = source.read_text(encoding="utf-8")

        self.assertEqual(text.count('"--from",'), 4)
        self.assertIn('"onboard",\n        "create",', text)
        self.assertIn('"onboard",\n        "adopt",', text)

    def test_scoped_init_checks_selectors_not_inherited_catalog_presence(self) -> None:
        source = Path(__file__).resolve().parents[2] / "scripts/wheel_smoke.py"
        text = source.read_text(encoding="utf-8")

        self.assertIn('scoped_manifest.get("default_flavor_selectors")', text)
        self.assertNotIn("installed unexpected lang-javascript Flavor files", text)

    def test_parent_clone_does_not_request_local_hardlinks(self) -> None:
        completed = subprocess.CompletedProcess(("git",), 0, "", "")
        with mock.patch("scripts.wheel_smoke.run", return_value=completed) as run:
            locator = _remotes_stripped_parent(
                Path("repository"), Path("parent.git"), "a" * 40
            )

        self.assertEqual(
            run.call_args_list[0].args[1:4], ("clone", "--bare", "--no-local")
        )
        self.assertTrue(locator.endswith("#" + "a" * 40))

    def test_subprocess_environment_drops_interpreter_resolution_vars(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "PYTHONPATH": "src",
                "PYTHONHOME": "/opt/python",
                "VIRTUAL_ENV": "/tmp/session-venv",
                "FORCE_COLOR": "1",
                "KEEP": "yes",
            },
            clear=False,
        ):
            env = _subprocess_environment()
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertNotIn("FORCE_COLOR", env)
        self.assertEqual(env["NO_COLOR"], "1")
        self.assertEqual(env["KEEP"], "yes")

    def test_installed_graph_exports_share_one_canonical_graph(self) -> None:
        graph = {
            "schema": "literate-ai/authority-graph@2",
            "project_id": "example",
            "nodes": [{"id": "repository:example"}],
            "edges": [],
        }
        markers = {
            "json": '{"schema":"literate-ai/authority-graph@2"}',
            "text": "project example\n",
            "mermaid": "flowchart LR\n",
            "dot": "digraph literate_ai {\n}\n",
            "svg": '<svg xmlns="http://www.w3.org/2000/svg"></svg>\n',
        }

        def completed(*arguments: str, **_kwargs) -> subprocess.CompletedProcess[str]:
            format_name = arguments[arguments.index("--format") + 1]
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "schema": "literate-ai/authority-graph@2",
                            "graph": graph,
                            "rendered": markers[format_name],
                        },
                    }
                ),
                "",
            )

        with mock.patch("scripts.wheel_smoke.run", side_effect=completed):
            evidence = assert_installed_graph_exports(Path("litai"), Path("project"))

        self.assertEqual(
            evidence,
            {
                "formats": ["dot", "json", "mermaid", "svg", "text"],
                "nodes": 1,
                "edges": 0,
            },
        )

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

    def test_wheel_backend_removes_stale_package_data_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "_build/setuptools/lib/literate_ai/builtin/skill.json"
            stale.parent.mkdir(parents=True)
            stale.write_text("{}\n", encoding="utf-8")

            with mock.patch.object(build_backend, "_ROOT", root):
                build_backend._reset_setuptools_wheel_staging()

            self.assertFalse((root / "_build/setuptools/lib").exists())

    def test_wheel_backend_rejects_linked_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "outside"
            target.mkdir()
            staging = root / "_build/setuptools/lib"
            staging.parent.mkdir(parents=True)
            try:
                staging.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("host cannot create directory symbolic links")

            with (
                mock.patch.object(build_backend, "_ROOT", root),
                self.assertRaisesRegex(RuntimeError, "symbolic link"),
            ):
                build_backend._reset_setuptools_wheel_staging()

            self.assertTrue(target.is_dir())

    def test_distribution_origin_is_resolved_before_the_build_lock_is_created(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "src/literate_ai/_distribution_origin.json"
            lock = root / ".literate-ai-distribution-origin.lock"
            origin.parent.mkdir(parents=True)

            def git_value(*arguments: str) -> str | None:
                self.assertFalse(lock.exists())
                if arguments[0] == "status":
                    return None
                if arguments[0] == "remote":
                    return "ssh://git.example.test/operator/literate-ai.git"
                return "a" * 40

            with (
                mock.patch.object(build_backend, "_ROOT", root),
                mock.patch.object(build_backend, "_ORIGIN", origin),
                mock.patch.object(build_backend, "_LOCK", lock),
                mock.patch.object(build_backend, "_git", side_effect=git_value),
                build_backend._embedded_distribution_origin(),
            ):
                self.assertTrue(lock.is_file())
                self.assertTrue(origin.is_file())

            self.assertFalse(lock.exists())
            self.assertFalse(origin.exists())

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

    def test_equivalent_clone_transports_embed_identical_origin_bytes(self) -> None:
        def origin(url):
            return distribution_origin_bytes(
                {
                    "LITAI_BUILD_REPOSITORY_URL": url,
                    "LITAI_BUILD_GIT_REVISION": "a" * 40,
                }
            )

        expected = origin("https://github.com/example/project")
        for alias in (
            "https://github.com/example/project.git",
            "https://github.com:443/example/project.git/",
            "git@github.com:example/project.git",
            "ssh://git@github.com/example/project.git",
            "ssh://git@github.com:22/example/project/",
        ):
            with self.subTest(alias=alias):
                self.assertEqual(origin(alias), expected)
        for distinct, expected_origin in (
            (
                "https://github.com/another/project.git",
                "https://github.com/another/project",
            ),
            (
                "https://github.com/example/other.git",
                "https://github.com/example/other",
            ),
            (
                "ssh://git@github.com:2222/example/project.git",
                "ssh://git@github.com:2222/example/project.git",
            ),
            (
                "https://git.example.test/example/project.git",
                "https://git.example.test/example/project.git",
            ),
            ("file:///projects/example/project", "file:///projects/example/project"),
        ):
            with self.subTest(distinct=distinct):
                self.assertNotEqual(origin(distinct), expected)
                self.assertEqual(
                    json.loads(origin(distinct))["repository_url"], expected_origin
                )

    def test_projects_only_git_visible_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            destination = root / "projection"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
                capture_output=True,
            )
            (repository / ".gitignore").write_text(
                "/private.json\n/_build/\n", encoding="utf-8"
            )
            (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            (repository / "intentional.txt").write_text(
                "untracked but visible\n", encoding="utf-8"
            )
            (repository / "private.json").write_text("secret\n", encoding="utf-8")
            staging = repository / "_build" / "setuptools" / "lib"
            staging.mkdir(parents=True)
            (staging / "stale.py").write_text("stale\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repository), "add", ".gitignore", "tracked.txt"],
                check=True,
                capture_output=True,
            )

            project_clean_source(repository, destination)

            self.assertEqual(
                sorted(
                    path.relative_to(destination).as_posix()
                    for path in destination.rglob("*")
                    if path.is_file()
                ),
                [".gitignore", "intentional.txt", "tracked.txt"],
            )
            self.assertEqual(
                (destination / "intentional.txt").read_text(encoding="utf-8"),
                "untracked but visible\n",
            )


if __name__ == "__main__":
    unittest.main()
