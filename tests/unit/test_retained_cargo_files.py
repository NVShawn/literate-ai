"""Real read-only importer file custody, including planned additions and removals."""

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
from literate_ai.adapters.retained_cargo_files import read_retained_cargo_files
from literate_ai.adapters.retained_cargo_import import verify_retained_cargo_archive
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.retained_cargo import CargoManifestChange
from literate_ai.projects import serialize_project_configuration
from tests.unit import test_project_configuration as projects
from tests.unit import test_retained_cargo_import as imports
from tests.unit import test_retained_cargo_plan as plans


def blob(content):
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type="application/json"
    )


class RetainedCargoFileTests(unittest.TestCase):
    def test_fresh_checkout_custody_composes_with_real_archive_storage(self):
        fixture = imports.RetainedCargoImportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory(prefix="fi-") as directory:
            base = Path(directory).resolve()
            root = base / "project"
            root.mkdir()
            binding_bytes = canonical_json_bytes(fixture.binding.to_dict())
            (root / "literate.project.json").write_bytes(
                serialize_project_configuration(
                    replace(
                        projects._definition(),
                        project_id=fixture.binding.importer_project_id,
                    )
                )
            )
            (root / "binding.json").write_bytes(binding_bytes)
            (root / "plan.json").write_bytes(fixture.plan_bytes)
            for item in fixture.plan.manifests:
                if item.path not in {"Cargo.toml", "Cargo.lock"}:
                    continue
                path = root / item.path
                content = b"workspace" if item.path == "Cargo.toml" else b"lock"
                self.assertEqual(blob(content), item.after)
                path.write_bytes(content)
            writer = FileSystemEvidenceStore(base / "store", writable=True)
            reference = writer.put_bytes(fixture.archive, media_type="application/zip")
            self.assertEqual(reference, fixture.binding.qualification_archive)
            store = FileSystemEvidenceStore(base / "store")

            def current():
                files = read_retained_cargo_files(
                    root,
                    binding_path="binding.json",
                    reviewed_binding=blob(binding_bytes),
                    plan_path="plan.json",
                    manifest_state="after",
                    package_state="absent",
                )
                return replace(
                    fixture.inputs, require_unchanged=files.require_unchanged
                )

            def read(source, expected, limit):
                self.assertEqual(source, fixture.binding.source_store_id)
                self.assertLessEqual(expected.size, limit)
                return store.get_bytes(expected)

            arguments = {**fixture.calls, "read_current": current, "read_archive": read}
            self.assertEqual(
                verify_retained_cargo_archive(
                    fixture.binding, fixture.plan_bytes, **arguments
                ),
                (fixture.plan, fixture.products),
            )
            self.assertTrue(
                all(
                    not (root / destination).exists()
                    for _, destination in fixture.binding.destinations
                )
            )

            def changed_read(*args):
                content = read(*args)
                (root / "Cargo.lock").write_bytes(b"foreign concurrent bytes")
                return content

            with self.assertRaises(ValueError):
                verify_retained_cargo_archive(
                    fixture.binding,
                    fixture.plan_bytes,
                    **{**arguments, "read_archive": changed_read},
                )
            self.assertEqual(
                (root / "Cargo.lock").read_bytes(), b"foreign concurrent bytes"
            )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cf-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        plan, binding = plans.RetainedCargoPlanTests().fixture()
        self.before = b"reviewed before bytes"
        self.after = b"reviewed after bytes"
        added = plan.graph.packages[0].root + "/Cargo.toml"
        manifests = tuple(
            sorted(
                (
                    *(
                        CargoManifestChange(
                            m.path,
                            None if m.path == added else blob(self.before),
                            blob(self.after),
                        )
                        for m in plan.manifests
                    ),
                    CargoManifestChange("old/Cargo.toml", blob(self.before), None),
                ),
                key=lambda m: m.path,
            )
        )
        self.added = added
        self.plan = replace(plan, manifests=manifests)
        self.plan_bytes = canonical_json_bytes(self.plan.to_dict())
        self.binding = replace(binding, workspace_plan=blob(self.plan_bytes))
        self.binding_bytes = canonical_json_bytes(self.binding.to_dict())
        self.write(
            "literate.project.json",
            serialize_project_configuration(
                replace(
                    projects._definition(), project_id=self.binding.importer_project_id
                )
            ),
        )
        self.write("binding.json", self.binding_bytes)
        self.write("plan.json", self.plan_bytes)
        self.populate("before")
        self.arguments = dict(
            binding_path="binding.json",
            reviewed_binding=blob(self.binding_bytes),
            plan_path="plan.json",
            manifest_state="before",
        )

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def populate(self, state):
        for manifest in self.plan.manifests:
            path = self.root / manifest.path
            if getattr(manifest, state) is None:
                if path.exists():
                    path.unlink()
            else:
                self.write(
                    manifest.path, self.before if state == "before" else self.after
                )

    def read(self, **changes):
        return read_retained_cargo_files(self.root, **{**self.arguments, **changes})

    def inventory(self):
        return {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_before_and_after_snapshots_are_exact_and_read_only(self):
        before = self.inventory()
        snapshot = self.read()
        self.assertEqual(snapshot.binding, self.binding)
        self.assertEqual(snapshot.plan, self.plan)
        self.assertEqual(snapshot.absent_paths, (self.added,))
        self.assertEqual(snapshot.identity, self.read().identity)
        snapshot.require_unchanged()
        self.assertEqual(before, self.inventory())
        self.populate("after")
        after = self.inventory()
        current = self.read(manifest_state="after")
        self.assertEqual(current.absent_paths, ("old/Cargo.toml",))
        self.assertNotEqual(current.identity, snapshot.identity)
        current.require_unchanged()
        self.assertEqual(after, self.inventory())
        with self.assertRaises(ValueError):
            snapshot.require_unchanged()

    def test_changed_binding_plan_project_and_manifest_refuse(self):
        for relative in (
            "binding.json",
            "plan.json",
            "literate.project.json",
            "Cargo.lock",
        ):
            original = (self.root / relative).read_bytes()
            snapshot = self.read()
            self.write(relative, original + b" ")
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                snapshot.require_unchanged()
            self.write(relative, original)
        with self.assertRaises(ValueError):
            self.read(
                reviewed_binding=replace(blob(self.binding_bytes), digest="0" * 64)
            )
        with self.assertRaises(ValueError):
            self.read(
                reviewed_binding=replace(
                    blob(self.binding_bytes), size=len(self.binding_bytes) + 1
                )
            )

    def unmaterialized_after_state(self):
        self.populate("after")
        for _, destination in self.binding.destinations:
            path = self.root / destination
            if path.exists():
                shutil.rmtree(path)
        return self.read(manifest_state="after", package_state="absent")

    def test_fresh_checkout_verifies_authored_after_state_before_provisioning(self):
        snapshot = self.unmaterialized_after_state()
        before = self.inventory()
        self.assertEqual(snapshot.package_state, "absent")
        self.assertEqual(
            snapshot.absent_paths,
            tuple(
                sorted(["old/Cargo.toml", *dict(self.binding.destinations).values()])
            ),
        )
        snapshot.require_unchanged()
        self.assertEqual(self.inventory(), before)
        with self.assertRaises(ValueError):
            self.read(manifest_state="after")
        self.populate("after")
        present = self.read(manifest_state="after")
        self.assertEqual(present.package_state, "present")
        self.assertNotEqual(present.identity, snapshot.identity)
        with self.assertRaisesRegex(ValueError, "expected-absence-changed"):
            snapshot.require_unchanged()

    def test_absent_packages_do_not_relax_authored_file_custody(self):
        snapshot = self.unmaterialized_after_state()
        self.write("Cargo.lock", b"foreign lock")
        with self.assertRaises(ValueError):
            snapshot.require_unchanged()
        with self.assertRaises(ValueError):
            self.read(manifest_state="after", package_state="absent")
        self.assertEqual((self.root / "Cargo.lock").read_bytes(), b"foreign lock")

    def test_existing_or_concurrently_added_artifact_directory_is_preserved(self):
        snapshot = self.unmaterialized_after_state()
        destination = self.root / self.binding.destinations[0][1]
        destination.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "expected-absence-changed"):
            snapshot.require_unchanged()
        with self.assertRaisesRegex(ValueError, "expected-absence-changed"):
            self.read(manifest_state="after", package_state="absent")
        self.assertTrue(destination.is_dir())

    def test_absent_package_phase_requires_explicit_after_state(self):
        for changes in (
            {"package_state": "absent"},
            {"manifest_state": "after", "package_state": "auto"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "package-state-invalid"),
            ):
                self.read(**changes)

    def test_planned_absence_is_rechecked_and_does_not_delete_foreign_file(self):
        snapshot = self.read()
        foreign = b"foreign concurrent file"
        self.write(self.added, foreign)
        with self.assertRaisesRegex(ValueError, "expected-absence-changed"):
            snapshot.require_unchanged()
        self.assertEqual((self.root / self.added).read_bytes(), foreign)
        with self.assertRaisesRegex(ValueError, "expected-absence-changed"):
            self.read()

    def test_symlinked_absent_parent_and_existing_manifest_refuse(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / self.added
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside / "missing")
        except (OSError, NotImplementedError):
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(ValueError):
            self.read()
        link.unlink()
        link.parent.rmdir()
        link.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.read()
        link.parent.unlink()
        (self.root / "Cargo.lock").unlink()
        (outside / "Cargo.lock").write_bytes(self.before)
        (self.root / "Cargo.lock").symlink_to(outside / "Cargo.lock")
        with self.assertRaises(ValueError):
            self.read()

    def test_wrong_project_duplicate_json_and_bounds_refuse(self):
        data = json.loads(self.binding_bytes)
        data["importer_project_id"] = "foreign"
        candidate = canonical_json_bytes(data)
        self.write("binding.json", candidate)
        with self.assertRaisesRegex(ValueError, "importer-project-mismatch"):
            self.read(reviewed_binding=blob(candidate))
        candidate = b'{"schema":"wrong",' + self.binding_bytes[1:]
        self.write("binding.json", candidate)
        with self.assertRaisesRegex(ValueError, "binding-file-invalid"):
            self.read(reviewed_binding=blob(candidate))
        self.write("binding.json", self.binding_bytes)
        for changes in (
            {"maximum_file_bytes": 1},
            {"maximum_total_bytes": 1},
            {"manifest_state": "maybe"},
            {"binding_path": "../binding.json"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.read(**changes)


if __name__ == "__main__":
    unittest.main()
