"""Parent repository checkout under parents/<id> in the current project."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.parent_checkout import parent_prefix_id
from literate_ai.cli import main
from tests.unit.test_project_cli import invoke

_PLAIN_TEXT_ENVIRONMENT = {
    key: value
    for key, value in os.environ.items()
    if key not in {"FORCE_COLOR", "COLORTERM", "CLICOLOR", "CLICOLOR_FORCE"}
} | {"NO_COLOR": "1", "TERM": "dumb"}


def _git(root: Path, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["GIT_AUTHOR_NAME"] = "parent-test"
    environment["GIT_AUTHOR_EMAIL"] = "parent-test@example.com"
    environment["GIT_COMMITTER_NAME"] = "parent-test"
    environment["GIT_COMMITTER_EMAIL"] = "parent-test@example.com"
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        env=environment,
    )


def _commit_file(root: Path, relative: str, content: str, message: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", relative)
    _git(root, "commit", "--quiet", "-m", message)


class ParentPrefixIdTests(unittest.TestCase):
    def test_uses_repository_name_without_git_suffix(self) -> None:
        self.assertEqual(
            parent_prefix_id("https://github.com/NVIDIA-dev/literate-ai.git"),
            "literate-ai",
        )
        self.assertEqual(
            parent_prefix_id("git@gitlab.internal.example:org/platform.git"),
            "platform",
        )


class ParentCheckoutCliTests(unittest.TestCase):
    def test_clones_a_parent_under_parents_and_updates_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "upstream"
            child = root / "derived"
            parent.mkdir()
            child.mkdir()
            _git(parent, "init", "--quiet", "-b", "main")
            _git(parent, "config", "user.email", "parent-test@example.com")
            _git(parent, "config", "user.name", "parent-test")
            _commit_file(parent, "README.md", "one\n", "init")
            _git(child, "init", "--quiet", "-b", "main")
            _git(child, "config", "user.email", "parent-test@example.com")
            _git(child, "config", "user.name", "parent-test")
            _commit_file(child, "CHILD.md", "child\n", "child")

            status, envelope = invoke(
                "project",
                "parent",
                "checkout",
                parent.resolve().as_uri(),
                "--project",
                str(child),
            )
            self.assertEqual(status, 0, envelope)
            result = envelope["result"]
            self.assertEqual(result["action"], "added")
            self.assertEqual(result["prefix"], "parents/upstream")
            self.assertEqual(
                result["repository_fetch_deadline"]["policy"]["total_seconds"],
                3600,
            )
            self.assertEqual(
                result["repository_fetch_deadline"]["policy"]["no_progress_seconds"],
                600,
            )
            checkout = child / "parents" / "upstream"
            self.assertEqual(
                (checkout / "README.md").read_text(encoding="utf-8"), "one\n"
            )
            exclude = subprocess.run(
                ("git", "-C", str(child), "rev-parse", "--git-path", "info/exclude"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            exclude_path = (
                Path(exclude) if Path(exclude).is_absolute() else child / exclude
            )
            self.assertIn("parents/", exclude_path.read_text(encoding="utf-8"))

            _commit_file(parent, "README.md", "two\n", "update")
            status, envelope = invoke(
                "project",
                "parent",
                "checkout",
                parent.resolve().as_uri() + "#main",
                "--project",
                str(child),
                "--repository-fetch-total-seconds",
                "7200",
                "--repository-fetch-no-progress-seconds",
                "900",
                "--repository-fetch-connect-seconds",
                "60",
            )
            self.assertEqual(status, 0, envelope)
            self.assertEqual(envelope["result"]["action"], "updated")
            self.assertEqual(
                set(
                    envelope["result"]["repository_fetch_deadline"][
                        "field_provenance"
                    ].values()
                ),
                {"cli"},
            )
            self.assertEqual(
                (checkout / "README.md").read_text(encoding="utf-8"), "two\n"
            )

    def test_initializes_nested_submodules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "module"
            parent = root / "upstream"
            child = root / "derived"
            module.mkdir()
            parent.mkdir()
            child.mkdir()
            _git(module, "init", "--quiet", "-b", "main")
            _git(module, "config", "user.email", "parent-test@example.com")
            _git(module, "config", "user.name", "parent-test")
            _commit_file(module, "nested.txt", "nested\n", "module")
            _git(parent, "init", "--quiet", "-b", "main")
            _git(parent, "config", "user.email", "parent-test@example.com")
            _git(parent, "config", "user.name", "parent-test")
            _commit_file(parent, "README.md", "one\n", "init")
            _git(
                parent,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--quiet",
                module.resolve().as_uri(),
                "vendor/module",
            )
            _git(parent, "commit", "--quiet", "-m", "add submodule")
            _git(child, "init", "--quiet", "-b", "main")
            _git(child, "config", "user.email", "parent-test@example.com")
            _git(child, "config", "user.name", "parent-test")
            _commit_file(child, "CHILD.md", "child\n", "child")

            status, envelope = invoke(
                "project",
                "parent",
                "checkout",
                parent.resolve().as_uri(),
                "--project",
                str(child),
            )
            self.assertEqual(status, 0, envelope)
            nested = child / "parents" / "upstream" / "vendor" / "module" / "nested.txt"
            self.assertEqual(nested.read_text(encoding="utf-8"), "nested\n")
            self.assertTrue(envelope["result"]["submodules"])

    def test_rejects_urls_with_embedded_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory)
            _git(child, "init", "--quiet", "-b", "main")
            status, envelope = invoke(
                "project",
                "parent",
                "checkout",
                "https://user:token@github.com/org/repo.git",
                "--project",
                str(child),
            )
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"], "project.parent_url_credentials"
            )

    def test_help_names_checkout(self) -> None:
        stdout = __import__("io").StringIO()
        stderr = __import__("io").StringIO()
        with mock.patch.dict(os.environ, _PLAIN_TEXT_ENVIRONMENT, clear=True):
            status = main(
                ("project", "parent", "checkout", "help"),
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(status, 0)
        self.assertIn("usage: litai project parent checkout", stdout.getvalue())

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_clone_reaps_git_descendant_holding_output_streams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "upstream"
            child = root / "derived"
            parent.mkdir()
            child.mkdir()
            _git(parent, "init", "--quiet", "-b", "main")
            _git(parent, "config", "user.email", "parent-test@example.com")
            _git(parent, "config", "user.name", "parent-test")
            _commit_file(parent, "README.md", "one\n", "init")
            _git(child, "init", "--quiet", "-b", "main")
            _git(child, "config", "user.email", "parent-test@example.com")
            _git(child, "config", "user.name", "parent-test")
            _commit_file(child, "CHILD.md", "child\n", "child")
            real_git = shutil.which("git")
            assert real_git is not None
            wrapper_dir = root / "bin"
            wrapper_dir.mkdir()
            wrapper = wrapper_dir / "git"
            wrapper.write_text(
                "#!" + sys.executable + "\n"
                "import subprocess, sys\n"
                f"result = subprocess.run([{real_git!r}, *sys.argv[1:]])\n"
                "subprocess.Popen("
                "[sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "raise SystemExit(result.returncode)\n",
                encoding="utf-8",
                newline="\n",
            )
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment["PATH"] = str(wrapper_dir) + os.pathsep + environment["PATH"]
            started = time.monotonic()
            with mock.patch.dict(os.environ, environment, clear=False):
                status, envelope = invoke(
                    "project",
                    "parent",
                    "checkout",
                    parent.resolve().as_uri(),
                    "--project",
                    str(child),
                )
            self.assertEqual(status, 0, envelope)
            self.assertLess(time.monotonic() - started, 45)
            self.assertEqual(
                (child / "parents" / "upstream" / "README.md").read_text(
                    encoding="utf-8"
                ),
                "one\n",
            )


if __name__ == "__main__":
    unittest.main()
