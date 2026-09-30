"""Real archive-to-package transactions with foreign-state preservation."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_cargo_materialization as materialization
from literate_ai.adapters.directory_artifacts import read_directory_export
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.retained_cargo_files import read_retained_cargo_files
from literate_ai.adapters.retained_package_tree import write_staged_package
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.projects import serialize_project_configuration
from tests.unit import test_project_configuration as projects
from tests.unit import test_retained_cargo_import as imports
from tests.unit.test_retained_cargo_files import blob


class RetainedCargoMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = imports.RetainedCargoImportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        temporary = tempfile.TemporaryDirectory(prefix="cm-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.binding_bytes = canonical_json_bytes(self.fixture.binding.to_dict())
        (self.root / "literate.project.json").write_bytes(
            serialize_project_configuration(
                replace(
                    projects._definition(),
                    project_id=self.fixture.binding.importer_project_id,
                )
            )
        )
        for name, content in (
            ("binding.json", self.binding_bytes),
            ("plan.json", self.fixture.plan_bytes),
            ("Cargo.toml", b"workspace"),
            ("Cargo.lock", b"lock"),
        ):
            (self.root / name).write_bytes(content)
        self.destinations = sorted(
            self.root / path for _, path in self.fixture.binding.destinations
        )

    def snapshot(self):
        return read_retained_cargo_files(
            self.root,
            binding_path="binding.json",
            reviewed_binding=blob(self.binding_bytes),
            plan_path="plan.json",
            manifest_state="after",
            package_state="provision",
        )

    def run_materializer(self, **changes):
        return materialization.materialize_retained_cargo_archive(
            self.snapshot(),
            environment={"OBJ_DIR": "pkgs"},
            **{**self.fixture.calls, **changes},
        )

    def populate(self, destination):
        destinations = dict(self.fixture.binding.destinations)
        blobs = dict(self.fixture.products.blobs)
        for manifest in self.fixture.binding.exports.graph.manifests:
            for export in manifest.exports:
                if self.root / destinations[export.identity] == destination:
                    content = read_directory_export(
                        blobs[export.blob], export.blob, max_bytes=4096, max_entries=100
                    )
                    destination.mkdir(parents=True)
                    return write_staged_package(destination, content)
        self.fail("fixture destination missing")

    def test_publish_then_reuse_exact_packages_without_rewriting(self):
        result = self.run_materializer()
        self.assertEqual(len(result.packages), len(self.destinations))
        before = {p: p.stat().st_ino for p in self.root.glob("pkgs/*/source/*")}
        result.require_unchanged()
        with result.custody():
            result.require_unchanged()
        reused = self.run_materializer()
        self.assertEqual(before, {p: p.stat().st_ino for p in before})
        reused.require_unchanged()
        self.assertEqual((self.root / "Cargo.lock").read_bytes(), b"lock")

    def test_mixed_existing_and_absent_packages(self):
        existing = self.populate(self.destinations[0])
        self.run_materializer().require_unchanged()
        existing.require_unchanged()
        self.assertTrue(all(p.is_dir() for p in self.destinations))

    def test_bad_archive_refuses_before_any_filesystem_mutation(self):
        before = set(self.root.iterdir())
        with self.assertRaises(ValueError):
            self.run_materializer(read_archive=lambda *args: b"invalid archive")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_foreign_file_in_existing_package_is_preserved(self):
        self.populate(self.destinations[0])
        foreign = self.destinations[0] / "foreign"
        foreign.write_bytes(b"owned elsewhere")
        with self.assertRaises(ValueError):
            self.run_materializer()
        self.assertEqual(foreign.read_bytes(), b"owned elsewhere")
        self.assertTrue(all(not p.exists() for p in self.destinations[1:]))

    def test_partial_existing_package_is_not_filled_in(self):
        self.destinations[0].mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.run_materializer()
        self.assertEqual(list(self.destinations[0].iterdir()), [])
        self.assertTrue(all(not p.exists() for p in self.destinations[1:]))

    def test_publication_failure_rolls_back_only_created_packages(self):
        original = materialization.publish_directory_exclusive
        count = 0

        def fail_second(source, destination, **kwargs):
            nonlocal count
            if destination in self.destinations:
                count += 1
                if count == 2:
                    destination.mkdir()
                    (destination / "foreign").write_bytes(b"keep")
                    raise FileExistsError("concurrent foreign destination")
            return original(source, destination, **kwargs)

        with patch.object(
            materialization, "publish_directory_exclusive", side_effect=fail_second
        ):
            with self.assertRaises(OrchestrationInventoryError) as raised:
                self.run_materializer()
        self.assertEqual(
            raised.exception.code,
            "orchestration.retained_materialization_publication_failed",
        )
        self.assertFalse(self.destinations[0].exists())
        self.assertEqual((self.destinations[1] / "foreign").read_bytes(), b"keep")
        self.assertTrue(list((self.root / "pkgs").glob(".rp-*")))

    def test_rollback_preserves_concurrent_changes_in_published_package(self):
        original = materialization.publish_directory_exclusive
        count = 0

        def fail_second(source, destination, **kwargs):
            nonlocal count
            if destination in self.destinations:
                count += 1
                if count == 2:
                    (self.destinations[0] / "foreign").write_bytes(b"keep")
                    raise OSError("injected publication failure")
            return original(source, destination, **kwargs)

        with patch.object(
            materialization, "publish_directory_exclusive", side_effect=fail_second
        ):
            with self.assertRaises(OrchestrationInventoryError):
                self.run_materializer()
        self.assertEqual((self.destinations[0] / "foreign").read_bytes(), b"keep")
        self.assertTrue(all(not p.exists() for p in self.destinations[1:]))

    def test_authored_lock_drift_during_staging_refuses_publication(self):
        original = materialization.write_staged_package

        def alter_lock(*args):
            result = original(*args)
            (self.root / "Cargo.lock").write_bytes(b"foreign lock")
            return result

        with patch.object(
            materialization, "write_staged_package", side_effect=alter_lock
        ):
            with self.assertRaises(ValueError):
                self.run_materializer()
        self.assertEqual((self.root / "Cargo.lock").read_bytes(), b"foreign lock")
        self.assertTrue(all(not p.exists() for p in self.destinations))

    def test_package_mutation_after_return_invalidates_custody(self):
        result = self.run_materializer()
        (self.destinations[0] / "foreign").write_bytes(b"keep")
        with self.assertRaises(ValueError):
            result.require_unchanged()
        with self.assertRaises(ValueError):
            with result.custody():
                self.fail("invalid package reached native gate boundary")

    def test_current_authority_drift_after_publication_rolls_back(self):
        def current():
            if all(p.exists() for p in self.destinations):
                return replace(self.fixture.inputs, configured_store_id="foreign-store")
            return self.fixture.inputs

        with self.assertRaisesRegex(ValueError, "current-inputs-changed"):
            self.run_materializer(read_current=current)
        self.assertTrue(all(not p.exists() for p in self.destinations))
        self.assertEqual((self.root / "Cargo.lock").read_bytes(), b"lock")

    def test_custody_rejects_competing_materializer(self):
        result = self.run_materializer()
        with result.custody():
            with self.assertRaises(OrchestrationInventoryError):
                self.run_materializer()
            result.require_unchanged()
        result.require_unchanged()
