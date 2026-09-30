"""Tests for litai catalog copy DAG-safe composition."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.catalog_import import (
    CatalogImportError,
    _is_git_source_spec,
    _reject_git_credentials,
    catalog_copy,
    catalog_graph,
)
from literate_ai.contracts.catalog_imports import (
    CatalogImportsFile,
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
    def test_malformed_source_manifest_does_not_leak_partial_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "leaked")
            destination = make_project(Path(d) / "destination", "dest")
            add_flavor(source, "flavor")
            (source / "literate.project.json").write_text(
                '{"project_id":"leaked","version":7}', encoding="utf-8"
            )
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(destination, str(source), ["flavor:flavor"])
            self.assertEqual(raised.exception.code, "catalog.import_source_invalid")

    def test_no_cycle_fresh_projects(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-a")
            dest = make_project(Path(d) / "dest", "proj-b")
            add_flavor(source, "myflavor")
            # Should not raise
            catalog_copy(dest, str(source), ["flavor:myflavor"])
            # imports.json created
            self.assertTrue((dest / ".literate" / "imports.json").exists())

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

    def test_transitive_cycle_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a = make_project(Path(d) / "a", "proj-a")
            b = make_project(Path(d) / "b", "proj-b")
            c = make_project(Path(d) / "c", "proj-c")
            add_flavor(a, "fa")
            add_flavor(b, "fb")
            add_flavor(c, "fc")

            # B copies from A: A → B edge
            catalog_copy(b, str(a), ["flavor:fa"])
            # C copies from B: A → B → C edges
            catalog_copy(c, str(b), ["flavor:fb"])
            # Now A tries to copy from C: would create cycle A → B → C → A
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(a, str(c), ["flavor:fc"])
            self.assertEqual(raised.exception.code, "catalog.import_cycle")

    def test_unknown_item_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            dest = make_project(Path(d) / "dest", "proj-dst")
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(dest, str(source), ["flavor:nonexistent"])
            self.assertEqual(raised.exception.code, "catalog.import_item_not_found")

    def test_conflict_without_overwrite_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            dest = make_project(Path(d) / "dest", "proj-dst")
            add_flavor(source, "myflavor")
            # First copy succeeds
            catalog_copy(dest, str(source), ["flavor:myflavor"])
            # Second copy without --overwrite fails
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(dest, str(source), ["flavor:myflavor"])
            self.assertEqual(raised.exception.code, "catalog.import_conflict")

    def test_overwrite_replaces_item(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            dest = make_project(Path(d) / "dest", "proj-dst")
            add_flavor(source, "myflavor")
            catalog_copy(dest, str(source), ["flavor:myflavor"])
            # Modify source flavor
            (source / "flavors" / "myflavor" / "flavor.md").write_text(
                "---\nname: myflavor\nversion: 2.0.0\n---\n", encoding="utf-8"
            )
            catalog_copy(dest, str(source), ["flavor:myflavor"], overwrite=True)
            self.assertIn(
                "2.0.0",
                (dest / "flavors" / "myflavor" / "flavor.md").read_text(),
            )

    def test_provenance_carries_transitive_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a = make_project(Path(d) / "a", "proj-a")
            b = make_project(Path(d) / "b", "proj-b")
            c = make_project(Path(d) / "c", "proj-c")
            add_flavor(a, "fa")
            add_flavor(b, "fb")

            # B copies from A
            catalog_copy(b, str(a), ["flavor:fa"])
            # C copies from B
            catalog_copy(c, str(b), ["flavor:fb"])

            imports = CatalogImportsFile.load(c)
            self.assertEqual(len(imports.imports), 1)
            src = imports.imports[0].source
            # Direct source is proj-b
            self.assertEqual(src.project_id, "proj-b")
            # Transitive ancestors include proj-a and literate-ai
            ancestor_ids = {ta.project_id for ta in src.transitive_ancestors}
            self.assertIn("proj-a", ancestor_ids)
            self.assertIn("literate-ai", ancestor_ids)

    def test_invalid_item_spec_format(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            dest = make_project(Path(d) / "dest", "proj-dst")
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(dest, str(source), ["openscad"])  # missing kind:
            self.assertEqual(raised.exception.code, "catalog.import_item_invalid")

    def test_workflow_and_routing_copy_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            dest = make_project(Path(d) / "dest", "proj-dst")
            workflow = source / "workflows" / "production" / "staging" / "dev"
            workflow.mkdir(parents=True)
            (workflow / "workflow.md").write_text("# dev\n", encoding="utf-8")
            routing = source / "routing" / "production" / "staging" / "dev"
            routing.mkdir(parents=True)
            (routing / "routing.json").write_text("{}\n", encoding="utf-8")
            catalog_copy(
                dest,
                str(source),
                [
                    "workflow:production/staging/dev",
                    "routing:production/staging/dev",
                ],
            )
            self.assertTrue(
                (
                    dest
                    / "workflows"
                    / "production"
                    / "staging"
                    / "dev"
                    / "workflow.md"
                ).is_file()
            )
            self.assertTrue(
                (
                    dest / "routing" / "production" / "staging" / "dev" / "routing.json"
                ).is_file()
            )

    def test_graph_renders_dag(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            dest = make_project(Path(d) / "dest", "proj-dst")
            add_flavor(source, "myflavor")
            catalog_copy(dest, str(source), ["flavor:myflavor"])

            graph = catalog_graph(dest)
            self.assertEqual(graph["project_id"], "proj-dst")
            node_ids = {n["project_id"] for n in graph["nodes"]}
            self.assertIn("proj-src", node_ids)
            self.assertIn("literate-ai", node_ids)
            edge_pairs = [(e["from"], e["to"]) for e in graph["edges"]]
            self.assertIn(("proj-src", "proj-dst"), edge_pairs)
            self.assertIn("flowchart", graph["mermaid"])

    def test_source_not_literate_ai_project_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            not_a_project = Path(d) / "not-a-project"
            not_a_project.mkdir()
            dest = make_project(Path(d) / "dest", "proj-dst")
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(dest, str(not_a_project), ["flavor:x"])
            self.assertEqual(raised.exception.code, "catalog.import_source_invalid")


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
    def test_git_source_copies_and_records_commit_ref(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            add_flavor(source, "myflavor")
            dest = make_project(Path(d) / "dest", "proj-dst")
            _git(source, "init", "--quiet", "-b", "main")
            _git(source, "config", "user.email", "catalog-test@example.com")
            _git(source, "config", "user.name", "catalog-test")
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "source")
            commit = _git(source, "rev-parse", "HEAD")
            result = catalog_copy(
                dest, f"git:{source.resolve().as_uri()}", ["flavor:myflavor"]
            )
            self.assertTrue((dest / "flavors" / "myflavor" / "flavor.md").is_file())
            self.assertTrue(result["source_ref"].startswith("git:"))
            self.assertTrue(result["source_ref"].endswith(f"@{commit}"))
            imports = CatalogImportsFile.load(dest)
            self.assertEqual(imports.imports[0].source.ref, result["source_ref"])
            self.assertEqual(imports.imports[0].source.project_id, "proj-src")

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

    def test_https_and_ssh_urls_are_git_sources_without_git_prefix(self) -> None:
        self.assertTrue(_is_git_source_spec("https://example.invalid/repo.git"))
        self.assertTrue(_is_git_source_spec("ssh://git@example.invalid/repo.git#main"))
        self.assertTrue(_is_git_source_spec("git@example.invalid:org/repo.git"))
        self.assertFalse(_is_git_source_spec("local:/tmp/project"))
        self.assertFalse(_is_git_source_spec("/tmp/project"))
        _reject_git_credentials("ssh://git@example.invalid/repo.git")
        _reject_git_credentials("git@example.invalid:org/repo.git")

    def test_file_uri_without_git_prefix_clones_at_head(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = make_project(Path(d) / "source", "proj-src")
            add_flavor(source, "myflavor")
            dest = make_project(Path(d) / "dest", "proj-dst")
            _git(source, "init", "--quiet", "-b", "main")
            _git(source, "config", "user.email", "catalog-test@example.com")
            _git(source, "config", "user.name", "catalog-test")
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "source")
            commit = _git(source, "rev-parse", "HEAD")
            result = catalog_copy(dest, source.resolve().as_uri(), ["flavor:myflavor"])
            self.assertTrue((dest / "flavors" / "myflavor" / "flavor.md").is_file())
            self.assertTrue(result["source_ref"].endswith(f"@{commit}"))

    def test_git_source_rejects_a_clone_that_is_not_a_literate_project(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "not-a-project"
            source.mkdir()
            dest = make_project(Path(d) / "dest", "proj-dst")
            _git(source, "init", "--quiet", "-b", "main")
            _git(source, "config", "user.email", "catalog-test@example.com")
            _git(source, "config", "user.name", "catalog-test")
            (source / "README").write_text("not a project\n", encoding="utf-8")
            _git(source, "add", ".")
            _git(source, "commit", "--quiet", "-m", "empty")
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(
                    dest, f"git:{source.resolve().as_uri()}", ["flavor:myflavor"]
                )
            self.assertEqual(raised.exception.code, "catalog.import_source_invalid")

    def test_git_source_rejects_option_like_locators(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dest = make_project(Path(d) / "dest", "proj-dst")
            with self.assertRaises(CatalogImportError) as raised:
                catalog_copy(dest, "git:--upload-pack=evil", ["flavor:x"])
            self.assertEqual(raised.exception.code, "catalog.import_git_invalid")


if __name__ == "__main__":
    unittest.main()
