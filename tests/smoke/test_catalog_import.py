"""Tests for litai catalog copy DAG-safe composition."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.catalog_import import (
    CatalogImportError,
    _open_source,
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
    def test_selected_catalog_slice_leaves_unrelated_blobs_unfetched(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            add_flavor(source, "myflavor")
            unrelated = source / "unrelated" / "payload"
            unrelated.parent.mkdir()
            unrelated.write_bytes(os.urandom(65536))
            _git(source, "init", "--quiet", "-b", "main")
            _git(source, "config", "uploadpack.allowFilter", "true")
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "source")
            selected = _git(source, "rev-parse", "HEAD")
            excluded_blob = _git(source, "rev-parse", "HEAD:unrelated/payload")
            with _open_source(
                f"git:{source.resolve().as_uri()}#{selected}",
                (".literate", "flavors/myflavor"),
            ) as (snapshot, _ref):
                self.assertTrue((snapshot / "literate.project.json").is_file())
                self.assertTrue((snapshot / "flavors/myflavor/flavor.md").is_file())
                self.assertFalse((snapshot / "unrelated/payload").exists())
                missing = _git(
                    snapshot, "rev-list", "--objects", "--missing=print", "HEAD"
                )
                self.assertIn("?" + excluded_blob, missing.splitlines())

    def test_catalog_copy_fetches_selected_submodule_without_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            child = Path(d) / "child"
            child.mkdir()
            _git(child, "init", "--quiet", "-b", "main")
            (child / "flavor.md").write_text("selected flavor\n")
            _git(child, "add", ".")
            _git(child, "commit", "--quiet", "-m", "selected")
            source = make_project(Path(d) / "source", "proj-src")
            dest = make_project(Path(d) / "dest", "proj-dst")
            _git(source, "init", "--quiet", "-b", "main")
            for path in ("flavors/myflavor", "unrelated/child"):
                _git(
                    source,
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(child),
                    path,
                )
            # An unavailable unrelated submodule must not prevent catalog acquisition.
            _git(
                source,
                "config",
                "--file",
                ".gitmodules",
                "submodule.unrelated/child.url",
                str(Path(d) / "missing"),
            )
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "source")
            selected = _git(source, "rev-parse", "HEAD")
            catalog_copy(
                dest, f"git:{source.resolve().as_uri()}#{selected}", ["flavor:myflavor"]
            )
            copied = dest / "flavors/myflavor"
            self.assertEqual((copied / "flavor.md").read_text(), "selected flavor\n")
            self.assertFalse((copied / ".git").exists())

    def test_catalog_item_path_traversal_is_rejected_before_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dest = make_project(Path(d) / "dest", "proj-dst")
            for item in ("flavor:../outside", "skill:/absolute", "flavor:foo/../bar"):
                with (
                    self.subTest(item=item),
                    self.assertRaises(CatalogImportError) as raised,
                ):
                    catalog_copy(
                        dest, "git:https://example.invalid/unreachable.git", [item]
                    )
                self.assertEqual(raised.exception.code, "catalog.import_item_invalid")

    def test_exact_pin_fetch_omits_later_history_and_records_selected_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            add_flavor(source, "myflavor")
            _git(source, "init", "--quiet", "-b", "main")
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "selected")
            selected = _git(source, "rev-parse", "HEAD")
            (source / "unselected-later-input").write_bytes(b"later" * 65536)
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "later")
            later = _git(source, "rev-parse", "HEAD")
            with _open_source(f"git:{source.resolve().as_uri()}#{selected}") as (
                snapshot,
                ref,
            ):
                self.assertEqual(_git(snapshot, "rev-parse", "HEAD"), selected)
                self.assertEqual(
                    _git(snapshot, "rev-parse", "--is-shallow-repository"), "true"
                )
                self.assertEqual(_git(snapshot, "rev-list", "--count", "HEAD"), "1")
                with self.assertRaises(subprocess.CalledProcessError):
                    _git(snapshot, "cat-file", "-e", later)
                self.assertFalse((snapshot / "unselected-later-input").exists())
                self.assertTrue(ref.endswith("@" + selected))

    def test_sha256_commit_uses_the_matching_git_object_format(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            try:
                _git(source, "init", "--quiet", "--object-format=sha256", "-b", "main")
            except subprocess.CalledProcessError:
                self.skipTest("Git does not support SHA-256 repositories")
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "selected")
            selected = _git(source, "rev-parse", "HEAD")
            self.assertEqual(len(selected), 64)
            with _open_source(f"git:{source.resolve().as_uri()}#{selected}") as (
                snapshot,
                ref,
            ):
                self.assertEqual(_git(snapshot, "rev-parse", "HEAD"), selected)
                self.assertEqual(
                    _git(snapshot, "rev-parse", "--show-object-format"), "sha256"
                )
                self.assertEqual(
                    _git(snapshot, "rev-parse", "--is-shallow-repository"), "true"
                )
                self.assertTrue(ref.endswith("@" + selected))

    def test_selected_snapshot_initializes_its_submodule_revision(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            child = Path(d) / "child"
            child.mkdir()
            _git(child, "init", "--quiet", "-b", "main")
            (child / "payload").write_text("selected child\n")
            _git(child, "add", ".")
            _git(child, "commit", "--quiet", "-m", "selected child")
            child_pin = _git(child, "rev-parse", "HEAD")
            (child / "payload").write_text("later child\n")
            _git(child, "add", ".")
            _git(child, "commit", "--quiet", "-m", "later child")
            child_later = _git(child, "rev-parse", "HEAD")
            source = make_project(Path(d) / "source", "proj-src")
            _git(source, "init", "--quiet", "-b", "main")
            _git(
                source,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(child),
                "vendor/child",
            )
            _git(source / "vendor/child", "checkout", "--quiet", child_pin)
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "selected parent")
            selected = _git(source, "rev-parse", "HEAD")
            _git(source / "vendor/child", "checkout", "--quiet", child_later)
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "later parent")
            with _open_source(f"git:{source.resolve().as_uri()}#{selected}") as (
                snapshot,
                _ref,
            ):
                self.assertEqual(
                    _git(snapshot / "vendor/child", "rev-parse", "HEAD"), child_pin
                )
                self.assertEqual(
                    (snapshot / "vendor/child/payload").read_text(), "selected child\n"
                )

    def test_missing_pin_does_not_mutate_destination(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            dest = make_project(Path(d) / "dest", "proj-dst")
            _git(source, "init", "--quiet", "-b", "main")
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "source")
            before = {
                p.relative_to(dest): p.read_bytes()
                for p in dest.rglob("*")
                if p.is_file()
            }
            with self.assertRaises(CatalogImportError):
                catalog_copy(
                    dest,
                    f"git:{source.resolve().as_uri()}#{'0' * 40}",
                    ["flavor:missing"],
                )
            after = {
                p.relative_to(dest): p.read_bytes()
                for p in dest.rglob("*")
                if p.is_file()
            }
            self.assertEqual(after, before)

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
