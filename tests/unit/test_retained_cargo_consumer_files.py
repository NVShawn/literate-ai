"""Local file custody around a real verified retained-package materialization."""

import os
import unittest
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.retained_cargo_consumer_files import (
    read_retained_cargo_consumer_files,
)
from literate_ai.contracts.retained_cargo import CargoManifestChange
from literate_ai.projects import PinnedInputClosureError
from tests.unit import test_retained_cargo_materialization as fixtures


class RetainedCargoConsumerFilesTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RetainedCargoMaterializationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.materialized = fixture.run_materializer()
        self.root = fixture.root
        (self.root / ".cargo").mkdir()
        (self.root / ".cargo/config.toml").write_text("[net]\noffline = true\n")
        (self.root / "build").mkdir()
        (self.root / "build/consumer.rs").write_text("// retained source\n")

    def capture(self, **changes):
        return read_retained_cargo_consumer_files(
            self.materialized, **{"environment": {"OBJ_DIR": "pkgs"}, **changes}
        )

    def test_snapshot_excludes_only_declared_disposable_roots(self):
        snapshot = self.capture()
        identity = snapshot.current_identity()
        names = {name for name, _ in snapshot.nodes}
        self.assertIn("build/consumer.rs", names)
        self.assertIn(".cargo/config.toml", names)
        for directory in ("cargo-output", "generated", "pkgs/scratch"):
            path = self.root / directory
            path.mkdir(parents=True, exist_ok=True)
            (path / "new").write_bytes(b"output")
        self.assertEqual(snapshot.current_identity(), identity)

    def test_addition_removal_and_identical_replacement_are_detected(self):
        snapshot = self.capture()
        path = self.root / "build/consumer.rs"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises((ValueError, PinnedInputClosureError)):
            snapshot.current_identity()
        path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "custody-changed"):
            snapshot.current_identity()
        snapshot = self.capture()
        (self.root / "new.rs").write_bytes(b"new input")
        with self.assertRaisesRegex(ValueError, "custody-changed"):
            snapshot.current_identity()

    def test_next_build_captures_changed_source_without_changing_provider_package(self):
        snapshot = self.capture()
        before = snapshot.current_identity()
        (self.root / "build/consumer.rs").write_text("// new consumer source\n")
        with self.assertRaises((ValueError, PinnedInputClosureError)):
            snapshot.current_identity()
        # The same materialization and provider package remain current; a new
        # per-run snapshot binds the edited source instead of a clean Git lock.
        self.assertNotEqual(self.capture().current_identity(), before)

    def test_cached_directory_metadata_cannot_replace_physical_observation(self):
        snapshot = self.capture()
        identity = snapshot.current_identity()
        scan = os.scandir

        @contextmanager
        def cached_scan(path):
            with scan(path) as entries:

                def cached_entries():
                    for entry in entries:
                        node = entry.stat(follow_symlinks=False)
                        fields = {
                            name: getattr(node, name)
                            for name in dir(node)
                            if name.startswith("st_")
                        }
                        fields.update(st_ino=0, st_dev=0, st_nlink=0)
                        yield SimpleNamespace(
                            name=entry.name,
                            stat=lambda fields=fields, **_: SimpleNamespace(**fields),
                        )

                yield cached_entries()

        with patch("os.scandir", cached_scan):
            self.materialized.require_unchanged()
            self.assertEqual(snapshot.current_identity(), identity)
            os.link(self.root / "build/consumer.rs", self.root / "foreign-link")
            with self.assertRaisesRegex(ValueError, "file-unsafe"):
                self.capture()

    def test_cargo_configuration_drift_is_detected(self):
        snapshot = self.capture()
        (self.root / ".cargo/config.toml").write_text("[net]\noffline = false\n")
        with self.assertRaises((ValueError, PinnedInputClosureError)):
            snapshot.current_identity()

    def test_links_and_hardlinks_are_refused(self):
        path = self.root / "linked"
        os.link(self.root / "build/consumer.rs", path)
        with self.assertRaisesRegex(ValueError, "file-unsafe"):
            self.capture()
        path.unlink()
        try:
            path.symlink_to(self.root / "build/consumer.rs")
        except OSError:
            self.skipTest("host cannot create symbolic links")
        with self.assertRaisesRegex(ValueError, "link-unsafe"):
            self.capture()

    def test_excluded_root_cannot_be_a_link(self):
        path = self.root / "cargo-output"
        try:
            path.symlink_to(self.root / "build", target_is_directory=True)
        except OSError:
            self.skipTest("host cannot create symbolic links")
        with self.assertRaisesRegex(ValueError, "link-unsafe"):
            self.capture()

    def test_bounds_and_configuration_exclusion_fail_closed(self):
        for changes in (
            {"maximum_entries": 1},
            {"maximum_file_bytes": 1},
            {"maximum_entries": True},
            {"environment": {"OBJ_DIR": ".cargo"}},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises((ValueError, PinnedInputClosureError)),
            ):
                self.capture(**changes)

    def test_disposable_cache_cannot_hide_retained_manifest(self):
        plan = self.materialized.plan
        reference = next(
            item.after for item in plan.manifests if item.path == "Cargo.toml"
        )
        plan = replace(
            plan,
            manifests=tuple(
                sorted(
                    (
                        *plan.manifests,
                        CargoManifestChange("build/Cargo.toml", reference, reference),
                    ),
                    key=lambda item: item.path,
                )
            ),
        )
        self.materialized = replace(self.materialized, plan=plan)
        with self.assertRaisesRegex(ValueError, "exclusion-hides-input"):
            self.capture(environment={"OBJ_DIR": "build"})


if __name__ == "__main__":
    unittest.main()
