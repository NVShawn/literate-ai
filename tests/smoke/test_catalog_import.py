"""Tests for litai catalog copy DAG-safe composition."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.catalog_import import (
    CatalogImportError,
    catalog_copy,
)


def make_project(directory: Path, project_id: str, *, with_origin: bool = True) -> Path:
    """Create a minimal literate-ai project structure."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "literate.project.json").read_bytes()
    )
    manifest["project_id"] = project_id
    (directory / "literate.project.json").write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )
    (directory / ".literate").mkdir(exist_ok=True)
    if with_origin:
        origin = {
            "distribution_name": "literate-ai",
            "distribution_version": "0.2.0",
            "git_revision": "abc" * 13 + "a",
            "repository_url": "git@github.com:test/literate-ai.git",
            "schema": "urn:literate-ai:schema:v1:project-initialization-origin",
        }
        (directory / ".literate" / "initialization-origin.json").write_text(
            json.dumps(origin) + "\n", encoding="utf-8"
        )
    return directory


def add_flavor(project: Path, name: str) -> Path:
    flavor_dir = project / "flavors" / name
    flavor_dir.mkdir(parents=True, exist_ok=True)
    (flavor_dir / "flavor.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    (flavor_dir / "openspec").mkdir(exist_ok=True)
    (flavor_dir / "openspec" / "spec.md").write_text(
        f"# {name} spec\n", encoding="utf-8"
    )
    return flavor_dir


class DagAncestorTests(unittest.TestCase):
    def test_direct_cycle_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            proj_a = make_project(Path(d) / "a", "proj-a")
            proj_b = make_project(Path(d) / "b", "proj-b")
            add_flavor(proj_a, "flavor-from-b")
            add_flavor(proj_b, "flavor-from-a")

            # A copies from B — creates edge B→A
            catalog_copy(proj_a, str(proj_b), ["flavor:flavor-from-a"])

            # Now B tries to copy from A — would create A→B, forming cycle
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(proj_b, str(proj_a), ["flavor:flavor-from-b"])
            self.assertEqual(raised.exception.code, "catalog.import_cycle")


def _git(root: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment["GIT_AUTHOR_NAME"] = "catalog-test"
    environment["GIT_AUTHOR_EMAIL"] = "catalog-test@example.com"
    environment["GIT_COMMITTER_NAME"] = "catalog-test"
    environment["GIT_COMMITTER_EMAIL"] = "catalog-test@example.com"
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed.stdout.strip()


class GitCatalogSourceTests(unittest.TestCase):
    def test_git_source_pins_requested_revision(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            add_flavor(source, "myflavor")
            dest = make_project(Path(d) / "dest", "proj-dst")
            _git(source, "init", "--quiet", "-b", "main")
            _git(source, "config", "user.email", "catalog-test@example.com")
            _git(source, "config", "user.name", "catalog-test")
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "first")
            first = _git(source, "rev-parse", "HEAD")
            (source / "flavors" / "myflavor" / "flavor.md").write_text(
                "---\nname: myflavor\nversion: 2.0.0\n---\n", encoding="utf-8"
            )
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "second")
            catalog_copy(
                dest,
                f"git:{source.resolve().as_uri()}#{first}",
                ["flavor:myflavor"],
            )
            self.assertNotIn(
                "2.0.0",
                (dest / "flavors" / "myflavor" / "flavor.md").read_text(),
            )

    def test_git_source_rejects_embedded_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dest = make_project(Path(d) / "dest", "proj-dst")
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(
                    dest,
                    "git:https://user:s3cret@example.invalid/repo.git",
                    ["flavor:x"],
                )
            self.assertEqual(raised.exception.code, "catalog.import_git_credentials")
            self.assertNotIn("s3cret", str(raised.exception))
            self.assertNotIn("s3cret", raised.exception.message)

    def test_git_source_rejects_option_like_locators(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dest = make_project(Path(d) / "dest", "proj-dst")
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(dest, "git:--upload-pack=evil", ["flavor:x"])
            self.assertEqual(raised.exception.code, "catalog.import_git_invalid")


if __name__ == "__main__":
    unittest.main()
